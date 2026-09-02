import threading
import time
import os
import cv2

PREVIEW_FPS = 10  # GUI preview throttled to this many frames per second
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
        self.snapshot_request = None

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

    def request_snapshot(self, target_dir, callback=None, depth=None):
        """Schedule an instant snapshot capture on the very next processed frame with 0ms latency."""
        self.snapshot_request = (target_dir, callback, depth)

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
        # 1. Apply enhancements on native camera resolution (2.25x faster than 1080p)
        if self.config["white_balance"] and self.wb is not None:
            frame = self.wb.balanceWhite(frame)

        if self.config["clahe"]:
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = self.clahe.apply(lab[:, :, 0])  # in-place L channel, no split/merge overhead
            frame = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        # 2. Resize to standard 1080p output at the end
        if self.config["resize"]:
            frame = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)

        # --- your own stages go here ---

        return frame

    def run(self):
        last_dropped = -1
        last_preview = 0.0
        last_proc_stats = time.time()
        proc_frame_count = 0
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

            proc_frame_count += 1
            now_time = time.time()
            if now_time - last_proc_stats >= 1.0:
                self.stats_callback("proc_fps", f"{proc_frame_count / (now_time - last_proc_stats):.1f} FPS")
                proc_frame_count = 0
                last_proc_stats = now_time

            # Capture snapshot immediately on this exact live frame (0ms latency, clean frame)
            if self.snapshot_request is not None:
                target_dir, cb, depth_val = self.snapshot_request
                self.snapshot_request = None
                snap_frame = frame.copy()

                def _save_snap(img, sdir, callback, depth):
                    try:
                        os.makedirs(sdir, exist_ok=True)
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        ms = int((time.time() % 1) * 1000)
                        depth_str = f"_depth_{depth:.2f}m" if depth is not None else ""
                        fname = f"ROV_Capture_{ts}_{ms:03d}{depth_str}.jpg"
                        fpath = os.path.join(sdir, fname)
                        if cv2.imwrite(fpath, img, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                            print(f"Snapshot saved: {fpath}")
                            if callback:
                                callback("capture_status", fname)
                        else:
                            print(f"Failed to write snapshot: {fpath}")
                            if callback:
                                callback("capture_status", "Save failed")
                    except Exception as ex:
                        print(f"Error saving snapshot: {ex}")
                        if callback:
                            callback("capture_status", "Error saving")

                threading.Thread(target=_save_snap, args=(snap_frame, target_dir, cb, depth_val), daemon=True).start()

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