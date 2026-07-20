"""Inline SVG diagrams for the report, drawn in the document's own palette."""

INK   = "#14161F"
GRAPH = "#22263A"
SLATE = "#3F4668"
STEEL = "#5C6178"
LINE  = "#DDDEE4"
SOFT  = "#F7F7F9"
LIGHT = "#9AA0C4"
FONT  = "Calibri, 'Segoe UI', Arial, sans-serif"

# ---------------------------------------------------------------- pipeline ---
def pipeline():
    W, H = 1000, 300
    bw, bh, by = 230, 92, 74           # stage box
    xs = [60, 390, 720]
    names = ["1  CAPTURE", "2  PROCESS", "3  SEND"]
    cls   = ["VideoInputThread", "VideoProcessingThread", "VideoOutputThread"]
    subs  = [["UDP H.264 / RTSP / file", "decode to BGR frames"],
             ["resize, white balance,", "CLAHE, telemetry overlay"],
             ["pace to 30 FPS,", "encode H.264, send RTP"]]
    unders = ["raw recording", "preview to screen (15 FPS)", "processed recording"]

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="{FONT}" role="img" aria-label="Three-stage pipeline diagram">']
    p.append(f'<rect width="{W}" height="{H}" fill="#fff"/>')

    # end labels
    p.append(f'<text x="30" y="46" font-size="15" font-weight="700" fill="{STEEL}" '
             f'letter-spacing="1.5">CAMERA</text>')
    p.append(f'<text x="{W-30}" y="46" font-size="15" font-weight="700" fill="{STEEL}" '
             f'letter-spacing="1.5" text-anchor="end">QGROUNDCONTROL</text>')

    for i, x in enumerate(xs):
        p.append(f'<rect x="{x}" y="{by}" width="{bw}" height="{bh}" rx="9" '
                 f'fill="{SOFT}" stroke="{LINE}"/>')
        p.append(f'<rect x="{x}" y="{by}" width="{bw}" height="27" rx="9" fill="{GRAPH}"/>')
        p.append(f'<rect x="{x}" y="{by+18}" width="{bw}" height="9" fill="{GRAPH}"/>')
        p.append(f'<text x="{x+13}" y="{by+19}" font-size="13" font-weight="700" '
                 f'fill="#fff" letter-spacing="1.2">{names[i]}</text>')
        p.append(f'<text x="{x+13}" y="{by+46}" font-size="12" font-weight="700" '
                 f'fill="{GRAPH}">{cls[i]}</text>')
        for j, s in enumerate(subs[i]):
            p.append(f'<text x="{x+13}" y="{by+64+j*15}" font-size="11.5" fill="{STEEL}">{s}</text>')

        # side output beneath each stage
        p.append(f'<path d="M{x+bw/2} {by+bh} L{x+bw/2} {by+bh+30}" stroke="{LINE}" '
                 f'stroke-width="1.5" stroke-dasharray="3 3"/>')
        p.append(f'<text x="{x+bw/2}" y="{by+bh+46}" font-size="11" fill="{STEEL}" '
                 f'text-anchor="middle">{unders[i]}</text>')

    # queues between stages
    for x in (xs[0]+bw, xs[1]+bw):
        mx = x + (375-260)/2
        p.append(f'<path d="M{x+8} {by+bh/2} L{x+100} {by+bh/2}" stroke="{SLATE}" '
                 f'stroke-width="2" marker-end="url(#ar)"/>')
        p.append(f'<rect x="{x+14}" y="{by+bh/2-26}" width="72" height="20" rx="4" '
                 f'fill="#fff" stroke="{LINE}"/>')
        p.append(f'<text x="{x+50}" y="{by+bh/2-12}" font-size="10.5" fill="{GRAPH}" '
                 f'text-anchor="middle" font-weight="700">queue: 3</text>')

    # feed arrows in/out
    p.append(f'<path d="M{W-30} {by-6} L{W-30} 56" stroke="{SLATE}" stroke-width="2" '
             f'marker-end="url(#arU)"/>')
    p.append(f'<path d="M{xs[2]+bw} {by+bh/2} L{W-30} {by+bh/2} L{W-30} {by-6}" fill="none" '
             f'stroke="{SLATE}" stroke-width="2"/>')
    p.append(f'<path d="M30 56 L30 {by+bh/2} L{xs[0]} {by+bh/2}" fill="none" '
             f'stroke="{SLATE}" stroke-width="2" marker-end="url(#ar)"/>')

    # caption strip
    p.append(f'<rect x="30" y="252" width="{W-60}" height="30" rx="5" fill="{SOFT}" stroke="{LINE}"/>')
    p.append(f'<text x="46" y="272" font-size="11.5" fill="{GRAPH}">'
             f'<tspan font-weight="700">Queues hold 3 frames and discard the oldest when full</tspan>'
             f'  —  under load the pipeline drops frames instead of building up delay.</text>')

    p.append(f'<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
             f'markerHeight="6" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{SLATE}"/></marker>'
             f'<marker id="arU" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
             f'markerHeight="6" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{SLATE}"/></marker></defs>')
    p.append('</svg>')
    return "\n".join(p)


# ------------------------------------------------------------ latency bar ---
def latency():
    stages = [("Ingest (bridge + decode)", 109.0), ("Pacer hold", 13.4),
              ("Encode + mux", 4.0), ("Filters", 1.0), ("Queues", 0.1)]
    total = sum(v for _, v in stages)
    W, H = 980, 210
    x0, x1 = 30, W - 30
    bw = x1 - x0
    fills = [GRAPH, SLATE, STEEL, "#8A90A8", LIGHT]

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="{FONT}" role="img" aria-label="Latency budget bar chart">']
    p.append(f'<rect width="{W}" height="{H}" fill="#fff"/>')
    p.append(f'<text x="{x0}" y="26" font-size="13" font-weight="700" fill="{GRAPH}">'
             f'Camera frame → RTP packet on the wire</text>')
    p.append(f'<text x="{x1}" y="26" font-size="13" font-weight="700" fill="{GRAPH}" '
             f'text-anchor="end">{total:.1f} ms total</text>')

    x = x0
    for i, (name, v) in enumerate(stages):
        w = bw * v / total
        p.append(f'<rect x="{x:.1f}" y="40" width="{w:.1f}" height="42" fill="{fills[i]}"/>')
        if w > 60:
            p.append(f'<text x="{x+w/2:.1f}" y="66" font-size="12" font-weight="700" fill="#fff" '
                     f'text-anchor="middle">{v:.0f} ms</text>')
        x += w
    p.append(f'<rect x="{x0}" y="40" width="{bw}" height="42" fill="none" stroke="{LINE}"/>')

    # legend
    for i, (name, v) in enumerate(stages):
        col, row = divmod(i, 3)
        lx = x0 + col * 330
        ly = 108 + row * 24
        p.append(f'<rect x="{lx}" y="{ly-10}" width="11" height="11" rx="2" fill="{fills[i]}"/>')
        p.append(f'<text x="{lx+18}" y="{ly}" font-size="11.5" fill="{GRAPH}">{name}</text>')
        p.append(f'<text x="{lx+300}" y="{ly}" font-size="11.5" fill="{STEEL}" '
                 f'text-anchor="end">{v:.1f} ms</text>')

    p.append(f'<rect x="{x0}" y="168" width="{bw}" height="30" rx="5" fill="{SOFT}" stroke="{LINE}"/>')
    p.append(f'<text x="{x0+16}" y="188" font-size="11.5" fill="{GRAPH}">'
             f'<tspan font-weight="700">Ingest dominates</tspan> — mostly the inherent cost of '
             f'receiving and decoding compressed video. It was 611 ms before this work.</text>')
    p.append('</svg>')
    return "\n".join(p)


if __name__ == "__main__":
    import os
    d = os.path.dirname(os.path.abspath(__file__))
    open(os.path.join(d, "fig_pipeline.svg"), "w", encoding="utf-8").write(pipeline())
    open(os.path.join(d, "fig_latency.svg"), "w", encoding="utf-8").write(latency())
    print("wrote fig_pipeline.svg, fig_latency.svg")
