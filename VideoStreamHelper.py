from collections import deque
import subprocess
from utils import *
import numpy as np
import re

class FFmpegFrameReader:
    """Read decoded BGR frames from an FFmpeg subprocess we drive ourselves.

    cv2.VideoCapture buffers roughly 0.6s on live network sources and ignores FFmpeg's
    low-latency options however they are passed (OPENCV_FFMPEG_CAPTURE_OPTIONS included).
    Driving FFmpeg directly, with -fflags nobuffer / -flags low_delay / tiny probesize,
    measured ~110ms of ingest latency against ~600ms through VideoCapture on the same
    stream -- by far the largest single latency saving available in this pipeline.
    """

    LOW_LATENCY = ["-fflags", "nobuffer", "-flags", "low_delay",
                   "-probesize", "32", "-analyzeduration", "0"]

    def __init__(self, url, extra_input=()):
        self.url = url
        self.extra_input = list(extra_input)
        self.proc = None
        self.width = None
        self.height = None
        self.frame_bytes = 0
        self.tail = deque(maxlen=25)

    def open(self, timeout=15.0):
        """Start FFmpeg and wait until it reports the decoded frame geometry."""
        cmd = ["ffmpeg", "-nostats", *self.LOW_LATENCY, *self.extra_input,
               "-i", self.url, "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-fps_mode", "passthrough", "-"]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception as e:
            print(f"Failed to start FFmpeg reader: {e}")
            return False

        found = {}

        def watch(line):
            if "wh" not in found:
                m = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", line)
                if m:
                    found["wh"] = (int(m.group(1)), int(m.group(2)))

        self.tail = drain_pipe(self.proc.stderr, on_line=watch)

        deadline = time.time() + timeout
        while time.time() < deadline and "wh" not in found:
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)

        if "wh" not in found:
            print(f"Error: FFmpeg could not open '{self.url}'.")
            report_child_failure("ffmpeg-in", self.proc, self.tail)
            close_process(self.proc, grace=0)
            self.proc = None
            return False

        self.width, self.height = found["wh"]
        self.frame_bytes = self.width * self.height * 3
        print(f"Ingesting {self.width}x{self.height} via FFmpeg (low-latency mode)")
        return True

    def read(self):
        """Return the next frame as a writable HxWx3 BGR array, or None at end of stream."""
        if self.proc is None or self.proc.stdout is None:
            return None
        buf = bytearray(self.frame_bytes)
        view = memoryview(buf)
        filled = 0
        while filled < self.frame_bytes:
            try:
                n = self.proc.stdout.readinto(view[filled:])
            except (OSError, ValueError):
                return None
            if not n:
                return None
            filled += n
        return np.frombuffer(buf, np.uint8).reshape(self.height, self.width, 3)

    def release(self):
        if self.proc:
            close_process(self.proc, grace=0)
            self.proc = None
class GStreamerUDPReader:
    """Read decoded BGR frames from GStreamer for UDP RTP H.264 streams on Windows.

    Passively receives UDP packets on the specified port, decodes directly via avdec_h264
    in GStreamer, and streams raw BGR frames over a local loopback TCP socket into Python.
    """

    def __init__(self, port):
        self.port = port
        self.gst_proc = None
        self.server_sock = None
        self.client_conn = None
        self.width = None
        self.height = None
        self.frame_bytes = 0
        self.tail = deque(maxlen=25)
        self.running = False

    def open(self, timeout=8.0):
        gst_executable = find_gstreamer_path()
        if not gst_executable:
            print("Error: Could not find GStreamer installation on this PC. Please verify it is installed.")
            return False

        # Sniff UDP stream for in-band SPS/PPS parameter sets
        sprop = probe_stream_parameters(self.port, timeout=0.3)
        if not sprop:
            # Standard fallback parameter sets for streams that omit inline SPS/PPS
            sprop = 'Z0LAKNoB4AiflwFqAgICgAAAAwAAAwBxiEZA,aM48gA=='

        # Allocate ephemeral local TCP port for loopback BGR streaming
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(('127.0.0.1', 0))
        tcp_port = self.server_sock.getsockname()[1]
        self.server_sock.listen(1)
        self.server_sock.settimeout(timeout)

        sprop_escaped = sprop.replace(',', r'\,').replace('=', r'\=')
        caps_str = f'application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264,sprop-parameter-sets=(string)"{sprop_escaped}"'

        gst_cmd = [
            gst_executable, '-v',
            'udpsrc', f'port={self.port}',
            f'caps={caps_str}',
            '!', 'rtpjitterbuffer', 'latency=20',
            '!', 'rtph264depay',
            '!', 'h264parse',
            '!', 'avdec_h264',
            '!', 'videoconvert',
            '!', 'video/x-raw,format=BGR',
            '!', 'tcpclientsink', 'host=127.0.0.1', f'port={tcp_port}'
        ]

        print(f"Ingesting UDP RTP H264 on port {self.port} via GStreamer direct decoder...")
        try:
            self.gst_proc = subprocess.Popen(
                gst_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
        except Exception as e:
            print(f"Failed to spawn GStreamer: {e}")
            self.release()
            return False

        found_geom = {}

        def watch_stdout():
            try:
                for raw in iter(self.gst_proc.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip()
                    self.tail.append(line)
                    if "wh" not in found_geom:
                        m = re.search(r'video/x-raw.*?width=\(int\)(\d+).*?height=\(int\)(\d+)', line)
                        if m:
                            found_geom["wh"] = (int(m.group(1)), int(m.group(2)))
            except Exception:
                pass

        threading.Thread(target=watch_stdout, daemon=True).start()
        drain_pipe(self.gst_proc.stderr)

        try:
            self.client_conn, _ = self.server_sock.accept()
            self.client_conn.settimeout(5.0)
        except Exception as e:
            print(f"GStreamer failed to connect to local stream reader: {e}")
            self.release()
            return False

        deadline = time.time() + timeout
        while time.time() < deadline and "wh" not in found_geom:
            if self.gst_proc.poll() is not None:
                break
            time.sleep(0.02)

        if "wh" not in found_geom:
            print(f"Error: Could not determine geometry from GStreamer stream on port {self.port}.")
            self.release()
            return False

        self.width, self.height = found_geom["wh"]
        self.frame_bytes = self.width * self.height * 3
        self.running = True
        print(f"Ingesting {self.width}x{self.height} via GStreamer (direct decoder)")
        return True

    def read(self):
        if not self.running or self.client_conn is None:
            return None
        buf = bytearray(self.frame_bytes)
        view = memoryview(buf)
        filled = 0
        while filled < self.frame_bytes:
            try:
                n = self.client_conn.recv_into(view[filled:])
            except (socket.timeout, OSError):
                return None
            if not n:
                return None
            filled += n
        return np.frombuffer(buf, np.uint8).reshape(self.height, self.width, 3)

    def release(self):
        self.running = False
        if self.client_conn:
            try:
                self.client_conn.close()
            except Exception:
                pass
            self.client_conn = None
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None
        if self.gst_proc:
            try:
                self.gst_proc.terminate()
                self.gst_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.gst_proc.kill()
                except Exception:
                    pass
            self.gst_proc = None

# --- Thread 3: Video Transmission ---
class VideoOutputThread(threading.Thread):
    def __init__(self, dest_port, encoder, output_queue, stats_callback, record_enabled=False, record_mode="Processed", forward_enabled=True):
        super().__init__(daemon=True)
        self.dest_port = dest_port
        self.encoder = encoder
        self.output_queue = output_queue
        self.stats_callback = stats_callback
        self.record_enabled = record_enabled
        self.record_mode = record_mode
        self.forward_enabled = forward_enabled
        self.running = True
        self.ffmpeg_process = None
        self.ffmpeg_tail = deque()
        self.record_process = None
        self.record_tail = deque()
        self.frame_size = None

    def run(self):
        """Emit frames to FFmpeg on a true constant-rate cadence.

        FFmpeg is told the stream is CFR at DEFAULT_OUTPUT_FPS, so it stamps every frame
        exactly 1/fps apart no matter when it actually arrives. Simply forwarding frames
        as they turn up therefore makes RTP timestamps advance slower than real time
        whenever the pipeline runs below that rate, and the receiver's media clock falls
        progressively further behind -- measured at +16.5% of elapsed time at 25 FPS,
        i.e. unbounded, ever-growing delay in QGroundControl.

        Pacing the writer instead -- repeating the newest frame when starved, skipping
        stale ones when the source runs fast -- makes the declared rate honest and locks
        the media clock to the wall clock.
        """
        port = parse_port(self.dest_port)
        if port is None:
            print(f"Error: '{self.dest_port}' is not a valid destination port (expected 1-65535).")
            return

        frame_period = 1.0 / DEFAULT_OUTPUT_FPS
        latest = None
        next_send = None
        window_start = None
        window_recv = 0

        while self.running:
            now = time.perf_counter()
            wait = frame_period if next_send is None else max(0.0, next_send - now)
            frame = self.output_queue.get(timeout=min(wait, frame_period) or 0.001)

            if frame is not None:
                if self.frame_size is None:
                    if not self.start_stream(frame, port):
                        return
                    next_send = window_start = time.perf_counter()
                # Keep the array, not a bytes copy: .tobytes() would duplicate ~6 MB per
                # frame (187 MB/s at 1080p30) for nothing. FFmpeg's stdin takes the buffer
                # directly, and holding the array alive keeps that memoryview valid.
                latest = np.ascontiguousarray(fit_to(frame, self.frame_size))
                window_recv += 1

            if latest is None or next_send is None:
                continue

            now = time.perf_counter()
            if now < next_send:
                continue                      # not due yet -- keep draining for a fresher frame

            if now - window_start >= 5.0:
                self.report_source_rate(window_recv / (now - window_start))
                window_start, window_recv = now, 0

            if not self.send(latest):
                break

            next_send += frame_period
            # If we fell badly behind (encoder stall, machine hiccup), resync rather than
            # bursting a catch-up flood that would spike latency all over again.
            if time.perf_counter() - next_send > 0.5:
                next_send = time.perf_counter() + frame_period

        self.cleanup()

    def start_stream(self, frame, port):
        """Lock the stream geometry to the first frame, then start FFmpeg and recording.

        The geometry has to be fixed: FFmpeg is fed headerless rawvideo, so a mid-stream
        size change would desync every frame that followed it.
        """
        h, w = frame.shape[:2]
        self.frame_size = (w, h)
        if self.forward_enabled:
            print(f"Forwarding frame to QGroundControl on UDP port {port}")
            enc_codec, enc_opts = resolve_encoder(self.encoder)
            if not self.start_ffmpeg(port, enc_codec, enc_opts, w, h):
                return False
        if self.record_enabled and self.record_mode == "Processed":
            self.record_process, self.record_tail, rec_file = start_recorder(w, h, self.encoder, "processed")
            if rec_file:
                self.stats_callback("rec_status", f"Recording: {os.path.basename(rec_file)}")
        return True

    def set_recording(self, enabled, mode="Processed"):
        self.record_enabled = enabled
        self.record_mode = mode
        if not enabled and self.record_process:
            close_process(self.record_process, self.record_tail, "ffmpeg-rec")
            self.record_process = None
            self.stats_callback("rec_status", "Recording: Inactive")
        elif enabled and self.record_mode == "Processed" and self.record_process is None and self.frame_size is not None:
            w, h = self.frame_size
            self.record_process, self.record_tail, rec_file = start_recorder(w, h, self.encoder, "processed")
            if rec_file:
                self.stats_callback("rec_status", f"Recording: {os.path.basename(rec_file)}")

    def send(self, frame):
        """Write one frame to the stream and, if active, the recorder. False if the
        stream pipe died and the thread should stop."""
        if self.forward_enabled and self.ffmpeg_process:
            try:
                self.ffmpeg_process.stdin.write(frame.data)
            except OSError:
                print("FFmpeg streaming pipe closed.")
                return False

        if self.record_process:
            try:
                self.record_process.stdin.write(frame.data)
            except OSError:
                print("Recorder pipe closed; stopping local archive.")
                report_child_failure("ffmpeg-rec", self.record_process, self.record_tail)
                self.record_process = None
                self.stats_callback("rec_status", "Recording: Error")
        return True

    def report_source_rate(self, src_fps):
        """Warn when the pipeline is running meaningfully below the stream rate.

        The rate is measured by counting arrivals, not repeated sends: two unsynchronized
        30 Hz clocks beat against each other, so even a source keeping up perfectly lands
        ~18% of sends just before a frame arrives. Inferring the rate from repeats reports
        a healthy 30 FPS feed as 24 FPS.
        """
        if src_fps < DEFAULT_OUTPUT_FPS * 0.9:
            print(f"Note: source delivering ~{src_fps:.1f} FPS, below the "
                  f"{DEFAULT_OUTPUT_FPS} FPS stream rate; frames are being repeated "
                  f"to hold cadence (stream stays real-time).")

    def on_ffmpeg_progress(self, line):
        """Report the true encoded network bitrate straight from FFmpeg's -progress feed.

        Measuring the rawvideo bytes written into the pipe instead would report the
        uncompressed throughput (hundreds of Mbps), not what actually hits the tether.
        """
        if not line.startswith("bitrate="):
            return
        value = line.split("=", 1)[1].strip()
        if not value.endswith("kbits/s"):   # FFmpeg emits "N/A" until the first frames land
            return
        try:
            kbps = float(value[:-len("kbits/s")])
        except ValueError:
            return
        self.stats_callback("bitrate", f"{kbps / 1000.0:.2f} Mbps")

    def start_ffmpeg(self, port, enc_codec, enc_opts, width, height):
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(DEFAULT_OUTPUT_FPS),
            "-i", "-",
            "-c:v", enc_codec,
            *enc_opts,
            # Without this libx264 infers yuv444p from bgr24 input and emits a High 4:4:4
            # stream that QGroundControl and most hardware decoders refuse to play.
            "-pix_fmt", "yuv420p",
            # One keyframe per second. The default GOP is 250 frames (~8s), so a receiver
            # joining late -- or recovering from tether packet loss -- would sit frozen or
            # garbled for up to 8 seconds waiting on the next IDR.
            "-g", str(DEFAULT_OUTPUT_FPS),
            # Don't let the RTP muxer hold packets back (default muxdelay is 0.7s).
            "-muxdelay", "0", "-max_delay", "0",
            "-an",
            "-nostats", "-progress", "pipe:1",
            "-f", "rtp",
            f"rtp://127.0.0.1:{port}?pkt_size=1200"
        ]
        try:
            self.ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            drain_pipe(self.ffmpeg_process.stdout, on_line=self.on_ffmpeg_progress)
            self.ffmpeg_tail = drain_pipe(self.ffmpeg_process.stderr)
            print(f"Streaming {width}x{height} {enc_codec} to rtp://127.0.0.1:{port}")
            return True
        except Exception as e:
            print(f"Failed to start FFmpeg streaming: {e}")
            self.running = False
            return False

    def set_forwarding(self, enabled):
        """Enable or disable RTP forwarding while the thread is running."""
        if self.forward_enabled == enabled:
            return
        self.forward_enabled = enabled
        if enabled:
            # Start RTP forwarding
            if self.frame_size is None:
                print("Forwarding enabled; waiting for first frame to start FFmpeg.")
                return
            if self.ffmpeg_process is None:
                port = parse_port(self.dest_port)
                if port is None:
                    print(
                        f"Error: '{self.dest_port}' is not a valid destination port "
                        "(expected 1-65535)."
                    )
                    self.forward_enabled = False
                    return
                w, h = self.frame_size
                enc_codec, enc_opts = resolve_encoder(self.encoder)
                print(f"Forwarding enabled → UDP port {port}")
                if not self.start_ffmpeg(
                    port,
                    enc_codec,
                    enc_opts,
                    w,
                    h
                ):
                    self.forward_enabled = False
        else:
            # Stop RTP forwarding
            print("Forwarding disabled.")
            if self.ffmpeg_process:
                close_process(
                    self.ffmpeg_process,
                    self.ffmpeg_tail,
                    "ffmpeg-rtp"
                )
                self.ffmpeg_process = None
                self.ffmpeg_tail = deque()

    def cleanup(self):
        close_process(self.ffmpeg_process, self.ffmpeg_tail, "ffmpeg-rtp")
        self.ffmpeg_process = None
        close_process(self.record_process, self.record_tail, "ffmpeg-rec")
        self.record_process = None
        self.stats_callback("rec_status", "Recording: Inactive")

# --- Thread 1: Video Capture ---
class VideoInputThread(threading.Thread):
    def __init__(self, source_type, source_path, decoder, input_queue, stats_callback,
                 encoder="Software", record_raw=False):
        super().__init__(daemon=True)
        self.source_type = source_type
        self.source_path = source_path
        self.decoder = decoder  # TODO: not wired yet -- see "Known limitations" in readme.md
        self.input_queue = input_queue
        self.stats_callback = stats_callback
        self.encoder = encoder
        self.record_raw = record_raw
        self.running = True
        self.record_process = None
        self.record_tail = deque()
        self.raw_size = None
        self.reader = None

    def run(self):
        opened = self.open_source()
        if opened is None:
            self.cleanup()
            return
        cap, fps_target = opened
        try:
            self.capture_loop(cap, fps_target)
        finally:
            self.cleanup(cap)

    def open_source(self):
        """Resolve the configured source. Returns (cap, fps_target), or None on failure.

        Local files use OpenCV, which handles seeking so playback can loop. Live network
        sources instead set self.reader (FFmpeg direct or GStreamer) and return cap=None.
        """
        if self.source_type == "Video_File":
            cap = cv2.VideoCapture(self.source_path)
            if not cap.isOpened():
                print(f"Error: could not open video file '{self.source_path}'.")
                return None
            file_fps = cap.get(cv2.CAP_PROP_FPS)
            return cap, (file_fps if file_fps > 0 else 30.0)

        if self.source_type == "UDP H264":
            listen_port = parse_port(self.source_path)
            if listen_port is None:
                print(f"Error: '{self.source_path}' is not a valid UDP port (expected 1-65535).")
                return None
            self.reader = GStreamerUDPReader(listen_port)
            if not self.reader.open():
                self.reader = None
                return None
            return None, 30.0

        print(f"Configuring RTSP source... to {self.source_path}")
        self.reader = FFmpegFrameReader(self.source_path)
        if not self.reader.open():
            self.reader = None
            return None
        return None, 30.0

    def read_frame(self, cap):
        """Next frame from whichever backend this source uses, or None."""
        if cap is not None:
            ok, frame = cap.read()
            return frame if ok else None
        return self.reader.read() if self.reader else None

    def capture_loop(self, cap, fps_target):
        time_per_frame = 1.0 / fps_target
        last_time = time.time()
        frame_count = 0
        read_failures = 0

        while self.running:
            loop_start = time.perf_counter()
            frame = self.read_frame(cap)

            if frame is None:
                if cap is not None:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)     # loop the file
                    continue
                read_failures += 1
                if read_failures % 500 == 0:
                    print(f"Warning: {read_failures} consecutive empty reads from {self.source_type} source.")
                time.sleep(0.01)
                continue
            read_failures = 0

            if self.record_raw:
                self.write_raw(frame)

            self.input_queue.put(frame)
            frame_count += 1

            now = time.time()
            if now - last_time >= 1.0:
                self.stats_callback("input_fps", f"{frame_count / (now - last_time):.1f} FPS")
                frame_count = 0
                last_time = now

            if cap is not None:
                sleep_needed = time_per_frame - (time.perf_counter() - loop_start)
                if sleep_needed > 0:
                    time.sleep(sleep_needed)

    def set_recording(self, enabled):
        self.record_raw = enabled
        if not enabled and self.record_process:
            close_process(self.record_process, self.record_tail, "ffmpeg-raw")
            self.record_process = None
            self.stats_callback("rec_status", "Recording: Inactive")

    def write_raw(self, frame):
        """Archive the untouched source frame ("Raw Original" recording mode)."""
        if self.record_process is None:
            h, w = frame.shape[:2]
            self.record_process, self.record_tail, rec_file = start_recorder(
                w, h, self.encoder, "raw")
            if self.record_process is None:
                self.record_raw = False
                self.stats_callback("rec_status", "Recording: Failed to start")
                return
            self.raw_size = (w, h)
            if rec_file:
                self.stats_callback("rec_status", f"Recording: {os.path.basename(rec_file)}")
        try:
            frame = np.ascontiguousarray(fit_to(frame, self.raw_size))
            self.record_process.stdin.write(frame.data)
        except OSError:
            print("Raw recorder pipe closed; stopping raw archive.")
            report_child_failure("ffmpeg-raw", self.record_process, self.record_tail)
            self.record_process = None
            self.record_raw = False
            self.stats_callback("rec_status", "Recording: Error")

    def cleanup(self, cap=None):
        if cap is not None:
            cap.release()
        if self.reader:
            self.reader.release()
            self.reader = None
        close_process(self.record_process, self.record_tail, "ffmpeg-raw")
        self.record_process = None
        self.stats_callback("rec_status", "Recording: Inactive")