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
RANK_BAR_BG = (75, 75, 72)
BADGE_BG = (20, 20, 19)
BADGE_OLD = (150, 150, 148)

# Every render function below is laid out in "logical" pixels, then scaled up
# before drawing. Discord doesn't upscale small attachments to fill the chat
# width, so a card sized for its logical dimensions looks soft on high-DPI
# screens — rendering at 2x and letting the client downscale keeps text and
# edges crisp. Bump SCALE (not the per-card numbers) to go sharper later.
SCALE = 2


def s(value: int) -> int:
    return round(value * SCALE)


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


def _rounded_triangle(width: int, height: int, fill, supersample: int = 4) -> Image.Image:
    """A right-pointing triangle with corners softened by supersample + downscale,
    since PIL has no native rounded-polygon primitive."""
    big_w, big_h = width * supersample, height * supersample
    big = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    ImageDraw.Draw(big).polygon([(0, 0), (0, big_h), (big_w, big_h // 2)], fill=fill)
    return big.resize((width, height), Image.LANCZOS)


def _progress_bar(draw: ImageDraw.ImageDraw, box, ratio: float, bar_bg):
    x0, y0, x1, y1 = box
    radius = (y1 - y0) // 2
    draw.rounded_rectangle(box, radius=radius, fill=bar_bg)
    ratio = max(0.0, min(1.0, ratio))
    if ratio > 0:
        fill_x1 = max(x0 + int((x1 - x0) * ratio), x0 + (y1 - y0))
        draw.rounded_rectangle((x0, y0, min(fill_x1, x1), y1), radius=radius, fill=BAR_FILL)


def _base_canvas(w: int, h: int, bg_color, background_bytes: Optional[bytes]) -> Image.Image:
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
    return img


def _gg_text(gg_into_level: int, gg_needed: int) -> str:
    if gg_needed:
        return f"{gg_into_level:,} / {gg_needed:,} GG"
    return f"{gg_into_level:,} GG (MAX)"


def render_rank_card(name: str, avatar_bytes: bytes, level: int, rank: Optional[int],
                      gg_into_level: int, gg_needed: int,
                      background_bytes: Optional[bytes] = None) -> io.BytesIO:
    W, H = s(480), s(130)
    img = _base_canvas(W, H, RANK_BG, background_bytes)
    draw = ImageDraw.Draw(img)

    avatar_size = s(85)
    avatar = _circular_avatar(avatar_bytes, avatar_size, ring_width=s(4))
    img.paste(avatar, (W - avatar_size - s(18), (H - avatar_size) // 2), avatar)

    text_x = s(18)
    draw.text((text_x, s(12)), name, font=_font(True, s(20)), fill=GOLD)
    draw.text((text_x, s(38)), f"LEVEL: {level}", font=_font(True, s(15)), fill=WHITE)

    ratio = (gg_into_level / gg_needed) if gg_needed else 1.0
    bar_right = W - avatar_size - s(36)
    bar_box = (text_x, s(66), bar_right, s(76))
    _progress_bar(draw, bar_box, ratio, RANK_BAR_BG)

    rank_text = f"RANK: {rank}" if rank else "RANK: Unranked"
    draw.text((text_x, s(84)), rank_text, font=_font(False, s(12)), fill=WHITE)

    gg_text = _gg_text(gg_into_level, gg_needed)
    bbox = draw.textbbox((0, 0), gg_text, font=_font(False, s(12)))
    draw.text((bar_right - (bbox[2] - bbox[0]), s(84)), gg_text, font=_font(False, s(12)), fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_levelup_card(name: str, avatar_bytes: bytes, old_level: int, new_level: int,
                         background_bytes: Optional[bytes] = None) -> io.BytesIO:
    W, H = s(780), s(220)
    pill_radius = H // 2

    # Same background/accent treatment as the rank card — the two are meant
    # to read as one family, differentiated by shape (pill vs rectangle) and
    # content, not by separate color palettes.
    base = _base_canvas(W, H, RANK_BG, background_bytes).convert("RGBA")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=pill_radius, fill=255)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.paste(base, (0, 0), mask)
    draw = ImageDraw.Draw(img)

    avatar_size = s(120)
    avatar_x = W - avatar_size - s(45)
    avatar = _circular_avatar(avatar_bytes, avatar_size, ring_width=s(4))
    img.paste(avatar, (avatar_x, (H - avatar_size) // 2), avatar)

    text_x = s(60)
    name_font = _font(True, s(40))
    levelup_font = _font(True, s(56))
    num_font = _font(True, s(32))

    name_bbox = draw.textbbox((0, 0), name, font=name_font)
    levelup_bbox = draw.textbbox((0, 0), "LEVEL UP!", font=levelup_font)
    name_h = name_bbox[3] - name_bbox[1]
    levelup_h = levelup_bbox[3] - levelup_bbox[1]
    levelup_w = levelup_bbox[2] - levelup_bbox[0]

    # Name + "LEVEL UP!" form one block, vertically centered against the
    # card (and therefore the avatar) instead of pinned near the top.
    line_gap = s(6)
    block_h = name_h + line_gap + levelup_h
    block_top = (H - block_h) // 2
    name_y = block_top - name_bbox[1]
    levelup_ink_top = block_top + name_h + line_gap
    levelup_y = levelup_ink_top - levelup_bbox[1]
    levelup_center_y = levelup_ink_top + levelup_h // 2

    draw.text((text_x, name_y), name, font=name_font, fill=GOLD)
    draw.text((text_x, levelup_y), "LEVEL UP!", font=levelup_font, fill=WHITE)

    # transition badge: "{old level} ▶ {new level}", aligned to the
    # "LEVEL UP!" line rather than the full card height.
    old_text, new_text = str(old_level), str(new_level)
    gap = s(14)
    arrow_w = s(11)
    old_bbox = draw.textbbox((0, 0), old_text, font=num_font)
    new_bbox = draw.textbbox((0, 0), new_text, font=num_font)
    old_w = old_bbox[2] - old_bbox[0]
    new_w = new_bbox[2] - new_bbox[0]
    text_top, text_bottom = old_bbox[1], old_bbox[3]
    text_h = text_bottom - text_top
    pad_x, pad_y = s(26), s(7)
    badge_w = old_w + gap + arrow_w + gap + new_w + pad_x * 2
    badge_h = text_h + pad_y * 2

    badge_x = text_x + levelup_w + s(50)
    badge_x = min(badge_x, avatar_x - badge_w - s(30))
    badge_y = levelup_center_y - badge_h // 2
    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
        radius=badge_h // 2, fill=BADGE_BG
    )
    cx = badge_x + pad_x
    cy = badge_y + pad_y - text_top
    draw.text((cx, cy), old_text, font=num_font, fill=BADGE_OLD)
    cx += old_w + gap

    # Triangle: drawn at 4x on its own small canvas and downsampled, so the
    # resample filter softens the corners instead of hard polygon points.
    tri_h = text_h // 2
    tri_mid_y = badge_y + pad_y + text_h // 2
    triangle = _rounded_triangle(arrow_w, tri_h, GOLD)
    img.paste(triangle, (cx, tri_mid_y - tri_h // 2), triangle)
    cx += arrow_w + gap
    draw.text((cx, cy), new_text, font=num_font, fill=GOLD)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
