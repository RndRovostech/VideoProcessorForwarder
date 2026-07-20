# Changelog & Roadmap

Working record for `CameraProcessForwarder.py` — what changed, what improved, and what is worth doing next.

- [Version history](#version-history)
- [Changes made — 20 Jul 2026](#changes-made--20-jul-2026)
- [Measured improvements](#measured-improvements)
- [Future improvements](#future-improvements)
- [Things already tried that did not work](#things-already-tried-that-did-not-work)
- [Notes to self](#notes-to-self)

---

## Version history

| Version | Notes |
|---|---|
| `old/main.py` | First attempt. Basic capture and display. |
| `old/main2.py` | Added GStreamer, threading, GUI. GStreamer path hardcoded to `D:\gstreamer`. |
| `CameraProcessForwarder.py` | Current — was `main3.py`. Auto-detects GStreamer, cleaner source handling. **Reviewed and repaired 20 Jul 2026.** |

**Housekeeping, 20 Jul 2026:** `main3.py` renamed to `CameraProcessForwarder.py`; earlier
versions moved into `old/`; leftover `ROV_Record_*.mp4` test files deleted. All documentation
references updated.

---

## Changes made — 20 Jul 2026

### Fixed — stream-breaking

| # | Problem | Fix |
|---|---|---|
| 1 | FFmpeg's `stderr` pipe was never read. Once the 64 KB buffer filled, the encoder froze permanently. | All child pipes drained by a background thread, which also keeps the last lines for error reporting. |
| 2 | UDP bridge port hardcoded to `5001`. Any source port except 5000 received nothing. | Derived from the configured port. |
| 3 | GStreamer caps missing `media`, `clock-rate`, `encoding-name` — the depayloader could not negotiate. | Full caps string restored (as `main2.py` had). |
| 4 | Frame size hardcoded to 1920×1080. With *Resize* off and a 720p source, every frame desynced. | Geometry locks to the first frame; later sizes rescaled. |
| 5 | Stream encoded as yuv444p, which QGroundControl refuses to play. | Forced `yuv420p`. |

### Fixed — silent data loss

| # | Problem | Fix |
|---|---|---|
| 6 | "Raw Original" recording produced **0-byte files** (see `ROV_Record_1784251954.mp4`). Frames were only written in *Processed* mode. | Raw archiving moved into the capture thread, the only stage holding unfiltered frames. |
| 7 | `terminate()` fired immediately after closing FFmpeg's input, truncating the MP4 before its index was written. | Shutdown waits for FFmpeg to finish. |
| 8 | Recordings played at the wrong speed — 6 s of capture became a 3.3 s file. | Arrival-time timestamps + variable-rate muxing. Verified 6.04 s for 6 s. |

### Fixed — wrong telemetry

| # | Problem | Fix |
|---|---|---|
| 9 | "Network Bitrate" showed ~694 Mbps instead of 7 Mbps — it measured raw frames entering the pipe, not encoded output. | Read from FFmpeg's own progress output. |
| 10 | "Dropped Frames" was permanently 0. `FrameQueue.put()` evicted the oldest frame and still returned success — but the eviction *is* the drop. | Counted across both queues. |
| 11 | The "frames repeated" warning I added was itself wrong — it checked the current loop iteration, but the loop runs several times between sends. A perfect 30.1 FPS source still triggered it. | Rewritten to measure arrival rate directly. |

### Fixed — latency (the main issue)

| # | Problem | Fix |
|---|---|---|
| 12 | **Delay grew over time.** FFmpeg stamped frames 1/30 s apart regardless of arrival, so a slow pipeline made the stream clock run behind real time — +16.5% of elapsed time at 25 FPS, unbounded. | Output paced to a true 30 FPS: repeat the newest frame when starved, send only the freshest when fast. |
| 13 | **Constant ~0.5 s delay.** `cv2.VideoCapture` buffers ~0.6 s on network sources and ignores FFmpeg's low-latency flags, including via `OPENCV_FFMPEG_CAPTURE_OPTIONS`. | New `FFmpegFrameReader` drives FFmpeg directly. Local files still use OpenCV (needs seeking). |
| 14 | FFmpeg duplicated every frame (`dup=713`) — a small `probesize` stops it inferring the frame rate, so it padded to a guessed CFR. *(Introduced by fix 13, found by measurement.)* | Added `-fps_mode passthrough`. |

### Fixed — robustness

- Threads are daemons and are joined on stop. Previously a restart could leave two producers
  on one queue, and a dead RTSP source could stop the app closing.
- Port validation — an invalid port used to crash.
- Missing `cv2.xphoto` degrades to a warning instead of killing the GUI.
- Preview throttled to 15 FPS — the Qt event queue could previously grow without bound.
- GStreamer discovery now checks the installer's environment variable and drives C:–G:.
  **My install is on `D:\`**, which the original C:-only list would have missed had it not
  also been on `PATH`.
- Keyframe every second (was every 250 frames ≈ 8 s), so QGC recovers quickly after packet loss.

### Improved — efficiency

- Removed `.tobytes()` from the frame path: **187 MB/s of pointless copying**, 0.88 ms per frame.
  FFmpeg accepts the array buffer directly.
- Removed ~100% frame duplication in the decoder (fix 14).
- Preview cost halved by throttling.

### Improved — structure

- `VideoInputThread.run()` was ~130 lines doing five jobs → split into `open_source()`,
  `launch_gst_bridge()`, `read_frame()`, `capture_loop()`.
- `VideoOutputThread.run()` → split into `start_stream()`, `send()`, `report_source_rate()`.
- **Added `apply_filters()` as a single, documented extension point** so others can add their
  own image processing without touching the threading code.
- Removed dead code (`cap_api` for RTSP, unused variables).
- Moved design rationale from inline comment blocks into docstrings.
- Extracted `fit_to()` for resize logic repeated in three places.

---

## Measured improvements

| Metric | Before | After |
|---|---|---|
| End-to-end latency | ~630 ms | **~128 ms** |
| Ingest latency | 611 ms | **109 ms** |
| Clock drift at 25 FPS | +16.5% of elapsed, unbounded | **−0.1%** |
| Memory copying | 187 MB/s | **0** |
| Frames decoded per 30 FPS source | ~60 (half duplicates) | **30.4** |
| Longest function | ~130 lines | **62 lines** |

**Latency breakdown now:** ingest 109 ms · queues 0.1 ms · filters 1.0 ms · pacer 13.4 ms ·
encode 4.0 ms = **~128 ms total**.

---

## Future improvements

### High value

**1. GPU filters via `cv2.UMat`.**
CLAHE is the most expensive stage by far. Wrapping frames in `cv2.UMat` runs resize, CLAHE and
white balance on the GPU through OpenCL. Biggest remaining CPU saving, and it would give
people much more headroom for their own algorithms.

**2. Wire up the `Dec:` box, or remove it.**
It currently does nothing — decoding runs on the CPU inside FFmpeg. Either pass
`-hwaccel cuda` / `-hwaccel qsv` to `FFmpegFrameReader`, or take the control out. A dead
control is misleading mid-dive.

**3. Configurable destination address.**
Fixed at `127.0.0.1`, so QGC must run on the same machine. Add a host field next to the port.

### Medium value

**4. Bundle each child process with its error log.**
The `(process, error_tail)` pairing repeats four times. A small wrapper class would remove the
duplication. *Deliberately skipped in the July pass — it touches many call sites and the
priority was keeping verified behaviour intact.*

**5. Try dropping GStreamer entirely.**
FFmpeg can read RTP directly given an SDP file. That would remove a whole process and a whole
dependency. Risk: needs SPS/PPS in-band, which depends on the camera. Test with the real ROV
camera before committing.

**6. Selectable output frame rate.**
`DEFAULT_OUTPUT_FPS` is a constant. A 60 FPS camera is currently sent at 30.

**7. Save the settings.**
Source, ports and filter choices reset every launch. A small JSON config would help.

### Lower value / nice to have

**8.** Multiple cameras at once.
**9.** Record and stream at different resolutions.
**10.** Snapshot button — save a single frame as PNG.

---

## Things already tried that did not work

Keep this list. These all look like obvious fixes and are not.

| Idea | Result |
|---|---|
| Wallclock timestamps on the **stream** | **838 ms worse.** Correct for recordings, wrong for live RTP. |
| `OPENCV_FFMPEG_CAPTURE_OPTIONS` for low latency | **No effect at all** — 600.1 ms with, 600.1 ms without. OpenCV discards them. |
| Blaming MPEG-TS remuxing for ingest delay | Removing the container entirely still left 537 ms. |
| `-threads 1` / `thread_type slice` on the decoder | No measurable difference. |
| Short GOP and `muxdelay` for latency | No local effect. Kept anyway for packet-loss recovery, but they were not the fix. |
| Inferring source FPS from repeated sends | Unreliable — two unsynchronised 30 Hz clocks beat, making a healthy 30 FPS feed read as 24. Count arrivals instead. |

---

## Notes to self

- **Test with `Video_File` first.** It loops, is repeatable, and needs no hardware.
- **The test scripts are worth rebuilding if lost.** Five harnesses were used: file+raw
  recording, processed recording at 1080p, UDP bridge with a real RTP sender, media-clock
  drift via captured RTP timestamps, and ingest latency via a black↔white flip test. The flip
  test is the one that found the 611 ms — a visual transition survives H.264 compression
  intact, so it measures true end-to-end delay.
- **Measure, do not assume.** Every wrong turn in the table above looked correct on paper.
- **If QGC video looks late again:** a *constant* delay is QGC's own buffer (100–200 ms,
  outside our control). A delay that *grows* means the media-clock fault is back — check the
  output pacer first.
- The 0-byte and tiny `ROV_Record_*.mp4` files in the project folder are from the old raw
  recording bug. Safe to delete.
