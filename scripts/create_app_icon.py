from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ICONSET = ASSETS / "usag.iconset"
ICNS = ASSETS / "usag.icns"
SIZE = 1024

ORANGE = (244, 145, 100)
TEAL = (70, 181, 198)
INK = (31, 41, 55)
BG_TOP = (248, 250, 252)
BG_BOTTOM = (226, 235, 241)


def main() -> None:
    base = _render_base_icon()
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)

    for points in (16, 32, 128, 256, 512):
        _save_icon(base, points, scale=1)
        _save_icon(base, points, scale=2)

    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True)
    print(f"created {ICNS}")


def _render_base_icon() -> Image.Image:
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((116, 130, 908, 906), radius=188, fill=(15, 23, 42, 58))
    shadow = shadow.filter(ImageFilter.GaussianBlur(34))
    icon.alpha_composite(shadow)

    card = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    card_mask = Image.new("L", (SIZE, SIZE), 0)
    mask_draw = ImageDraw.Draw(card_mask)
    mask_draw.rounded_rectangle((104, 92, 920, 884), radius=184, fill=255)

    gradient = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    grad_px = gradient.load()
    for y in range(SIZE):
        ratio = y / (SIZE - 1)
        color = tuple(round(BG_TOP[i] * (1 - ratio) + BG_BOTTOM[i] * ratio) for i in range(3))
        for x in range(SIZE):
            grad_px[x, y] = (*color, 255)
    card.alpha_composite(gradient)
    card.putalpha(card_mask)
    icon.alpha_composite(card)

    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((104, 92, 920, 884), radius=184, outline=(255, 255, 255, 170), width=9)
    draw.rounded_rectangle((146, 138, 878, 838), radius=142, outline=(15, 23, 42, 28), width=5)

    _draw_progress_ring(draw, (250, 212, 774, 736), start=-218, end=36, color=ORANGE, width=70)
    _draw_progress_ring(draw, (250, 212, 774, 736), start=52, end=206, color=TEAL, width=70)
    _draw_progress_ring(
        draw,
        (250, 212, 774, 736),
        start=220,
        end=302,
        color=(204, 213, 223),
        width=70,
    )

    draw.rounded_rectangle((320, 568, 724, 630), radius=31, fill=(226, 232, 240, 255))
    draw.rounded_rectangle((320, 568, 574, 630), radius=31, fill=TEAL)
    draw.rounded_rectangle((320, 670, 724, 732), radius=31, fill=(226, 232, 240, 255))
    draw.rounded_rectangle((320, 670, 470, 732), radius=31, fill=ORANGE)

    font = _font(360)
    text = "u"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text(((SIZE - text_w) / 2, 254 - bbox[1] + (250 - text_h) / 2), text, font=font, fill=INK)

    draw.rounded_rectangle((662, 236, 744, 318), radius=28, fill=(255, 255, 255, 228))
    draw.rounded_rectangle((686, 272, 720, 300), radius=8, fill=TEAL)
    draw.rounded_rectangle((686, 254, 720, 264), radius=5, fill=ORANGE)

    return icon


def _draw_progress_ring(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    start: int,
    end: int,
    color: tuple[int, int, int],
    width: int,
) -> None:
    draw.arc(box, start=start, end=end, fill=color, width=width)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in (
        "/System/Library/Fonts/SFNSRounded.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _save_icon(base: Image.Image, points: int, *, scale: int) -> None:
    pixels = points * scale
    filename = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
    resized = base.resize((pixels, pixels), Image.Resampling.LANCZOS)
    resized.save(ICONSET / filename)


if __name__ == "__main__":
    main()
