import sys
import cv2
import numpy as np
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton
from PyQt5.QtCore import QTimer


# Note: Requires opencv-contrib-python
class ROVRelay4K(QWidget):
    def __init__(self):
        super().__init__()
        # Initialize the White Balance algorithm
        self.initUI()
        self.timer = QTimer()
        self.timer.timeout.connect(self.process_frame)

    def initUI(self):
        self.setWindowTitle("Rovostech 4K/1080p Processor")
        layout = QVBoxLayout()
        self.source_type = QComboBox()
        self.source_type.addItems(["Video_File", "RTSP", "UDP (H.264)"])
        layout.addWidget(QLabel("Source Type:"))
        layout.addWidget(self.source_type)
        self.source_input = QLineEdit("./GateC_SmallCannal.mkv")
        layout.addWidget(self.source_input)
        self.dest_port = QLineEdit("5600")
        layout.addWidget(self.dest_port)
        self.btn_start = QPushButton("Start Stream")
        self.btn_start.clicked.connect(self.toggle_stream)
        layout.addWidget(self.btn_start)
        self.status = QLabel("Status: Idle")
        layout.addWidget(self.status)
        self.setLayout(layout)

    def get_pipelines(self):
        src = self.source_input.text()
        port = self.dest_port.text()
        stype = self.source_type.currentText()
        # Output Pipeline: Using Intel QuickSync for hardware acceleration
        # Adding mpegtsmux makes the stream recognizable to QGC immediately
        # Added caps to appsrc so the encoder knows exactly what's coming from OpenCV
        # We MUST define the width/height/fps in appsrc to match your cv2.resize
        out_p = (f"appsrc ! video/x-raw,format=BGR,width=1920,height=1080,framerate=30/1 ! "
                 f"videoconvert ! "
                 f"nvh264enc bitrate=15000 preset=low-latency-hq ! "
                 f"rtph264pay config-interval=1 pt=96 ! "
                 f"udpsink host=127.0.0.1 port={port} sync=false")
        
        if stype == "RTSP":
            in_p = f"rtspsrc location={src} latency=0 ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! appsink"
        elif stype == "UDP (H.264)":
            in_p = f"udpsrc port={src} ! application/x-rtp,payload=96 ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! appsink"
        else:
            in_p = src # Direct file path
        return in_p, out_p

    def toggle_stream(self):
        if self.timer.isActive():
            self.timer.stop()
            if hasattr(self, 'cap'): self.cap.release()
            if hasattr(self, 'out'): self.out.release()
            cv2.destroyAllWindows()
            self.btn_start.setText("Start Stream")
        else:
            in_p, out_p = self.get_pipelines()
            # If it's a file, we use standard backend; if it's a string pipe, we use GST
            self.cap = cv2.VideoCapture(in_p, cv2.CAP_GSTREAMER if self.source_type.currentText() != "Video_File" else cv2.CAP_ANY)
            # Target resolution for QGC (1080p is safer for real-time 11th gen i5)
            self.out = cv2.VideoWriter(out_p, cv2.CAP_GSTREAMER, 0, 30, (1920, 1080))
            if self.cap.isOpened():
                self.timer.start(1)
                self.btn_start.setText("Stop Stream")
                self.status.setText("Status: Streaming...")

    def process_frame(self):
        ret, frame = self.cap.read()
        if not ret: return
        # 1. Resize to target output resolution (1080p)
        frame = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)
        # 2. Apply OpenCV C++ White Balance
        # This automatically handles the gain calculation and application
        # 3. Preview and Send to QGC
        cv2.imshow("Frame Stream", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            self.toggle_stream()
            return
        self.out.write(frame)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    ex = ROVRelay4K()
    ex.show()
    sys.exit(app.exec_())