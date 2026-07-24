"""Generate the HYPE mark: desktop/resources/icon.ico (shell icon) and www/favicon.ico (web app).

One white stream meander over the app's accent navy (#2f4b7c family, www/styles.css tokens),
with a pair of vertical exchange arrows beneath it — downwelling and upwelling, the hyporheic
signature. Drawn at 512 px with supersampling headroom and downsampled into multi-resolution
.ico files. Deterministic: rerunning produces the same art.

Usage:  python desktop/scripts/make_icon.py [repo-root]
Requires Pillow (the repo .venv has it via matplotlib).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 512
TOP_COLOR = (58, 90, 148)      # lighter accent
BOTTOM_COLOR = (26, 53, 94)    # deeper accent
INK = (255, 255, 255)


def build_mark(size: int = CANVAS, supersample: int = 2) -> Image.Image:
    big = size * supersample
    art = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(art)

    # Vertical gradient background.
    for y in range(big):
        t = y / (big - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(TOP_COLOR, BOTTOM_COLOR))
        draw.line([(0, y), (big, y)], fill=color + (255,))

    # The stream: one thick meander band across the upper third (a stroked polyline this
    # wide grows fringe artifacts, so it is a filled band).
    baseline, amplitude, width = 0.30, 0.05, 60
    half = width * supersample / 2

    def center(x: float) -> float:
        return big * baseline + big * amplitude * math.sin(2 * math.pi * 1.25 * x / big + 0.6)

    xs = list(range(-8, big + 9, 4))
    upper = [(x, center(x) - half) for x in xs]
    lower = [(x, center(x) + half) for x in reversed(xs)]
    draw.polygon(upper + lower, fill=INK + (255,))

    # Exchange arrows under the bed: downwelling (left, pointing down) and upwelling
    # (right, pointing up).
    def arrow(cx: float, top: float, bottom: float, head_h: float, head_w: float,
              shaft_w: float, down: bool) -> None:
        cx, top, bottom = cx * big, top * big, bottom * big
        head_h, head_w, shaft_w = head_h * big, head_w * big, shaft_w * big
        if down:
            shaft = [(cx - shaft_w / 2, top), (cx + shaft_w / 2, top),
                     (cx + shaft_w / 2, bottom - head_h), (cx - shaft_w / 2, bottom - head_h)]
            head = [(cx - head_w / 2, bottom - head_h), (cx + head_w / 2, bottom - head_h),
                    (cx, bottom)]
        else:
            shaft = [(cx - shaft_w / 2, bottom), (cx + shaft_w / 2, bottom),
                     (cx + shaft_w / 2, top + head_h), (cx - shaft_w / 2, top + head_h)]
            head = [(cx - head_w / 2, top + head_h), (cx + head_w / 2, top + head_h),
                    (cx, top)]
        draw.polygon(shaft, fill=INK + (235,))
        draw.polygon(head, fill=INK + (235,))

    arrow(0.36, 0.50, 0.84, head_h=0.10, head_w=0.17, shaft_w=0.065, down=True)
    arrow(0.64, 0.50, 0.84, head_h=0.10, head_w=0.17, shaft_w=0.065, down=False)

    # Rounded-square mask.
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (big - 1, big - 1)], radius=round(big * 0.18), fill=255)
    art.putalpha(mask)
    return art.resize((size, size), Image.LANCZOS)


def save_ico(art: Image.Image, path: Path, sizes: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    art.save(path, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"wrote {path} ({path.stat().st_size} bytes, sizes {sizes})")


def main() -> None:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    art = build_mark()
    save_ico(art, repo / "desktop" / "resources" / "icon.ico", [16, 24, 32, 48, 64, 128, 256])
    save_ico(art, repo / "www" / "favicon.ico", [16, 32, 48])


if __name__ == "__main__":
    main()
