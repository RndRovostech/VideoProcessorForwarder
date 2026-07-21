"""
Generate images/AppIcon.ico from the artwork described in images/AppIcon.svg.

    python images/make_icon.py

Uses only OpenCV and NumPy -- already dependencies of the application -- so the
icon can be rebuilt on any machine that can run the app, with no extra install
and no SVG rasteriser.

The artwork is drawn once at 8x and downsampled with INTER_AREA for each size in
the .ico. Supersampling rather than cv2's LINE_AA is what keeps the 16 px and
20 px entries clean: antialiased primitives drawn directly at 16 px lose the ring
entirely.

Windows picks an entry by size -- 16/20/24/32 for the title bar, taskbar and
Explorer lists, 48/64 for medium icons, 256 for the large view and the file
dialog preview. Dropping the small entries makes Windows downscale 256 itself,
which looks noticeably worse.
"""

import os
import struct

import cv2
import numpy as np

# BGRA, because that is the order cv2 works in. Values match images/AppIcon.svg.
GROUND = (0x1F, 0x16, 0x14, 255)   # #14161F graphite
RING   = (0xC4, 0xA0, 0x9A, 255)   # #9AA0C4 pale slate
CYAN   = (0xDE, 0xA6, 0x2B, 255)   # #2BA6DE

BASE = 256          # design canvas, matching the SVG viewBox
SS = 8              # supersample factor
SIZES = [16, 20, 24, 32, 48, 64, 128, 256]

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "AppIcon.ico")


def draw_master():
    """Draw the icon at BASE*SS and return it as a BGRA array."""
    n = BASE * SS
    img = np.zeros((n, n, 4), dtype=np.uint8)

    def s(v):
        return int(round(v * SS))

    # --- rounded-square ground, built as a mask so the corners stay transparent
    mask = np.zeros((n, n), dtype=np.uint8)
    r = s(56)
    cv2.rectangle(mask, (r, 0), (n - r, n), 255, -1)
    cv2.rectangle(mask, (0, r), (n, n - r), 255, -1)
    for cx, cy in ((r, r), (n - r, r), (r, n - r), (n - r, n - r)):
        cv2.circle(mask, (cx, cy), r, 255, -1)
    img[mask > 0] = GROUND

    # --- lens ring and pupil
    cv2.circle(img, (s(99), s(128)), s(52), RING, s(17))
    cv2.circle(img, (s(99), s(128)), s(22), CYAN, -1)

    # --- forward chevron. cv2 has no round line caps, so the polyline is drawn
    #     square and a disc is stamped at each vertex to round the ends and joint.
    pts = np.array([[s(171), s(92)], [s(207), s(128)], [s(171), s(164)]], np.int32)
    cv2.polylines(img, [pts], False, CYAN, s(21))
    for p in pts:
        cv2.circle(img, tuple(p), s(21) // 2, CYAN, -1)

    return img


def write_ico(master, path):
    """Downsample master to each size and pack the results into a PNG-based .ico."""
    pngs = []
    for size in SIZES:
        resized = cv2.resize(master, (size, size), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", resized)
        if not ok:
            raise RuntimeError("PNG encode failed at %dpx" % size)
        pngs.append(buf.tobytes())

    header = struct.pack("<HHH", 0, 1, len(SIZES))
    offset = len(header) + 16 * len(SIZES)

    entries = b""
    for size, png in zip(SIZES, pngs):
        # 256 is stored as 0 -- the field is a single byte, so 256 does not fit.
        dim = 0 if size == 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)

    with open(path, "wb") as fh:
        fh.write(header + entries + b"".join(pngs))

    return sum(len(p) for p in pngs)


if __name__ == "__main__":
    total = write_ico(draw_master(), OUT)
    print("wrote %s" % OUT)
    print("  %d entries: %s" % (len(SIZES), ", ".join("%dpx" % s for s in SIZES)))
    print("  %.1f KB" % (os.path.getsize(OUT) / 1024.0))
