"""Small server-side SVG line charts, so the page needs no JavaScript or CDN.

Hovering a chart shows that day's value: each point is a <g class="pt"> with
an invisible full-height hit strip, a guide line and a readout in the strip
above the plot. style.css reveals them on :hover (a tap on phones).
"""

from __future__ import annotations

from markupsafe import Markup, escape


def line_chart(
    points: list[tuple[str, float]],
    y_min: float = 0,
    y_max: float = 100,
    suffix: str = "",
    width: int = 640,
    height: int = 196,
    step: bool = False,
    label: str = "",
) -> Markup:
    if not points:
        return Markup('<p class="muted">No history yet.</p>')
    pad_l, pad_r, pad_t, pad_b = 36, 12, 26, 24
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    span = (y_max - y_min) or 1

    def x(i: int) -> float:
        return pad_l + (inner_w * i / (len(points) - 1) if len(points) > 1 else inner_w / 2)

    def y(v: float) -> float:
        v = min(max(v, y_min), y_max)
        return pad_t + inner_h * (1 - (v - y_min) / span)

    coords: list[str] = []
    for i, (_, v) in enumerate(points):
        if step and coords:
            coords.append(f"{x(i):.1f},{y(points[i - 1][1]):.1f}")
        coords.append(f"{x(i):.1f},{y(v):.1f}")

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img"'
        f' aria-label="{escape(label)}">'
    ]
    for frac in (0, 0.5, 1):
        gv = y_min + span * frac
        gy = y(gv)
        parts.append(
            f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" y1="{gy:.1f}" y2="{gy:.1f}"/>'
            f'<text class="axis" x="{pad_l - 6}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{gv:g}{escape(suffix)}</text>"
        )
    parts.append(f'<polyline class="line" points="{" ".join(coords)}"/>')
    n = len(points)
    col = inner_w / (n - 1) if n > 1 else inner_w
    for i, (day, v) in enumerate(points):
        cx, cy = x(i), y(v)
        hit_l = max(pad_l, cx - col / 2) if n > 1 else pad_l
        hit_r = min(width - pad_r, cx + col / 2) if n > 1 else width - pad_r
        text = f"{day} · {v:g}{suffix}"
        half = len(text) * 3.7  # ~7.4px per 12px monospace character
        tx = min(max(cx, half + 2), width - half - 2)
        parts.append(
            f'<g class="pt">'
            f'<rect class="hit" x="{hit_l:.1f}" y="{pad_t}" width="{hit_r - hit_l:.1f}"'
            f' height="{inner_h}"/>'
            f'<line class="guide" x1="{cx:.1f}" x2="{cx:.1f}" y1="{pad_t}"'
            f' y2="{pad_t + inner_h}"/>'
            f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="3">'
            f"<title>{escape(day)}: {v:g}{escape(suffix)}</title></circle>"
            f'<text class="tip" x="{tx:.1f}" y="{pad_t - 9}" text-anchor="middle">'
            f"{escape(text)}</text></g>"
        )
    first, last = points[0][0], points[-1][0]
    parts.append(f'<text class="axis" x="{pad_l}" y="{height - 6}">{escape(first)}</text>')
    if last != first:
        parts.append(
            f'<text class="axis" x="{width - pad_r}" y="{height - 6}" text-anchor="end">'
            f"{escape(last)}</text>"
        )
    parts.append("</svg>")
    return Markup("".join(parts))
