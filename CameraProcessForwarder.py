import os
# os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file,rtp,udp"

import sys
import re
import subprocess
import time
import threading
from collections import deque
from queue import Queue, Empty, Full
import cv2
import numpy as np
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QComboBox, QPushButton, QGroupBox, QCheckBox)
from PyQt5.QtCore import pyqtSignal, QObject, Qt
from PyQt5.QtGui import QImage, QPixmap, QIcon
import shutil
import ctypes

PREVIEW_FPS = 15.0          # GUI preview is throttled so it can never backlog the event loop
DEFAULT_OUTPUT_FPS = 30

APP_ICON = os.path.join("images", "AppIcon.ico")
APP_ID = "Rovostech.VideoProcessorForwarder"   # see the taskbar note in __main__


def resource_path(relative):
    """Absolute path to a bundled asset, working both from source and frozen.

    PyInstaller unpacks its `datas` into a temporary folder and points
    sys._MEIPASS at it. Running from source there is no such attribute and the
    assets sit beside this file, so the fallback is this script's own directory.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def load_app_icon():
    """Return the application icon, or an empty QIcon if the file is missing."""
    path = resource_path(APP_ICON)
    if os.path.exists(path):
        return QIcon(path)
    # Not fatal -- the app just falls back to the default Qt icon.
    print("Warning: app icon not found at %s (using the default)." % path)
    return QIcon()

def find_gstreamer_path():
    """Locate the absolute path to gst-launch-1.0.exe on Windows."""
    # 1. Check if it's already in the system PATH
    system_path = shutil.which("gst-launch-1.0")
    if system_path:
        return system_path

    # 2. Check the root env vars the official GStreamer installer sets
    for env_var in ("GSTREAMER_1_0_ROOT_MSVC_X86_64",
                    "GSTREAMER_1_0_ROOT_MINGW_X86_64",
                    "GSTREAMER_1_0_ROOT_X86_64"):
        root = os.environ.get(env_var)
        if root:
            candidate = os.path.join(root, "bin", "gst-launch-1.0.exe")
            if os.path.exists(candidate):
                return candidate

    # 3. Check standard Windows GStreamer installation paths.
    #    Installs are not always on C: -- probe every fixed drive letter.
    layouts = [
        r"{drive}:\gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe",
        r"{drive}:\gstreamer\1.0\mingw_x86_64\bin\gst-launch-1.0.exe",
        r"{drive}:\gstreamer\1.0\x86_64\bin\gst-launch-1.0.exe",
        r"{drive}:\gstreamer\1.0\x86\bin\gst-launch-1.0.exe",
    ]
    for drive in "CDEFG":
        for layout in layouts:
            path = layout.format(drive=drive)
            if os.path.exists(path):
                return path

    return None


def parse_port(value):
    """Return a valid TCP/UDP port number, or None if the text is not usable."""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def fit_to(frame, size):
    """Resize a frame only if it does not already match (width, height)."""
    if (frame.shape[1], frame.shape[0]) == size:
        return frame
    return cv2.resize(frame, size, interpolation=cv2.INTER_LINEAR)


def resolve_encoder(name):
    """Map the UI encoder choice onto an FFmpeg codec plus its low-latency options."""
    if name == "NVENC":
        return "h264_nvenc", ["-preset", "p1", "-tune", "ull"]
    if name == "Intel QSV":
        return "h264_qsv", ["-preset", "veryfast", "-async_depth", "1"]
    return "libx264", ["-preset", "ultrafast", "-tune", "zerolatency"]


def drain_pipe(pipe, keep=25, on_line=None):
    """Continuously consume a subprocess pipe so the OS buffer can never fill and deadlock.

    FFmpeg and GStreamer both write progress/diagnostics to stderr every second. Left
    unread, the 64 KB pipe buffer fills and the child process blocks forever mid-stream.
    Returns a deque holding the most recent lines, for reporting if the child dies.
    """
    tail = deque(maxlen=keep)

    def _reader():
        try:
            for raw in iter(pipe.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                tail.append(line)
                if on_line:
                    try:
                        on_line(line)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()
    return tail


def report_child_failure(label, process, tail):
    """Print the tail of a child process' stderr when it exited badly."""
    if process is None or process.poll() in (None, 0):
        return
    print(f"[{label}] exited with code {process.poll()}")
    for line in tail:
        print(f"[{label}] {line}")


def start_recorder(width, height, encoder, suffix):
    """Spawn an FFmpeg process that archives raw BGR frames to a local MP4.

    Deliberately no input -r: frames are stamped by arrival time (wallclock) and muxed
    VFR, so an archive is always real-time accurate even when the pipeline runs below
    the nominal frame rate. Declaring a fixed rate instead yields a sped-up recording.
    """
    enc_codec, enc_opts = resolve_encoder(encoder)
    rec_file = f"ROV_Record_{suffix}_{int(time.time())}.mp4"
    rec_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-use_wallclock_as_timestamps", "1",
        "-i", "-",
        "-c:v", enc_codec, *enc_opts,
        "-fps_mode", "vfr",
        "-pix_fmt", "yuv420p",
        rec_file
    ]
    try:
        process = subprocess.Popen(rec_cmd, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        print(f"Recording {suffix} stream to {rec_file} ({width}x{height})")
        return process, drain_pipe(process.stderr)
    except Exception as e:
        print(f"Failed to start FFmpeg record ({suffix}): {e}")
        return None, deque()


def close_process(process, tail=None, label=None, grace=5.0):
    """Close stdin and shut a child process down without raising.

    Closing stdin gives FFmpeg its EOF, and `grace` seconds to flush the encoder and
    write the MP4 moov atom -- terminating straight away truncates the recording into
    an unplayable file. Pass grace=0 for children that never exit on their own (gst).
    """
    if not process:
        return
    try:
        if process.stdin:
            process.stdin.close()
    except Exception:
        pass
    try:
        process.wait(timeout=grace)
    except Exception:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    if label and tail is not None:
        report_child_failure(label, process, tail)

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
        # -fps_mode passthrough is essential: with a tiny probesize FFmpeg cannot infer the
        # source frame rate and will pad the output to a guessed CFR, emitting ~2x the
        # frames as duplicates and doubling the work in every downstream stage.
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

        # The geometry is only known once FFmpeg has parsed the stream header, so watch
        # its stderr for the decoded video line rather than assuming a resolution.
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
        # A fresh buffer per frame: downstream queues hold on to these, so reusing one
        # would let the next read overwrite a frame still in flight.
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
        # Deliberate shutdown -- terminating always yields a non-zero code, so don't
        # dump the stderr tail as though something had gone wrong.
        if self.proc:
            close_process(self.proc, grace=0)
            self.proc = None

# --- Thread Safe Frame Buffer ---
class FrameQueue:
    def __init__(self, maxsize=3):
        self.queue = Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, item):
        """Store a frame, evicting the oldest if the buffer is full.

        Returns False when a frame was actually lost. The eviction *is* the drop, so it
        has to be counted here -- reporting only the (practically unreachable) Full case
        left the dropped-frame telemetry pinned at zero.
        """
        evicted = False
        try:
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                    evicted = True
                except Empty:
                    pass
            self.queue.put_nowait(item)
        except Full:
            evicted = True
        if evicted:
            self.dropped += 1
        return not evicted

    def reset_stats(self):
        self.dropped = 0

    def get(self, timeout=0.1):
        try:
            return self.queue.get(timeout=timeout)
        except Empty:
            return None

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
        self.gst_process = None
        self.gst_tail = deque()
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
        sources instead set self.reader (FFmpeg direct) and return cap=None, because
        cv2.VideoCapture buffers ~0.6s on them -- see FFmpegFrameReader.
        """
        if self.source_type == "Video_File":
            cap = cv2.VideoCapture(self.source_path)
            if not cap.isOpened():
                print(f"Error: could not open video file '{self.source_path}'.")
                return None
            file_fps = cap.get(cv2.CAP_PROP_FPS)
            return cap, (file_fps if file_fps > 0 else 30.0)

        if self.source_type == "UDP H264":
            url = self.launch_gst_bridge()
            if url is None:
                return None
        else:
            print(f"Configuring RTSP source... to {self.source_path}")
            url = self.source_path

        self.reader = FFmpegFrameReader(url)
        if not self.reader.open():
            self.reader = None
            return None
        return None, 30.0

    def launch_gst_bridge(self):
        """Start the GStreamer RTP->MPEG-TS bridge; returns its loopback URL, or None.

        FFmpeg ingests raw RTP poorly without an SDP, so GStreamer depayloads the
        incoming stream and republishes it as MPEG-TS on the next port up.
        """
        listen_port = parse_port(self.source_path)
        if listen_port is None:
            print(f"Error: '{self.source_path}' is not a valid UDP port (expected 1-65535).")
            return None
        if listen_port == 65535:
            print("Error: UDP port must be below 65535 (the bridge needs port+1).")
            return None
        bridge_port = listen_port + 1

        gst_executable = find_gstreamer_path()
        if not gst_executable:
            print("Error: Could not find GStreamer installation on this PC. Please verify it is installed.")
            return None

        print(f"Bridging RTP/H264 on udp:{listen_port} -> MPEG-TS on udp:{bridge_port}")
        print(f"  using {gst_executable}")
        gst_cmd = [
            gst_executable, "-q",
            "udpsrc", f"port={listen_port}",
            # Full caps are mandatory: rtpjitterbuffer/rtph264depay cannot negotiate
            # without media, clock-rate and encoding-name.
            "caps=application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264",
            # Shallow jitter buffer: it is pure added latency on a short tether run.
            # Raise it only if packet reordering causes visible tearing.
            "!", "rtpjitterbuffer", "latency=20",
            "!", "rtph264depay",
            "!", "h264parse",
            "!", "mpegtsmux",
            "!", "udpsink", "host=127.0.0.1", f"port={bridge_port}"
        ]

        try:
            self.gst_process = subprocess.Popen(
                gst_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            self.gst_tail = drain_pipe(self.gst_process.stderr)
            time.sleep(0.5)         # let GStreamer bind the port before FFmpeg connects
            if self.gst_process.poll() is not None:
                report_child_failure("gstreamer", self.gst_process, self.gst_tail)
                return None
        except Exception as e:
            print(f"Error launching background GStreamer process: {e}")
            return None

        return f"udp://127.0.0.1:{bridge_port}?overrun_nonfatal=1&fifo_size=50000000"

    def read_frame(self, cap):
        """Next frame from whichever backend this source uses, or None."""
        if cap is not None:
            ok, frame = cap.read()
            return frame if ok else None
        return self.reader.read()

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

            # Files have no natural clock, so pace playback to their frame rate.
            # Live sources arrive at their own pace already.
            if cap is not None:
                sleep_needed = time_per_frame - (time.perf_counter() - loop_start)
                if sleep_needed > 0:
                    time.sleep(sleep_needed)

    def write_raw(self, frame):
        """Archive the untouched source frame ("Raw Original" recording mode)."""
        if self.record_process is None:
            h, w = frame.shape[:2]
            self.record_process, self.record_tail = start_recorder(
                w, h, self.encoder, "raw")
            if self.record_process is None:
                self.record_raw = False
                return
            self.raw_size = (w, h)
        try:
            frame = np.ascontiguousarray(fit_to(frame, self.raw_size))
            self.record_process.stdin.write(frame.data)
        except OSError:
            print("Raw recorder pipe closed; stopping raw archive.")
            report_child_failure("ffmpeg-raw", self.record_process, self.record_tail)
            self.record_process = None
            self.record_raw = False

    def cleanup(self, cap=None):
        if cap is not None:
            cap.release()
        if self.reader:
            self.reader.release()
            self.reader = None
        close_process(self.record_process, self.record_tail, "ffmpeg-raw")
        self.record_process = None
        if self.gst_process:
            # gst-launch never exits on its own -- go straight to terminate
            close_process(self.gst_process, grace=0)
            self.gst_process = None

# --- Thread 2: Dedicated Video Processing Thread ---
class VideoProcessingThread(threading.Thread):
    def __init__(self, input_queue, output_queue, config, stats_callback, preview_signal):
        super().__init__(daemon=True)
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.config = config
        self.stats_callback = stats_callback
        self.preview_signal = preview_signal
        self.running = True

        # xphoto lives in opencv-contrib-python; degrade gracefully instead of crashing
        # the GUI thread when only the base opencv-python wheel is installed.
        self.wb = None
        if hasattr(cv2, "xphoto"):
            self.wb = cv2.xphoto.createGrayworldWB()
            self.wb.setSaturationThreshold(0.9)
        else:
            print("Warning: cv2.xphoto missing (install opencv-contrib-python). "
                  "White Balance will be skipped.")
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))

    def apply_filters(self, frame):
        """### ADD YOUR OWN IMAGE PROCESSING HERE ###

        This is the one method you need to touch to change what the video looks like.

        Args:
            frame: an OpenCV BGR image, numpy uint8 array of shape (height, width, 3).
                   Exactly what cv2.imread() or cap.read() gives you.

        Returns:
            A BGR uint8 frame. Return a new array or modify and return the one you were
            given -- both are fine.

        Rules that matter:
          1. Stay BGR uint8 with 3 channels. If you convert to grayscale, HSV or LAB,
             convert back before returning, or the encoder will reject the frame.
          2. Keep the size consistent. The stream geometry locks to the first frame, so
             a later size change is rescaled back and wastes work.
          3. Watch the clock. Check "Processing Overhead" in the window: stay under
             33 ms to hold 30 FPS. Slower is not fatal -- frames get dropped, not
             queued -- but the video gets choppy.
          4. This runs on a worker thread. Do not touch Qt widgets from here; use
             self.stats_callback(...) to report numbers to the GUI instead.
          5. Build expensive objects once in __init__, not per frame (see self.clahe).
        """
        if self.config["resize"]:
            frame = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)

        if self.config["white_balance"] and self.wb is not None:
            frame = self.wb.balanceWhite(frame)

        if self.config["clahe"]:
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            cl = self.clahe.apply(l)
            limg = cv2.merge((cl, a, b))
            frame = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)

        # --- your own stages go here ---

        return frame

    def run(self):
        last_dropped = -1
        last_preview = 0.0
        preview_interval = 1.0 / PREVIEW_FPS
        while self.running:
            frame = self.input_queue.get(timeout=0.1)
            if frame is None:
                continue

            # Normalize odd source formats to 3-channel BGR before any filter sees them
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif frame.shape[2] == 4:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

            start_time = time.perf_counter()
            frame = self.apply_filters(frame)
            proc_time_ms = (time.perf_counter() - start_time) * 1000.0
            self.stats_callback("proc_time", f"{proc_time_ms:.1f} ms")

            cv2.putText(frame, f"Latency: {proc_time_ms:.1f}ms", (30, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2, cv2.LINE_AA)

            # Push to transmitter, then report end-to-end loss across both stages
            self.output_queue.put(frame)
            total_dropped = self.input_queue.dropped + self.output_queue.dropped
            if total_dropped != last_dropped:
                last_dropped = total_dropped
                self.stats_callback("dropped", str(total_dropped))

            # Send back to GUI Thread via safe Signal.
            # Throttled: emitting all 30 FPS of 1080p frames outruns the Qt event loop
            # and the queued-signal backlog grows without bound.
            now = time.perf_counter()
            if now - last_preview >= preview_interval:
                last_preview = now
                self.preview_signal.emit(frame)

# --- Thread 3: Video Transmission ---
class VideoOutputThread(threading.Thread):
    def __init__(self, dest_port, encoder, output_queue, stats_callback, record_enabled=False, record_mode="Processed"):
        super().__init__(daemon=True)
        self.dest_port = dest_port
        self.encoder = encoder
        self.output_queue = output_queue
        self.stats_callback = stats_callback
        self.record_enabled = record_enabled
        self.record_mode = record_mode
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
        enc_codec, enc_opts = resolve_encoder(self.encoder)
        if not self.start_ffmpeg(port, enc_codec, enc_opts, w, h):
            return False
        if self.record_enabled and self.record_mode == "Processed":
            self.record_process, self.record_tail = start_recorder(w, h, self.encoder, "processed")
        return True

    def send(self, frame):
        """Write one frame to the stream and, if active, the recorder. False if the
        stream pipe died and the thread should stop."""
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

    def cleanup(self):
        close_process(self.ffmpeg_process, self.ffmpeg_tail, "ffmpeg-rtp")
        self.ffmpeg_process = None
        close_process(self.record_process, self.record_tail, "ffmpeg-rec")
        self.record_process = None

# --- Main Application Logic & GUI ---
class ROVProcessorApp(QWidget):
    stats_updated = pyqtSignal(str, str)
    preview_updated = pyqtSignal(np.ndarray)

    def __init__(self):
        super().__init__()

        self.input_queue = FrameQueue(maxsize=3)
        self.output_queue = FrameQueue(maxsize=3)
        self.input_thread = None
        self.process_thread = None
        self.output_thread = None
        self.processing_active = False

        self.proc_config = {
            "resize": True,
            "white_balance": False,
            "clahe": False,
            "record_enabled": False,
            "record_mode": "Processed",
            "source_type": "Video_File"
        }

        self.default_sources = {
            "Video_File": "./UnderwaterVideoPlayback720.mp4",
            "RTSP": "rtsp://192.168.2.160:774",
            "UDP H264": "5000"
        }

        self.initUI()

        self.stats_updated.connect(self.update_stats_label)
        self.preview_updated.connect(self.update_preview_window)

    def initUI(self):
        self.setWindowTitle("Rovostech Video Processor Forwarder")
        self.resize(700, 750)
        main_layout = QHBoxLayout()

        # --- LEFT PANEL ---
        left_panel = QVBoxLayout()

        src_group = QGroupBox("Source Selection")
        src_layout = QVBoxLayout()
        self.source_type = QComboBox()
        self.source_type.addItems(["UDP H264", "RTSP", "Video_File"])
        self.source_input = QLineEdit("5000")
        src_layout.addWidget(QLabel("Type:"))
        src_layout.addWidget(self.source_type)
        src_layout.addWidget(QLabel("URI / Filepath / Port:"))
        src_layout.addWidget(self.source_input)
        src_group.setLayout(src_layout)
        left_panel.addWidget(src_group)

        hw_group = QGroupBox("Acceleration Selection")
        hw_layout = QHBoxLayout()
        self.decoder_select = QComboBox()
        self.decoder_select.addItems(["Auto", "NVDEC", "Intel", "Software"])
        self.encoder_select = QComboBox()
        self.encoder_select.addItems(["Auto", "NVENC", "Intel QSV", "Software"])
        hw_layout.addWidget(QLabel("Dec:"))
        hw_layout.addWidget(self.decoder_select)
        hw_layout.addWidget(QLabel("Enc:"))
        hw_layout.addWidget(self.encoder_select)
        hw_group.setLayout(hw_layout)
        left_panel.addWidget(hw_group)

        proc_group = QGroupBox("Processing Filters")
        proc_layout = QVBoxLayout()
        self.chk_resize = QCheckBox("Resize (1080p)")
        self.chk_resize.setChecked(True)
        self.chk_wb = QCheckBox("White Balance")
        self.chk_clahe = QCheckBox("CLAHE")
        proc_layout.addWidget(self.chk_resize)
        proc_layout.addWidget(self.chk_wb)
        proc_layout.addWidget(self.chk_clahe)
        proc_group.setLayout(proc_layout)
        left_panel.addWidget(proc_group)

        rec_group = QGroupBox("Recording Options")
        rec_layout = QVBoxLayout()
        self.chk_record = QCheckBox("Enable Local Recording")
        self.rec_mode = QComboBox()
        self.rec_mode.addItems(["Processed", "Raw Original"])
        rec_layout.addWidget(self.chk_record)
        rec_layout.addWidget(QLabel("Mode:"))
        rec_layout.addWidget(self.rec_mode)
        rec_group.setLayout(rec_layout)
        left_panel.addWidget(rec_group)

        out_group = QGroupBox("Transmission Destination")
        out_layout = QVBoxLayout()
        self.dest_port = QLineEdit("5600")
        out_layout.addWidget(QLabel("QGroundControl UDP Port:"))
        out_layout.addWidget(self.dest_port)
        out_group.setLayout(out_layout)
        left_panel.addWidget(out_group)

        self.btn_start = QPushButton("Start Processing Pipeline")
        self.btn_start.clicked.connect(self.toggle_pipelines)
        self.btn_start.setStyleSheet("font-weight: bold; background-color: #2e7d32; color: white; padding: 10px;")
        left_panel.addWidget(self.btn_start)

        stats_group = QGroupBox("Diagnostic Telemetry")
        stats_layout = QVBoxLayout()
        self.lbl_in_fps = QLabel("Input Stream Rate: 0 FPS")
        self.lbl_proc_time = QLabel("Processing Overhead: 0.0 ms")
        self.lbl_bitrate = QLabel("Network Bitrate: 0.00 Mbps")
        self.lbl_dropped = QLabel("Dropped Frames: 0")
        stats_layout.addWidget(self.lbl_in_fps)
        stats_layout.addWidget(self.lbl_proc_time)
        stats_layout.addWidget(self.lbl_bitrate)
        stats_layout.addWidget(self.lbl_dropped)
        stats_group.setLayout(stats_layout)
        left_panel.addWidget(stats_group)

        main_layout.addLayout(left_panel, 1)

        # --- RIGHT PANEL ---
        self.preview_label = QLabel("Video Stream Preview")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background-color: black; border: 2px solid #333; color: white;")
        self.preview_label.setMinimumSize(480, 270)
        main_layout.addWidget(self.preview_label, 2)

        self.setLayout(main_layout)

        self.chk_resize.stateChanged.connect(self.sync_config)
        self.chk_wb.stateChanged.connect(self.sync_config)
        self.chk_clahe.stateChanged.connect(self.sync_config)
        self.chk_record.stateChanged.connect(self.sync_config)
        self.rec_mode.currentIndexChanged.connect(self.sync_config)
        self.source_type.currentTextChanged.connect(self.handle_source_type_change)

    def handle_source_type_change(self, selected_text):
        if selected_text in self.default_sources:
            self.source_input.setText(self.default_sources[selected_text])
        self.sync_config()

    def sync_config(self):
        self.proc_config["resize"] = self.chk_resize.isChecked()
        self.proc_config["white_balance"] = self.chk_wb.isChecked()
        self.proc_config["clahe"] = self.chk_clahe.isChecked()
        self.proc_config["record_enabled"] = self.chk_record.isChecked()
        self.proc_config["record_mode"] = self.rec_mode.currentText()
        self.proc_config["source_type"] = self.source_type.currentText()

    def handle_thread_stats(self, stat_type, value):
        self.stats_updated.emit(stat_type, value)

    def update_stats_label(self, stat_type, value):
        if stat_type == "input_fps":
            self.lbl_in_fps.setText(f"Input Stream Rate: {value}")
        elif stat_type == "bitrate":
            self.lbl_bitrate.setText(f"Network Bitrate: {value}")
        elif stat_type == "proc_time":
            self.lbl_proc_time.setText(f"Processing Overhead: {value}")
        elif stat_type == "dropped":
            self.lbl_dropped.setText(f"Dropped Frames: {value}")

    def update_preview_window(self, frame):
        if frame is None or frame.size == 0:
            return

        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        src_h, src_w = frame.shape[:2]
        box_w = max(self.preview_label.width(), 1)
        box_h = max(self.preview_label.height(), 1)

        # Fit inside the label without distorting the aspect ratio
        scale = min(box_w / src_w, box_h / src_h)
        target_w = max(int(src_w * scale), 1)
        target_h = max(int(src_h * scale), 1)

        small_frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
        rgb_image = np.ascontiguousarray(cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB))

        q_img = QImage(rgb_image.data, target_w, target_h, target_w * 3, QImage.Format_RGB888)
        self.preview_label.setPixmap(QPixmap.fromImage(q_img))

    def stop_pipelines(self):
        """Signal every worker to stop and wait for them, so a restart never runs two
        producers against the same queues."""
        for thread in (self.input_thread, self.process_thread, self.output_thread):
            if thread:
                thread.running = False
        for thread in (self.input_thread, self.process_thread, self.output_thread):
            if thread and thread.is_alive():
                thread.join(timeout=3.0)
                if thread.is_alive():
                    print(f"Warning: {type(thread).__name__} did not stop within 3s "
                          f"(likely blocked on a dead network read).")
        self.input_thread = None
        self.process_thread = None
        self.output_thread = None
        self.processing_active = False

    def toggle_pipelines(self):
        if self.processing_active:
            self.btn_start.setEnabled(False)
            try:
                self.stop_pipelines()
            finally:
                self.btn_start.setEnabled(True)

            self.preview_label.clear()
            self.preview_label.setText("Video Stream Preview")
            self.btn_start.setText("Start Processing Pipeline")
            self.btn_start.setStyleSheet("font-weight: bold; background-color: #2e7d32; color: white; padding: 10px;")
        else:
            while self.input_queue.get(0.01) is not None: pass
            while self.output_queue.get(0.01) is not None: pass
            self.input_queue.reset_stats()
            self.output_queue.reset_stats()
            self.lbl_dropped.setText("Dropped Frames: 0")

            self.sync_config()
            self.processing_active = True

            record_enabled = self.proc_config["record_enabled"]
            record_mode = self.proc_config["record_mode"]

            self.input_thread = VideoInputThread(
                source_type=self.source_type.currentText(),
                source_path=self.source_input.text(),
                decoder=self.decoder_select.currentText(),
                input_queue=self.input_queue,
                stats_callback=self.handle_thread_stats,
                encoder=self.encoder_select.currentText(),
                # "Raw Original" is archived at the source, before any filter runs
                record_raw=record_enabled and record_mode == "Raw Original"
            )
            self.input_thread.start()

            self.process_thread = VideoProcessingThread(
                input_queue=self.input_queue,
                output_queue=self.output_queue,
                config=self.proc_config,
                stats_callback=self.handle_thread_stats,
                preview_signal=self.preview_updated
            )
            self.process_thread.start()

            self.output_thread = VideoOutputThread(
                dest_port=self.dest_port.text(),
                encoder=self.encoder_select.currentText(),
                output_queue=self.output_queue,
                stats_callback=self.handle_thread_stats,
                record_enabled=record_enabled,
                record_mode=record_mode
            )
            self.output_thread.start()

            self.btn_start.setText("Stop Processing Pipeline")
            self.btn_start.setStyleSheet("font-weight: bold; background-color: #c62828; color: white; padding: 10px;")

    def closeEvent(self, event):
        self.stop_pipelines()
        event.accept()


if __name__ == "__main__":
    # Windows groups taskbar buttons by AppUserModelID, and a plain Python script
    # inherits python.exe's. Without this the taskbar shows the Python icon however
    # many times setWindowIcon is called -- the title bar updates, the taskbar does
    # not. Needed when running from source; harmless once frozen.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass    # not Windows, or the call is unavailable -- the icon still works

    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())     # applies to the window and any dialogs
    ex = ROVProcessorApp()
    ex.show()
    sys.exit(app.exec_())
