# --- Thread Safe Frame Buffer ---
from multiprocessing import Queue
from queue import Empty, Full

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