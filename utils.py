import os
import sys
from PyQt5.QtGui import QImage, QPixmap, QIcon
import shutil
import cv2
import threading
import subprocess
from collections import deque
import time
import socket
import base64

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


def get_app_dir():
    """Return the directory containing the script or frozen executable."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


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


def start_recorder(width, height, encoder, suffix, fps=DEFAULT_OUTPUT_FPS):
    """Spawn an FFmpeg process that archives raw BGR frames to a local MP4."""
    enc_codec, enc_opts = resolve_encoder(encoder)
    video_dir = os.path.join(get_app_dir(), "videos")
    os.makedirs(video_dir, exist_ok=True)
    rec_file = os.path.join(video_dir, f"ROV_Record_{suffix}_{int(time.time())}.mp4")
    rec_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", enc_codec, *enc_opts,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        rec_file
    ]
    try:
        process = subprocess.Popen(
            rec_cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        print(f"Recording {suffix} stream to {rec_file} ({width}x{height} @ {fps}fps)")
        return process, drain_pipe(process.stderr), rec_file
    except Exception as e:
        print(f"Failed to start FFmpeg record ({suffix}): {e}")
        return None, deque(), None


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

def probe_stream_parameters(port, timeout=0.4):
    """Sniff incoming UDP RTP packets to extract SPS/PPS or determine sender IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(timeout)
    sps, pps, sender_ip = None, None, None
    try:
        s.bind(('0.0.0.0', port))
    except Exception:
        return None, None

    start = time.time()
    while time.time() - start < timeout:
        try:
            p, addr = s.recvfrom(65536)
            if not sender_ip:
                sender_ip = addr[0]
            payload = p[12:]
            if not payload:
                continue
            hdr = payload[0]
            nal_type = hdr & 0x1F
            if nal_type == 24:  # STAP-A
                offset = 1
                while offset < len(payload):
                    if offset + 2 > len(payload):
                        break
                    sz = (payload[offset] << 8) | payload[offset + 1]
                    offset += 2
                    sub = payload[offset:offset + sz]
                    st = (sub[0] & 0x1F) if sub else -1
                    if st == 7 and not sps:
                        sps = sub
                    elif st == 8 and not pps:
                        pps = sub
                    offset += sz
            elif nal_type == 7 and not sps:
                sps = payload
            elif nal_type == 8 and not pps:
                pps = payload
            if sps and pps:
                break
        except (socket.timeout, OSError):
            break
        except Exception:
            break
    s.close()

    if sps and pps:
        sps_b64 = base64.b64encode(sps).decode('ascii')
        pps_b64 = base64.b64encode(pps).decode('ascii')
        return f"{sps_b64},{pps_b64}"

    return None