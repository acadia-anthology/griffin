import io
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Real font files aren't bundled yet — drop Arimo-Regular.ttf / Arimo-Bold.ttf
# (or whatever's decided on) into assets/fonts/ and these paths will pick them
# up automatically. Until then this falls back to Pillow's built-in font.
_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_BOLD = os.path.join(_HERE, "..", "assets", "fonts", "bold.ttf")
FONT_REGULAR = os.path.join(_HERE, "..", "assets", "fonts", "regular.ttf")

BG_COLOR = (43, 43, 42)
BORDER_COLOR = (75, 75, 72)
GOLD = (201, 173, 106)
WHITE = (225, 225, 225)
BAR_BG = (90, 90, 88)
BAR_FILL = GOLD


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size=size)


def _circular_avatar(avatar_bytes: bytes, size: int, ring_width: int = 4) -> Image.Image:
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(avatar, (0, 0), mask)
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        (ring_width // 2, ring_width // 2, size - ring_width // 2, size - ring_width // 2),
        outline=GOLD, width=ring_width
    )
    return Image.alpha_composite(out, ring)


def _progress_bar(draw: ImageDraw.ImageDraw, box, ratio: float):
    x0, y0, x1, y1 = box
    radius = (y1 - y0) // 2
    draw.rounded_rectangle(box, radius=radius, fill=BAR_BG)
    ratio = max(0.0, min(1.0, ratio))
    if ratio > 0:
        fill_x1 = max(x0 + int((x1 - x0) * ratio), x0 + (y1 - y0))
        draw.rounded_rectangle((x0, y0, min(fill_x1, x1), y1), radius=radius, fill=BAR_FILL)


def _base_canvas(w: int, h: int, background_bytes: Optional[bytes]) -> Image.Image:
    img = Image.new("RGB", (w, h), BG_COLOR)
    if background_bytes:
        try:
            bg = Image.open(io.BytesIO(background_bytes)).convert("RGB")
            scale = max(w / bg.width, h / bg.height)
            bg = bg.resize((max(1, int(bg.width * scale)), max(1, int(bg.height * scale))))
            x = (bg.width - w) // 2
            y = (bg.height - h) // 2
            bg = bg.crop((x, y, x + w, y + h))
            overlay = Image.new("RGB", (w, h), (0, 0, 0))
            img = Image.blend(bg, overlay, 0.35)  # darken so text stays legible over art
        except Exception:
            pass
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, w - 1, h - 1), outline=BORDER_COLOR, width=2)
    return img


def _gg_text(gg_into_level: int, gg_needed: int) -> str:
    if gg_needed:
        return f"{gg_into_level:,} / {gg_needed:,} GG"
    return f"{gg_into_level:,} GG (MAX)"


def render_rank_card(name: str, avatar_bytes: bytes, level: int, rank: Optional[int],
                      gg_into_level: int, gg_needed: int,
                      background_bytes: Optional[bytes] = None) -> io.BytesIO:
    W, H = 700, 200
    img = _base_canvas(W, H, background_bytes)
    draw = ImageDraw.Draw(img)

    avatar_size = 140
    avatar = _circular_avatar(avatar_bytes, avatar_size)
    img.paste(avatar, (W - avatar_size - 30, (H - avatar_size) // 2), avatar)

    text_x = 30
    draw.text((text_x, 20), name, font=_font(True, 30), fill=GOLD)
    draw.text((text_x, 60), f"GOBLIN GRADE: {level}", font=_font(True, 20), fill=WHITE)

    ratio = (gg_into_level / gg_needed) if gg_needed else 1.0
    bar_right = W - avatar_size - 60
    bar_box = (text_x, 102, bar_right, 120)
    _progress_bar(draw, bar_box, ratio)

    rank_text = f"RANK: {rank}" if rank else "RANK: Unranked"
    draw.text((text_x, 130), rank_text, font=_font(False, 16), fill=WHITE)

    gg_text = _gg_text(gg_into_level, gg_needed)
    bbox = draw.textbbox((0, 0), gg_text, font=_font(False, 16))
    draw.text((bar_right - (bbox[2] - bbox[0]), 130), gg_text, font=_font(False, 16), fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_levelup_card(name: str, avatar_bytes: bytes, level: int, tier_name: str,
                         gg_into_level: int, gg_needed: int,
                         background_bytes: Optional[bytes] = None) -> io.BytesIO:
    W, H = 900, 260
    img = _base_canvas(W, H, background_bytes)
    draw = ImageDraw.Draw(img)

    avatar_size = 190
    avatar = _circular_avatar(avatar_bytes, avatar_size)
    img.paste(avatar, (W - avatar_size - 40, (H - avatar_size) // 2), avatar)

    text_x = 40
    draw.text((text_x, 28), name, font=_font(True, 40), fill=GOLD)
    draw.text((text_x, 76), tier_name, font=_font(False, 22), fill=WHITE)
    draw.text((text_x, 110), f"GOBLIN GRADE {level}!", font=_font(True, 34), fill=GOLD)

    ratio = (gg_into_level / gg_needed) if gg_needed else 1.0
    bar_right = W - avatar_size - 70
    bar_box = (text_x, 172, bar_right, 192)
    _progress_bar(draw, bar_box, ratio)

    gg_text = _gg_text(gg_into_level, gg_needed)
    bbox = draw.textbbox((0, 0), gg_text, font=_font(False, 18))
    draw.text((bar_right - (bbox[2] - bbox[0]), 200), gg_text, font=_font(False, 18), fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
