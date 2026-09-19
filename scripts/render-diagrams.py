#!/usr/bin/env python3
"""Render docs/diagrams/*.excalidraw to PNG, so the pictures cannot drift from the file.

The PNGs beside these sources were exported by hand from the Excalidraw app. That is
fine once and a liability forever: nothing checks that the picture still describes the
schema, and the first time a diagram is edited without re-exporting, the repository
starts shipping a confident illustration of a system that no longer exists. The
.excalidraw file is the source; this makes the PNG a build artifact of it.

Deliberately small. It draws the element types these diagrams actually use —
rectangle, ellipse, diamond, line, arrow, text — with Pillow and the system fonts,
because the alternative was a headless Chromium to reproduce a hand-drawn stroke style
that carries no information. What matters is that the boxes, the labels and the arrows
are the ones in the file.

    python scripts/render-diagrams.py            # all of them
    python scripts/render-diagrams.py data-model
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DIAGRAMS = ROOT / "docs" / "diagrams"

SCALE = 2           # render at 2x and keep it; these are read zoomed-in
MARGIN = 40
BG = "#ffffff"

# Excalidraw's three font families, mapped to what macOS and most Linux boxes have.
# 1 is its hand-drawn face; a normal sans is a closer match than anything else present
# and the words are what matter.
FONTS = {
    1: ["/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    2: ["/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    3: ["/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
}
_cache: dict[tuple[int, int], ImageFont.FreeTypeFont] = {}


def font_for(family: int, size: int) -> ImageFont.FreeTypeFont:
    key = (family, size)
    if key not in _cache:
        for path in FONTS.get(family, FONTS[2]):
            if Path(path).exists():
                _cache[key] = ImageFont.truetype(path, size)
                break
        else:
            _cache[key] = ImageFont.load_default()
    return _cache[key]


def blend(color: str, opacity: int) -> str:
    """Excalidraw opacity is a percentage on a white page; flatten it rather than
    carrying an alpha channel through every draw call."""
    if not color or color == "transparent":
        return None
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None
    a = max(0, min(100, opacity)) / 100
    r2, g2, b2 = (round(255 + (v - 255) * a) for v in (r, g, b))
    return f"#{r2:02x}{g2:02x}{b2:02x}"


def bounds(elements: list[dict]) -> tuple[float, float, float, float]:
    xs, ys, xe, ye = [], [], [], []
    for e in elements:
        if e.get("isDeleted"):
            continue
        x, y = e["x"], e["y"]
        w, h = e.get("width", 0) or 0, e.get("height", 0) or 0
        if e["type"] in ("line", "arrow") and e.get("points"):
            px = [p[0] for p in e["points"]]
            py = [p[1] for p in e["points"]]
            xs.append(x + min(px)); xe.append(x + max(px))
            ys.append(y + min(py)); ye.append(y + max(py))
            continue
        xs.append(x); xe.append(x + w)
        ys.append(y); ye.append(y + h)
    return min(xs), min(ys), max(xe), max(ye)


def arrowhead(draw, x1, y1, x2, y2, color, width):
    angle = math.atan2(y2 - y1, x2 - x1)
    size = 10 * SCALE
    for spread in (2.6, -2.6):
        draw.line(
            [(x2, y2),
             (x2 + size * math.cos(angle + spread), y2 + size * math.sin(angle + spread))],
            fill=color, width=width,
        )


def render(path: Path) -> Path:
    data = json.loads(path.read_text())
    els = [e for e in data["elements"] if not e.get("isDeleted")]
    x0, y0, x1, y1 = bounds(els)
    W = int((x1 - x0) * SCALE) + MARGIN * 2
    H = int((y1 - y0) * SCALE) + MARGIN * 2
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    def X(v): return (v - x0) * SCALE + MARGIN
    def Y(v): return (v - y0) * SCALE + MARGIN

    # Shapes first, then text, so a label is never painted under the next box.
    for e in els:
        if e["type"] not in ("rectangle", "ellipse", "diamond", "line", "arrow"):
            continue
        op = e.get("opacity", 100)
        stroke = blend(e.get("strokeColor", "#1e1e1e"), 100)
        fill = (blend(e.get("backgroundColor"), op)
                if e.get("fillStyle") != "none" else None)
        w = max(1, int((e.get("strokeWidth", 1) or 1) * SCALE))
        ex, ey = X(e["x"]), Y(e["y"])
        ew, eh = (e.get("width", 0) or 0) * SCALE, (e.get("height", 0) or 0) * SCALE

        if e["type"] == "rectangle":
            r = 12 * SCALE if e.get("roundness") else 0
            draw.rounded_rectangle([ex, ey, ex + ew, ey + eh], radius=r,
                                   fill=fill, outline=stroke, width=w)
        elif e["type"] == "ellipse":
            draw.ellipse([ex, ey, ex + ew, ey + eh], fill=fill, outline=stroke, width=w)
        elif e["type"] == "diamond":
            draw.polygon([(ex + ew / 2, ey), (ex + ew, ey + eh / 2),
                          (ex + ew / 2, ey + eh), (ex, ey + eh / 2)],
                         fill=fill, outline=stroke)
        else:
            pts = [(X(e["x"] + p[0]), Y(e["y"] + p[1])) for p in (e.get("points") or [])]
            if len(pts) < 2:
                continue
            draw.line(pts, fill=stroke, width=w, joint="curve")
            if e["type"] == "arrow":
                arrowhead(draw, *pts[-2], *pts[-1], stroke, w)

    for e in els:
        if e["type"] != "text":
            continue
        f = font_for(e.get("fontFamily", 2), int(e.get("fontSize", 16) * SCALE))
        color = blend(e.get("strokeColor", "#1e1e1e"), e.get("opacity", 100))
        lh = e.get("lineHeight", 1.25)
        tx, ty = X(e["x"]), Y(e["y"])
        for i, line in enumerate(e.get("text", "").split("\n")):
            yy = ty + i * int(e.get("fontSize", 16) * SCALE * lh)
            xx = tx
            if e.get("textAlign") == "center" and e.get("width"):
                xx = tx + ((e["width"] * SCALE) - draw.textlength(line, font=f)) / 2
            draw.text((xx, yy), line, font=f, fill=color)

    out = path.with_suffix(".png")
    img.save(out, "PNG", optimize=True)
    return out


def main(argv: list[str]) -> int:
    wanted = argv[1:] or [p.stem for p in sorted(DIAGRAMS.glob("*.excalidraw"))]
    for name in wanted:
        src = DIAGRAMS / f"{name}.excalidraw"
        if not src.exists():
            print(f"  no such diagram: {name}")
            return 2
        out = render(src)
        kb = out.stat().st_size / 1024
        with Image.open(out) as im:
            print(f"  {out.relative_to(ROOT)}  {im.width}x{im.height}  {kb:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
