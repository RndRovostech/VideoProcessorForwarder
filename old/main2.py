import sys
import os
import subprocess
import time
import threading
from queue import Queue, Empty, Full
import cv2
import numpy as np
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QComboBox, QPushButton, QGroupBox, QCheckBox)
from PyQt5.QtCore import pyqtSignal, QObject, Qt
from PyQt5.QtGui import QImage, QPixmap

# Change this to your actual GStreamer install path
gstreamer_bin = r"D:\gstreamer\1.0\msvc_x86_64\bin"

if os.path.exists(gstreamer_bin):
    if gstreamer_bin not in os.environ["PATH"]:
        os.environ["PATH"] = gstreamer_bin + os.pathsep + os.environ["PATH"]
    
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(gstreamer_bin)
        except Exception:
            pass

# --- Thread Safe Frame Buffer ---
class FrameQueue:
    def __init__(self, maxsize=3):
        self.queue = Queue(maxsize=maxsize)
        
    def put(self, item):
        try:
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                except Empty:
                    pass
            self.queue.put_nowait(item)
            return True
        except Full:
            return False

    def get(self, timeout=0.1):
        try:
            return self.queue.get(timeout=timeout)
        except Empty:
            return None

# --- Thread 1: Video Capture ---
class VideoInputThread(threading.Thread):
    def __init__(self, source_type, source_path, decoder, input_queue, stats_callback):
        super().__init__()
        self.source_type = source_type
        self.source_path = source_path
        self.decoder = decoder
        self.input_queue = input_queue
        self.stats_callback = stats_callback
        self.running = True
        self.gst_process = None
        
    def run(self):
        local_port = 5601
        cap_path = self.source_path
        
        if self.source_type in ["RTSP", "UDP H264"]:
            hw_dec = "decodebin"
            if self.decoder == "NVDEC":
                hw_dec = "nvh264dec"
            elif self.decoder == "Intel":
                hw_dec = "qsvh264dec"
                
            if self.source_type == "RTSP":
                gst_cmd = [
                    "gst-launch-1.0", "-q",
                    "rtspsrc", f"location={self.source_path}", "latency=0", "!",
                    "rtph264depay", "!", "h264parse", "!", hw_dec, "!",
                    "videoconvert", "!", "video/x-raw,format=I420", "!",
                    "tcpserversink", f"port={local_port}", "host=127.0.0.1"
                ]
            else: 
                gst_cmd = [
                    "gst-launch-1.0", "-q",
                    "udpsrc", f"port={self.source_path}", 
                    # Define caps so GStreamer knows the incoming UDP data is RTP H264
                    "caps=application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264", "!",
                    "rtph264depay", "!", 
                    "h264parse", "!", 
                    hw_dec, "!",  # This decodes the H264 (e.g., avdec_h264)
                    "videoconvert", "!",
                    
                    # We must RE-ENCODE the raw frames and wrap them in a container to stream over TCP
                    "x264enc", "tune=zerolatency", "speed-preset=ultrafast", "!",
                    "mpegtsmux", "!",  # Wraps H264 into an MPEG Transport Stream
                    "tcpserversink", f"port={local_port}", "host=127.0.0.1"
                ]
                # gst_cmd = [
                #     "gst-launch-1.0", "-q",
                #     "udpsrc", f"port={self.source_path}", "!",
                #     "application/x-rtp,payload=96", "!",
                #     "rtph264depay", "!", "h264parse", "!", hw_dec, "!",
                #     "videoconvert", "!", "video/x-raw,format=I420", "!",
                #     "tcpserversink", f"port={local_port}", "host=127.0.0.1"
                # ]    
            try:
                command_str = " ".join(gst_cmd)
                self.gst_process = subprocess.Popen(
                    command_str, 
                    shell=True,
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL
                )
                time.sleep(0.5) 
                cap_path = f"tcp://127.0.0.1:{local_port}"
            except Exception as e:
                print(f"Failed to start GStreamer Subprocess: {e}")
                self.running = False

        cap = cv2.VideoCapture(cap_path, cv2.CAP_FFMPEG)
        
        fps_target = 30.0
        if self.source_type == "Video_File":
            file_fps = cap.get(cv2.CAP_PROP_FPS)
            if file_fps > 0:
                fps_target = file_fps
                
        time_per_frame = 1.0 / fps_target
        last_time = time.time()
        frame_count = 0
        
        while self.running:
            loop_start = time.perf_counter()
            
            ret, frame = cap.read()
            if not ret:
                if self.source_type == "Video_File":
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                time.sleep(0.01)
                continue
                
            self.input_queue.put(frame)
            frame_count += 1
            
            now = time.time()
            if now - last_time >= 1.0:
                fps = frame_count / (now - last_time)
                self.stats_callback("input_fps", f"{fps:.1f} FPS")
                frame_count = 0
                last_time = now

            if self.source_type == "Video_File":
                elapsed = time.perf_counter() - loop_start
                sleep_needed = time_per_frame - elapsed
                if sleep_needed > 0:
                    time.sleep(sleep_needed)

        cap.release()
        if self.gst_process:
            self.gst_process.terminate()
            self.gst_process.wait()

# --- Thread 2: Dedicated Video Processing Thread ---
class VideoProcessingThread(threading.Thread):
    def __init__(self, input_queue, output_queue, config, stats_callback, preview_signal):
        super().__init__()
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.config = config  # Dictionary containing dynamic UI checkbox states
        self.stats_callback = stats_callback
        self.preview_signal = preview_signal
        self.running = True
        
        # OpenCv Image Processors
        self.wb = cv2.xphoto.createGrayworldWB()
        self.wb.setSaturationThreshold(0.9)
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))

    def run(self):
        dropped_frames = 0
        while self.running:
            frame = self.input_queue.get(timeout=0.1)
            if frame is None:
                continue

            start_time = time.perf_counter()

            # Optional Recording of completely unaltered/raw frame
            if self.config["record_enabled"] and self.config["record_mode"] == "Raw Original":
                # We defer writing logic directly into the queue/process check
                pass 

            # Stage 1: Resize
            if self.config["resize"]:
                frame = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)

            # Stage 2: White Balance
            if self.config["white_balance"]:
                frame = self.wb.balanceWhite(frame)

            # Stage 3: CLAHE
            if self.config["clahe"]:
                lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                cl = self.clahe.apply(l)
                limg = cv2.merge((cl, a, b))
                frame = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)

            # Stage 4: Overlay Telemetry
            proc_time_ms = (time.perf_counter() - start_time) * 1000.0
            self.stats_callback("proc_time", f"{proc_time_ms:.1f} ms")
            
            cv2.putText(frame, f"Latency: {proc_time_ms:.1f}ms", (30, 60), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2, cv2.LINE_AA)

            # Push to transmitter
            pushed = self.output_queue.put(frame)
            if not pushed:
                dropped_frames += 1
                self.stats_callback("dropped", str(dropped_frames))

            # Send back to GUI Thread via safe Signal
            self.preview_signal.emit(frame)

            # Pacing logic for Video files
            if self.config["source_type"] == "Video_File":
                target_frame_time = 1.0 / 30.0
                elapsed_time = time.perf_counter() - start_time
                sleep_time = target_frame_time - elapsed_time
                if sleep_time > 0:
                    time.sleep(sleep_time)

# --- Thread 3: Video Transmission ---
class VideoOutputThread(threading.Thread):
    def __init__(self, dest_port, encoder, output_queue, stats_callback, record_enabled=False, record_mode="Processed"):
        super().__init__()
        self.dest_port = dest_port
        self.encoder = encoder
        self.output_queue = output_queue
        self.stats_callback = stats_callback
        self.record_enabled = record_enabled
        self.record_mode = record_mode
        self.running = True
        self.ffmpeg_process = None
        self.record_process = None
        
    def run(self):
        enc_codec = "libx264"
        enc_opts = ["-preset", "ultrafast", "-tune", "zerolatency"]
        
        if self.encoder == "NVENC":
            enc_codec = "h264_nvenc"
            enc_opts = ["-preset", "p1", "-tune", "ull"]
        elif self.encoder == "Intel QSV":
            enc_codec = "h264_qsv"
            enc_opts = ["-preset", "veryfast", "-async_depth", "1"]
            
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", "1920x1080",
            "-r", "30",
            "-i", "-", 
            "-c:v", enc_codec,
            *enc_opts,
            "-an",
            "-f", "rtp",
            f"rtp://127.0.0.1:{self.dest_port}?pkt_size=1200"
        ]
        
        try:
            self.ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:
            print(f"Failed to start FFmpeg streaming: {e}")
            self.running = False

        if self.record_enabled:
            rec_file = f"ROV_Record_{int(time.time())}.mp4"
            rec_cmd = [
                "ffmpeg", "-y",
                "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "bgr24", "-s", "1920x1080", "-r", "30",
                "-i", "-",
                "-c:v", enc_codec, *enc_opts,
                rec_file
            ]
            try:
                self.record_process = subprocess.Popen(rec_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"Failed to start FFmpeg record: {e}")

        last_time = time.time()
        sent_bytes = 0
        frame_bytes = 1920 * 1080 * 3 

        while self.running:
            frame = self.output_queue.get()
            if frame is None:
                continue
                
            try:
                self.ffmpeg_process.stdin.write(frame.tobytes())
                sent_bytes += frame_bytes
                
                if self.record_process and self.record_mode == "Processed":
                    self.record_process.stdin.write(frame.tobytes())
            except OSError:
                break
                
            now = time.time()
            if now - last_time >= 1.0:
                bitrate_mbps = (sent_bytes * 8) / (1024 * 1024) / (now - last_time)
                self.stats_callback("bitrate", f"{bitrate_mbps:.2f} Mbps")
                sent_bytes = 0
                last_time = now

        self.cleanup()

    def cleanup(self):
        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.stdin.close()
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait()
            except: pass
        if self.record_process:
            try:
                self.record_process.stdin.close()
                self.record_process.terminate()
                self.record_process.wait()
            except: pass

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
        
        # Thread config parameter bridge
        self.proc_config = {
            "resize": True,
            "white_balance": False,
            "clahe": False,
            "record_enabled": False,
            "record_mode": "Processed",
            "source_type": "Video_File"
        }
        # Add this dictionary for your default source paths
        self.default_sources = {
            "Video_File": "C:/Users/Jason/Downloads/UnderwaterVideoPlayback720.mp4",
            "RTSP": "rtsp://127.0.0.1:8554/live",
            "UDP H264": "5000"
        }

        self.initUI()
        
        # Safe Signal Connections
        self.stats_updated.connect(self.update_stats_label)
        self.preview_updated.connect(self.update_preview_window)

    def initUI(self):
        self.setWindowTitle("Rovostech Video Processor Forwarder")
        self.resize(700, 750)
        main_layout = QHBoxLayout() # Left: controls, Right: integrated preview

        # --- LEFT PANEL: CONTROLS ---
        left_panel = QVBoxLayout()

        # Source Group Box
        src_group = QGroupBox("Source Selection")
        src_layout = QVBoxLayout()
        self.source_type = QComboBox()
        self.source_type.addItems(["Video_File", "RTSP", "UDP H264"])
        self.source_input = QLineEdit("C:/Users/Jason/Downloads/UnderwaterVideoPlayback720.mp4")
        src_layout.addWidget(QLabel("Type:"))
        src_layout.addWidget(self.source_type)
        src_layout.addWidget(QLabel("URI / Filepath / Port:"))
        src_layout.addWidget(self.source_input)
        src_group.setLayout(src_layout)
        left_panel.addWidget(src_group)

        # Advanced Codec & Hardware Accelerators
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

        # Processing Stages Selection
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

        # Local Recording Options
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

        # Transmission Destination
        out_group = QGroupBox("Transmission Destination")
        out_layout = QVBoxLayout()
        self.dest_port = QLineEdit("5600")
        out_layout.addWidget(QLabel("QGroundControl UDP Port:"))
        out_layout.addWidget(self.dest_port)
        out_group.setLayout(out_layout)
        left_panel.addWidget(out_group)

        # Stream Engine Controls
        self.btn_start = QPushButton("Start Processing Pipeline")
        self.btn_start.clicked.connect(self.toggle_pipelines)
        self.btn_start.setStyleSheet("font-weight: bold; background-color: #2e7d32; color: white; padding: 10px;")
        left_panel.addWidget(self.btn_start)

        # Diagnostic Telemetry Dashboard
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

        # --- RIGHT PANEL: INTEGRATED VIDEO PREVIEW ---
        self.preview_label = QLabel("Video Stream Preview")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background-color: black; border: 2px solid #333; color: white;")
        self.preview_label.setMinimumSize(480, 270) # 16:9 Aspect ratio preview
        main_layout.addWidget(self.preview_label, 2)

        self.setLayout(main_layout)

        # Connect GUI controls dynamically to config bridge
        self.chk_resize.stateChanged.connect(self.sync_config)
        self.chk_wb.stateChanged.connect(self.sync_config)
        self.chk_clahe.stateChanged.connect(self.sync_config)
        self.chk_record.stateChanged.connect(self.sync_config)
        self.rec_mode.currentIndexChanged.connect(self.sync_config)
        self.source_type.currentTextChanged.connect(self.handle_source_type_change)
    
    def handle_source_type_change(self, selected_text):
        """Updates the text box with the default path whenever the dropdown changes."""
        if selected_text in self.default_sources:
            self.source_input.setText(self.default_sources[selected_text])
        
        # Keep your config bridge synchronized
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
        # Convert OpenCV BGR to QImage RGB safely on the UI Thread
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        
        # Resize to fit the UI preview widget (saving paint performance)
        preview_w = self.preview_label.width()
        preview_h = self.preview_label.height()
        
        small_frame = cv2.resize(frame, (preview_w, preview_h), interpolation=cv2.INTER_NEAREST)
        rgb_image = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)
        
        q_img = QImage(rgb_image.data, preview_w, preview_h, preview_w * ch, QImage.Format_RGB888)
        self.preview_label.setPixmap(QPixmap.fromImage(q_img))

    def toggle_pipelines(self):
        if self.processing_active:
            self.processing_active = False
            
            if self.input_thread:
                self.input_thread.running = False
            if self.process_thread:
                self.process_thread.running = False
            if self.output_thread:
                self.output_thread.running = False
                
            self.preview_label.clear()
            self.preview_label.setText("Video Stream Preview")
            self.btn_start.setText("Start Processing Pipeline")
            self.btn_start.setStyleSheet("font-weight: bold; background-color: #2e7d32; color: white; padding: 10px;")
        else:
            while self.input_queue.get(0.01) is not None: pass
            while self.output_queue.get(0.01) is not None: pass
            
            self.sync_config()
            self.processing_active = True
            
            self.input_thread = VideoInputThread(
                source_type=self.source_type.currentText(),
                source_path=self.source_input.text(),
                decoder=self.decoder_select.currentText(),
                input_queue=self.input_queue,
                stats_callback=self.handle_thread_stats
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
                record_enabled=self.chk_record.isChecked(),
                record_mode=self.rec_mode.currentText()
            )
            self.output_thread.start()

            self.btn_start.setText("Stop Processing Pipeline")
            self.btn_start.setStyleSheet("font-weight: bold; background-color: #c62828; color: white; padding: 10px;")

    def closeEvent(self, event):
        self.processing_active = False
        if self.input_thread:
            self.input_thread.running = False
        if self.process_thread:
            self.process_thread.running = False
        if self.output_thread:
            self.output_thread.running = False
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    ex = ROVProcessorApp()
    ex.show()
    sys.exit(app.exec_())