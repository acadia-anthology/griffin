import io
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Arimo (OFL-licensed, metric-compatible Arial substitute — safe to bundle,
# unlike actual Arial). It's a variable font, so bold/regular is one file
# with the weight axis switched rather than two separate files.
_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(_HERE, "..", "assets", "fonts", "Arimo.ttf")

GOLD = (201, 173, 106)
WHITE = (225, 225, 225)
BAR_FILL = GOLD

# The two cards are deliberately different sizes and shades — level-up is the
# big celebratory one, rank is a small compact status readout. Discord scales
# attachment previews to a shared width, so the *pixel* dimensions (not just
# relative proportions) need a real gap or they read as the same size in-chat.
RANK_BG = (35, 35, 34)
RANK_BORDER = (65, 65, 62)
RANK_BAR_BG = (75, 75, 72)

LEVELUP_BG = (74, 73, 69)
LEVELUP_BORDER = (100, 99, 94)
LEVELUP_BAR_BG = (110, 109, 104)


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    try:
        font = ImageFont.truetype(FONT_PATH, size)
        font.set_variation_by_name("Bold" if bold else "Regular")
        return font
    except (OSError, ValueError):
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


def _progress_bar(draw: ImageDraw.ImageDraw, box, ratio: float, bar_bg):
    x0, y0, x1, y1 = box
    radius = (y1 - y0) // 2
    draw.rounded_rectangle(box, radius=radius, fill=bar_bg)
    ratio = max(0.0, min(1.0, ratio))
    if ratio > 0:
        fill_x1 = max(x0 + int((x1 - x0) * ratio), x0 + (y1 - y0))
        draw.rounded_rectangle((x0, y0, min(fill_x1, x1), y1), radius=radius, fill=BAR_FILL)


def _base_canvas(w: int, h: int, bg_color, border_color, background_bytes: Optional[bytes]) -> Image.Image:
    img = Image.new("RGB", (w, h), bg_color)
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
    draw.rectangle((0, 0, w - 1, h - 1), outline=border_color, width=2)
    return img


def _gg_text(gg_into_level: int, gg_needed: int) -> str:
    if gg_needed:
        return f"{gg_into_level:,} / {gg_needed:,} GG"
    return f"{gg_into_level:,} GG (MAX)"


def render_rank_card(name: str, avatar_bytes: bytes, level: int, rank: Optional[int],
                      gg_into_level: int, gg_needed: int,
                      background_bytes: Optional[bytes] = None) -> io.BytesIO:
    W, H = 480, 130
    img = _base_canvas(W, H, RANK_BG, RANK_BORDER, background_bytes)
    draw = ImageDraw.Draw(img)

    avatar_size = 85
    avatar = _circular_avatar(avatar_bytes, avatar_size)
    img.paste(avatar, (W - avatar_size - 18, (H - avatar_size) // 2), avatar)

    text_x = 18
    draw.text((text_x, 12), name, font=_font(True, 20), fill=GOLD)
    draw.text((text_x, 38), f"LEVEL: {level}", font=_font(True, 15), fill=WHITE)

    ratio = (gg_into_level / gg_needed) if gg_needed else 1.0
    bar_right = W - avatar_size - 36
    bar_box = (text_x, 66, bar_right, 76)
    _progress_bar(draw, bar_box, ratio, RANK_BAR_BG)

    rank_text = f"RANK: {rank}" if rank else "RANK: Unranked"
    draw.text((text_x, 84), rank_text, font=_font(False, 12), fill=WHITE)

    gg_text = _gg_text(gg_into_level, gg_needed)
    bbox = draw.textbbox((0, 0), gg_text, font=_font(False, 12))
    draw.text((bar_right - (bbox[2] - bbox[0]), 84), gg_text, font=_font(False, 12), fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_levelup_card(name: str, avatar_bytes: bytes, level: int, tier_name: str,
                         gg_into_level: int, gg_needed: int,
                         background_bytes: Optional[bytes] = None) -> io.BytesIO:
    W, H = 780, 300
    img = _base_canvas(W, H, LEVELUP_BG, LEVELUP_BORDER, background_bytes)
    draw = ImageDraw.Draw(img)

    avatar_size = 200
    avatar = _circular_avatar(avatar_bytes, avatar_size)
    img.paste(avatar, (W - avatar_size - 35, (H - avatar_size) // 2), avatar)

    text_x = 35
    draw.text((text_x, 30), name, font=_font(True, 42), fill=GOLD)
    draw.text((text_x, 82), tier_name, font=_font(False, 20), fill=WHITE)
    draw.text((text_x, 116), f"LEVEL {level}!", font=_font(True, 36), fill=GOLD)

    ratio = (gg_into_level / gg_needed) if gg_needed else 1.0
    bar_right = W - avatar_size - 65
    bar_box = (text_x, 190, bar_right, 208)
    _progress_bar(draw, bar_box, ratio, LEVELUP_BAR_BG)

    gg_text = _gg_text(gg_into_level, gg_needed)
    bbox = draw.textbbox((0, 0), gg_text, font=_font(False, 16))
    draw.text((bar_right - (bbox[2] - bbox[0]), 216), gg_text, font=_font(False, 16), fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
