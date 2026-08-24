"""Draw a recording as the thing it actually is: time.

The vendor's own summary is a grid of cards. Shuffle them and you lose nothing, which
is the tell — the layout carries no information, so it displays rather than argues.

A conversation is not a grid. It has a beginning and an end, it moves, and things get
said at particular moments. So the picture is a ribbon along its own duration: tone
fills the band, and every commitment is pinned at the minute it was spoken. Where the
band darkens is where it got hard; a cluster of pins is where the work got decided.
You can read the shape before you read a word of it.

Two renderers over one layout. SVG goes straight into the console — live, themed,
clickable, no dependency. The same layout also exports to `.excalidraw` so you can open
it up and annotate it, which is the part that makes it yours rather than a report.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import Config
from .store import Store
from .summarize import summary_path

# Matches the diverging scale used by the trend chart, so a colour means the same
# thing everywhere in the product.
POLARITY = [
    (-0.5, "#a8412c", "#eb937a"),
    (-0.15, "#a4705f", "#cb9585"),
    (0.15, "#7f7563", "#a89d84"),
    (0.5, "#4a7f92", "#84b7ca"),
    (1.01, "#1f6f8b", "#63b5d3"),
]


def polarity_color(valence: float, dark: bool = False) -> str:
    for threshold, light_hex, dark_hex in POLARITY:
        if valence < threshold:
            return dark_hex if dark else light_hex
    return POLARITY[-1][2 if dark else 1]


def _hhmm(ms: float | None) -> str:
    if ms is None:
        return ""
    s = int(ms // 1000)
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:d}:{s % 60:02d}"


def _section(md: str, name: str) -> list[str]:
    """Bullet lines under a `## Name` heading, minus the 'None recorded.' filler."""
    m = re.search(rf"^##\s*{name}\s*$(.+?)(?=^##|\Z)", md or "", re.M | re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or line.lower().startswith("none recorded"):
            continue
        out.append(re.sub(r"\*\*(.+?)\*\*", r"\1", line))
    return out


def recording_story(cfg: Config, store: Store, rec_id: str) -> dict:
    """The model behind one recording's picture. Pure data — no geometry yet."""
    row = store.get(rec_id)
    if row is None:
        raise KeyError(rec_id)
    sent = store.sentiment_of(rec_id)
    md = summary_path(cfg, rec_id).read_text() if summary_path(cfg, rec_id).exists() else ""

    duration_ms = int((row["duration_s"] or 0) * 1000)
    segments = json.loads(sent["segments_json"]) if sent and sent["segments_json"] else []
    # Segments carry no timestamps of their own — they are equal slices of the
    # transcript — so they are laid out proportionally across the duration. That is an
    # approximation and the caption says so rather than implying false precision.
    n = max(len(segments), 1)
    for i, seg in enumerate(segments):
        seg["from_ms"] = duration_ms * i / n
        seg["to_ms"] = duration_ms * (i + 1) / n

    pins = [
        {
            "at_ms": a["at_ms"],
            "text": a["text"],
            "status": a["status"],
            "quote": a["quote"],
            "id": a["id"],
        }
        for a in store.actions(recording_id=rec_id)
        if a["at_ms"] is not None and a["status"] != "dropped" and a["at_ms"] <= duration_ms
    ]
    pins.sort(key=lambda p: p["at_ms"])
    # Anything you acted on outranks anything still merely proposed; within a tier,
    # earlier wins, so the labelled set still reads left to right as the conversation did.
    rank = {"done": 0, "in_progress": 1, "accepted": 2, "proposed": 3}
    for i, p in enumerate(sorted(pins, key=lambda p: (rank.get(p["status"], 9), p["at_ms"]))):
        p["labelled"] = i < MAX_LABELLED

    return {
        "kind": "recording",
        "id": rec_id,
        "title": row["filename"],
        "when": time.strftime("%A %d %B %Y, %H:%M", time.localtime(row["started_at"])),
        "duration_ms": duration_ms,
        "duration_min": round((row["duration_s"] or 0) / 60),
        "segments": segments,
        "pins": pins,
        "sentiment": (
            {
                "valence": sent["valence"],
                "energy": sent["energy"],
                "label": sent["label"],
                "confidence": sent["confidence"],
                "spread": sent["spread"],
                "drivers": [d for d in (sent["drivers"] or "").split("\n") if d],
            }
            if sent
            else None
        ),
        "decisions": _section(md, "Decisions")[:4],
        "questions": _section(md, "Open questions")[:4],
        "key_points": _section(md, "Key points")[:5],
    }


# --------------------------------------------------------------------------- layout

W = 1440
LEFT, RIGHT = 120, 1360
BAND_H = 58
HEADER_H = 116          # title, date, and the tone summary opposite it
LANE_H = 34

# A label on every pin is chaos and goes unread. On a busy conversation the stack also
# grows until it collides with the title — 18 commitments needed seven rows and ate the
# header. Label the ones that earned it, keep the rest as ticks, and say how many.
MAX_LABELLED = 8
MAX_LANES = 5


def _band_y(lanes_used: int) -> int:
    return HEADER_H + max(lanes_used, 1) * LANE_H + 24


def _height(lanes_used: int, body_lines: int) -> int:
    return _band_y(lanes_used) + BAND_H + 76 + body_lines * 19 + 64


def _x(ms: float, duration_ms: int) -> float:
    if duration_ms <= 0:
        return LEFT
    return LEFT + (ms / duration_ms) * (RIGHT - LEFT)


def _lanes(pins: list[dict], duration_ms: int, min_gap: float = 150.0) -> list[int]:
    """Stack pins into rows so their labels cannot overlap.

    A label is only useful if it is readable; three commitments in the same minute
    drawn on one line is three unreadable labels. Each pin takes the highest row whose
    last label ended far enough to the left.
    """
    lane_end: list[float] = []
    out = []
    for p in pins:
        x = _x(p["at_ms"], duration_ms)
        for lane, end in enumerate(lane_end):
            if x - end >= min_gap:
                lane_end[lane] = x
                out.append(lane)
                break
        else:
            lane_end.append(x)
            out.append(len(lane_end) - 1)
    return out


def _esc(t: str) -> str:
    return (
        (t or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _clip(t: str, n: int) -> str:
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------------ svg


def to_svg(model: dict, *, dark: bool = False) -> str:
    """Render the story as standalone SVG.

    Colours come through as CSS custom properties where the console can supply them,
    with literal fallbacks so the same file still reads correctly when saved out and
    opened on its own.
    """
    ink = "var(--foreground,#252016)"
    muted = "var(--muted-foreground,#786e57)"
    faint = "var(--faint,#a2977c)"
    rail = "var(--rail,#e0d5ba)"
    panel = "var(--panel,#fdfbf6)"
    gold = "var(--gold,#8a5a1b)"

    d, dur = model, model["duration_ms"]
    # Geometry follows the content: a quiet conversation gets a short picture, a busy
    # one grows downward instead of pushing its pins up through the title.
    # Lane assignment decides what can actually be labelled. Clamping a lane index
    # would silently stack the overflow on one row — three labels drawn on top of each
    # other is worse than three ticks. So anything past the cap is demoted to a tick.
    _cands = [p for p in d["pins"] if p.get("labelled")]
    for pin, lane in zip(_cands, _lanes(_cands, dur)):
        pin["labelled"] = lane < MAX_LANES
        pin["_lane"] = lane
    _labelled = [p for p in d["pins"] if p.get("labelled")]
    _lanes_used = (max(p["_lane"] for p in _labelled) + 1) if _labelled else 1
    band_y = _band_y(_lanes_used)
    H = _height(_lanes_used, len(d["decisions"]) + len(d["questions"]) + 2)
    parts = [
        f'<svg class="story" viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="{_esc(d["title"])} drawn along its own duration, '
        f'{len(d["pins"])} commitments pinned at the moment each was spoken">'
    ]

    parts.append(
        f'<text x="{LEFT}" y="56" font-size="26" fill="{ink}" '
        f'font-family="var(--font-display,Georgia,serif)">{_esc(_clip(d["title"], 64))}</text>'
        f'<text x="{LEFT}" y="84" font-size="14" fill="{muted}">{_esc(d["when"])} · '
        f'{d["duration_min"]} minutes</text>'
    )

    s = d["sentiment"]
    if s:
        parts.append(
            f'<text x="{RIGHT}" y="56" font-size="14" fill="{muted}" text-anchor="end">'
            f'{_esc(s["label"])} · valence {s["valence"]:+.2f} · confidence {s["confidence"]:.2f}</text>'
        )

    # ---- the ribbon: the recording drawn as its own duration ----------------
    if d["segments"]:
        for seg in d["segments"]:
            x0, x1 = _x(seg["from_ms"], dur), _x(seg["to_ms"], dur)
            # 2px surface gap between fills, never a border drawn around them
            parts.append(
                f'<rect x="{x0 + 1:.1f}" y="{band_y}" width="{max(x1 - x0 - 2, 1):.1f}" '
                f'height="{BAND_H}" rx="3" fill="{polarity_color(seg["valence"], dark)}"/>'
            )
            if x1 - x0 > 90:
                parts.append(
                    f'<text x="{(x0 + x1) / 2:.1f}" y="{band_y + BAND_H / 2 + 5:.0f}" '
                    f'font-size="13" text-anchor="middle" fill="{panel}">'
                    f'{seg["valence"]:+.2f}</text>'
                )
    else:
        parts.append(
            f'<rect x="{LEFT}" y="{band_y}" width="{RIGHT - LEFT}" height="{BAND_H}" rx="3" '
            f'fill="{rail}"/>'
            f'<text x="{(LEFT + RIGHT) / 2}" y="{band_y + BAND_H / 2 + 5}" font-size="13" '
            f'text-anchor="middle" fill="{muted}">not scored for tone</text>'
        )

    # ---- time axis ----------------------------------------------------------
    parts.append(
        f'<text x="{LEFT}" y="{band_y + BAND_H + 22}" font-size="12" fill="{faint}">0:00 start</text>'
        f'<text x="{RIGHT}" y="{band_y + BAND_H + 22}" font-size="12" fill="{faint}" '
        f'text-anchor="end">{_hhmm(dur)} end</text>'
    )
    for frac in (0.25, 0.5, 0.75):
        x = LEFT + frac * (RIGHT - LEFT)
        parts.append(
            f'<line x1="{x:.0f}" y1="{band_y + BAND_H}" x2="{x:.0f}" y2="{band_y + BAND_H + 8}" '
            f'stroke="{rail}" stroke-width="1"/>'
            f'<text x="{x:.0f}" y="{band_y + BAND_H + 22}" font-size="12" fill="{faint}" '
            f'text-anchor="middle">{_hhmm(dur * frac)}</text>'
        )

    # ---- commitments, pinned where they were said ---------------------------
    labelled = [p for p in d["pins"] if p.get("labelled")]
    ticks = [p for p in d["pins"] if not p.get("labelled")]
    if d["pins"]:
        for pin in labelled:
            x = _x(pin["at_ms"], dur)
            top = band_y - 26 - pin["_lane"] * LANE_H
            live = pin["status"] in ("accepted", "in_progress", "done")
            colour = gold if live else muted
            parts.append(
                f'<line x1="{x:.1f}" y1="{band_y}" x2="{x:.1f}" y2="{top + 6:.0f}" '
                f'stroke="{colour}" stroke-width="{2 if live else 1}"/>'
                f'<circle cx="{x:.1f}" cy="{band_y:.0f}" r="4" fill="{colour}" '
                f'stroke="{panel}" stroke-width="2"/>'
            )
            anchor, dx = ("end", -8) if x > (LEFT + RIGHT) * 0.6 else ("start", 8)
            parts.append(
                f'<text x="{x + dx:.1f}" y="{top:.0f}" font-size="12.5" fill="{ink}" '
                f'text-anchor="{anchor}">{_esc(_clip(pin["text"], 38))}</text>'
                f'<text x="{x + dx:.1f}" y="{top + 14:.0f}" font-size="11" fill="{faint}" '
                f'text-anchor="{anchor}">{_hhmm(pin["at_ms"])}'
                f'{"" if live else " · proposed"}</text>'
            )
        # The rest still happened, so they stay visible as ticks on the band — just
        # not worth a line of text each.
        for pin in ticks:
            x = _x(pin["at_ms"], dur)
            parts.append(
                f'<line x1="{x:.1f}" y1="{band_y - 9:.0f}" x2="{x:.1f}" y2="{band_y:.0f}" '
                f'stroke="{muted}" stroke-width="1"/>'
            )
        if ticks:
            parts.append(
                f'<text x="{LEFT}" y="{HEADER_H - 6:.0f}" font-size="11.5" fill="{faint}">'
                f'{len(ticks)} further proposals, shown as ticks on the band</text>'
            )
    else:
        parts.append(
            f'<text x="{LEFT}" y="{band_y - 30}" font-size="13" fill="{faint}">'
            f'Nothing was committed to here — a normal and common answer.</text>'
        )

    # ---- what came out of it ------------------------------------------------
    y = band_y + BAND_H + 62
    for heading, items, colour in (
        ("Decided", d["decisions"], gold),
        ("Left open", d["questions"], muted),
    ):
        if not items:
            continue
        parts.append(f'<text x="{LEFT}" y="{y}" font-size="13" fill="{colour}">{heading}</text>')
        for item in items:
            y += 19
            parts.append(
                f'<text x="{LEFT + 76}" y="{y}" font-size="12.5" fill="{ink}">'
                f'{_esc(_clip(item, 108))}</text>'
            )
        y += 30

    if s and s["drivers"]:
        parts.append(
            f'<text x="{LEFT}" y="{H - 26}" font-size="12" fill="{faint}">'
            f'Tone read from: {_esc(_clip("; ".join(s["drivers"][:3]), 120))}</text>'
        )
    parts.append(
        f'<text x="{RIGHT}" y="{H - 26}" font-size="11" fill="{faint}" text-anchor="end">'
        f'Tone is an estimate over a transcript. Segment widths are proportional, not measured.</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ----------------------------------------------------------------- excalidraw


def _el(kind: str, i: int, **kw) -> dict:
    base = {
        "id": f"s{i}", "type": kind, "angle": 0, "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 1,
        "strokeStyle": "solid", "roughness": 0, "opacity": 100, "groupIds": [],
        "frameId": None, "roundness": None, "seed": 10000 + i, "version": 1,
        "versionNonce": 10000 + i, "isDeleted": False, "boundElements": [],
        "updated": 1, "link": None, "locked": False,
    }
    base.update(kw)
    return base


def _text(i: int, x: float, y: float, text: str, size: int = 13,
          colour: str = "#1e1e1e", align: str = "left") -> dict:
    return _el(
        "text", i, x=x, y=y, width=max(len(text) * size * 0.6, 10), height=size * 1.25,
        strokeColor=colour, text=text, fontSize=size, fontFamily=3, textAlign=align,
        verticalAlign="top", containerId=None, originalText=text, lineHeight=1.25,
    )


def to_excalidraw(model: dict) -> dict:
    """The same story as an editable Excalidraw scene.

    The console renders SVG because it is live and needs no dependency. This exists for
    the other half of the ask: open it, drag things, write on it. A picture you can
    annotate is yours in a way a generated report never is.
    """
    d, dur = model, model["duration_ms"]
    cands = [p for p in d["pins"] if p.get("labelled")]
    for pin, lane in zip(cands, _lanes(cands, dur)):
        pin["labelled"] = lane < MAX_LANES
        pin["_lane"] = lane
    labelled = [p for p in d["pins"] if p.get("labelled")]
    lanes_used = (max(p["_lane"] for p in labelled) + 1) if labelled else 1
    band_y = _band_y(lanes_used)

    els: list[dict] = []
    n = 0

    def add(e):
        nonlocal n
        els.append(e)
        n += 1

    add(_text(n, LEFT, 30, _clip(d["title"], 70), 28, "#1e40af"))
    add(_text(n, LEFT, 68, f'{d["when"]} · {d["duration_min"]} minutes', 14, "#64748b"))
    s = d["sentiment"]
    if s:
        add(_text(n, LEFT, 90,
                  f'{s["label"]} · valence {s["valence"]:+.2f} · confidence {s["confidence"]:.2f}',
                  14, "#64748b"))

    if d["segments"]:
        for seg in d["segments"]:
            x0, x1 = _x(seg["from_ms"], dur), _x(seg["to_ms"], dur)
            fill = polarity_color(seg["valence"])
            add(_el("rectangle", n, x=x0 + 1, y=band_y, width=max(x1 - x0 - 2, 1),
                    height=BAND_H, strokeColor=fill, backgroundColor=fill,
                    roundness={"type": 3}, strokeWidth=2))
            if x1 - x0 > 90:
                add(_text(n, (x0 + x1) / 2 - 22, band_y + BAND_H / 2 - 9,
                          f'{seg["valence"]:+.2f}', 14, "#ffffff"))
    else:
        add(_el("rectangle", n, x=LEFT, y=band_y, width=RIGHT - LEFT, height=BAND_H,
                strokeColor="#cbd5e1", backgroundColor="#e2e8f0", roundness={"type": 3}))

    add(_text(n, LEFT, band_y + BAND_H + 10, "0:00 start", 12, "#94a3b8"))
    add(_text(n, RIGHT - 70, band_y + BAND_H + 10, f"{_hhmm(dur)} end", 12, "#94a3b8"))

    for pin in labelled:
        x = _x(pin["at_ms"], dur)
        top = band_y - 26 - pin["_lane"] * LANE_H
        live = pin["status"] in ("accepted", "in_progress", "done")
        colour = "#b45309" if live else "#64748b"
        add(_el("line", n, x=x, y=top + 8, width=0, height=band_y - top - 8,
                strokeColor=colour, strokeWidth=2 if live else 1,
                points=[[0, 0], [0, band_y - top - 8]], lastCommittedPoint=None,
                startBinding=None, endBinding=None, startArrowhead=None, endArrowhead=None))
        add(_el("ellipse", n, x=x - 5, y=band_y - 5, width=10, height=10,
                strokeColor=colour, backgroundColor=colour))
        label = _clip(pin["text"], 38)
        lx = x + 10 if x < (LEFT + RIGHT) * 0.6 else x - 10 - len(label) * 7.6
        add(_text(n, lx, top - 12, label, 13, "#1e1e1e"))
        add(_text(n, lx, top + 4, f'{_hhmm(pin["at_ms"])}{"" if live else " · proposed"}',
                  11, "#94a3b8"))

    y = band_y + BAND_H + 52
    for heading, items, colour in (("Decided", d["decisions"], "#b45309"),
                                   ("Left open", d["questions"], "#64748b")):
        if not items:
            continue
        add(_text(n, LEFT, y, heading, 14, colour))
        for item in items:
            y += 20
            add(_text(n, LEFT + 90, y, _clip(item, 100), 13, "#1e1e1e"))
        y += 32

    add(_text(n, LEFT, y + 12,
              "Tone is an estimate over a transcript. Segment widths are proportional, not measured.",
              11, "#94a3b8"))

    return {
        "type": "excalidraw", "version": 2, "source": "plaudvault",
        "elements": els,
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20},
        "files": {},
    }
