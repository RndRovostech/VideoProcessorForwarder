import os
import sys
import cv2
import numpy as np
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QComboBox, QPushButton, QGroupBox, QCheckBox)
from PyQt5.QtCore import pyqtSignal, QObject, Qt
from PyQt5.QtGui import QImage, QPixmap, QIcon
import ctypes
from utils import *
from VideoStreamHelper import VideoInputThread, VideoOutputThread
from VideoProcessing import VideoProcessingThread
from frame_queue import FrameQueue

PREVIEW_FPS = 5.0          # GUI preview is throttled so it can never backlog the event loop
DEFAULT_OUTPUT_FPS = 30

APP_ICON = os.path.join("images", "AppIcon.ico")
APP_ID = "Rovostech.VideoProcessorForwarder"   # see the taskbar note in __main__

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
        self.latest_frame = None
        self.capture_dir = os.path.join(get_app_dir(), "captures")
        # self.record_dir = os.path.join(get_app_dir(), "videos")


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

        self.btn_capture = QPushButton("📸 Capture Snapshot")
        self.btn_capture.clicked.connect(self.take_snapshot)
        self.btn_capture.setShortcut("Space")
        self.btn_capture.setToolTip("Capture current frame to captures/ folder (Shortcut: Space)")
        self.btn_capture.setStyleSheet("font-weight: bold; background-color: #1976d2; color: white; padding: 8px;")
        left_panel.addWidget(self.btn_capture)

        stats_group = QGroupBox("Diagnostic Telemetry")
        stats_layout = QVBoxLayout()
        self.lbl_in_fps = QLabel("Input Stream Rate: 0 FPS")
        self.lbl_proc_fps = QLabel("Processed Rate: 0 FPS")
        self.lbl_proc_time = QLabel("Processing Overhead: 0.0 ms")
        self.lbl_bitrate = QLabel("Network Bitrate: 0.00 Mbps")
        self.lbl_dropped = QLabel("Dropped Frames: 0")
        self.lbl_rec_status = QLabel("Recording: Inactive")
        self.lbl_capture_status = QLabel("Last Capture: None")
        stats_layout.addWidget(self.lbl_in_fps)
        stats_layout.addWidget(self.lbl_proc_fps)
        stats_layout.addWidget(self.lbl_proc_time)
        stats_layout.addWidget(self.lbl_bitrate)
        stats_layout.addWidget(self.lbl_dropped)
        stats_layout.addWidget(self.lbl_rec_status)
        stats_layout.addWidget(self.lbl_capture_status)
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

        # Dynamically toggle recording while pipeline is actively running
        if self.processing_active:
            rec_on = self.proc_config["record_enabled"]
            rec_mode = self.proc_config["record_mode"]
            if self.input_thread:
                self.input_thread.set_recording(rec_on and rec_mode == "Raw Original")
            if self.output_thread:
                self.output_thread.set_recording(rec_on and rec_mode == "Processed", rec_mode)

    def handle_thread_stats(self, stat_type, value):
        self.stats_updated.emit(stat_type, value)

    def update_stats_label(self, stat_type, value):
        if stat_type == "input_fps":
            self.lbl_in_fps.setText(f"Input Stream Rate: {value}")
        elif stat_type == "proc_fps":
            self.lbl_proc_fps.setText(f"Processed Rate: {value}")
        elif stat_type == "bitrate":
            self.lbl_bitrate.setText(f"Network Bitrate: {value}")
        elif stat_type == "proc_time":
            self.lbl_proc_time.setText(f"Processing Overhead: {value}")
        elif stat_type == "dropped":
            self.lbl_dropped.setText(f"Dropped Frames: {value}")
        elif stat_type == "rec_status":
            self.lbl_rec_status.setText(value)
        elif stat_type == "capture_status":
            self.lbl_capture_status.setText(f"Last Capture: {value}")

    def update_preview_window(self, frame):
        if frame is None or frame.size == 0:
            return

        self.latest_frame = frame

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

    def take_snapshot(self):
        """Request an instant snapshot directly from the video processing thread with 0ms latency."""
        if not self.processing_active or self.process_thread is None:
            self.lbl_capture_status.setText("Last Capture: Stream inactive")
            print("Capture failed: video stream is not running.")
            return

        self.process_thread.request_snapshot(self.capture_dir, self.handle_thread_stats)

    def stop_pipelines(self):
        """Signal every worker to stop and wait for them, so a restart never runs two
        producers against the same queues."""
        self.latest_frame = None
        self.lbl_rec_status.setText("Recording: Inactive")
        self.lbl_in_fps.setText("Input Stream Rate: 0 FPS")
        self.lbl_proc_fps.setText("Processed Rate: 0 FPS")
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
            try:
                while self.input_queue.get(0.01) is not None: pass
            except Exception as e:
                print("Error: ", e)
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
