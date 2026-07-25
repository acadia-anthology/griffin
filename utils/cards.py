import io
import os
import re as _re
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Arimo (OFL-licensed, metric-compatible Arial substitute — safe to bundle,
# unlike actual Arial). It's a variable font, so bold/regular is one file
# with the weight axis switched rather than two separate files.
_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(_HERE, "..", "assets", "fonts", "Arimo.ttf")

# Hardcoded bot-wide fallback background — used on every card whenever a
# member hasn't equipped their own library card. Drop an image in at this
# path (assets/default_background.png) to enable it; until then this is a
# no-op and cards just show their flat solid color, same as before.
DEFAULT_BACKGROUND_PATH = os.path.join(_HERE, "..", "assets", "default_background.png")
_default_background_cache = None
_default_background_loaded = False


def _load_default_background() -> Optional[bytes]:
    global _default_background_cache, _default_background_loaded
    if not _default_background_loaded:
        try:
            with open(DEFAULT_BACKGROUND_PATH, "rb") as f:
                _default_background_cache = f.read()
        except OSError:
            _default_background_cache = None
        _default_background_loaded = True
    return _default_background_cache

GOLD = (201, 173, 106)
WHITE = (225, 225, 225)
BAR_FILL = GOLD

# Matches assets/default_background.png — used as the accent whenever a card
# has no custom accent_color of its own (including the bot-wide default).
DEFAULT_ACCENT = (57, 181, 74)  # #39b54a

# The two cards are deliberately different sizes and shades — level-up is the
# big celebratory one, rank is a small compact status readout. Discord scales
# attachment previews to a shared width, so the *pixel* dimensions (not just
# relative proportions) need a real gap or they read as the same size in-chat.
RANK_BG = (35, 35, 34)
RANK_BAR_BG = (75, 75, 72)
BADGE_BG = (20, 20, 19)
BADGE_OLD = (150, 150, 148)
MUTED = (170, 170, 168)
PLACEHOLDER = (140, 140, 138)

# Every render function below is laid out in "logical" pixels, then scaled up
# before drawing. Discord doesn't upscale small attachments to fill the chat
# width, so a card sized for its logical dimensions looks soft on high-DPI
# screens — rendering at 2x and letting the client downscale keeps text and
# edges crisp. Bump SCALE (not the per-card numbers) to go sharper later.
SCALE = 2


def s(value: int) -> int:
    return round(value * SCALE)


# Same approach Abraxos uses for names the card font can't render: keep Basic
# Latin + Latin Extended (letters, accented Western/Eastern European chars,
# numbers, common punctuation), strip emoji/symbols/decorative unicode
# lookalikes that render as tofu boxes. Falls back to the Discord username
# (restricted to safe characters by Discord itself) if the display name has
# nothing legible left, and to a truncated raw fallback as a last resort.
_EXTRA_NAME_CHARS = set('""\'\'—–…•·')


def _clean_name(text: str) -> str:
    out = []
    for ch in text:
        if ch == ' ':
            out.append(' ')
        elif (ord(ch) <= 0x024F and ch.isprintable()) or ch in _EXTRA_NAME_CHARS:
            out.append(ch)
    cleaned = _re.sub(r'\s+', ' ', "".join(out)).strip()
    if cleaned and any(c.isalpha() or c.isdigit() for c in cleaned):
        return cleaned
    return ""


def sanitize_name(name: str, fallback: str) -> str:
    cleaned = _clean_name(name)
    if cleaned:
        return cleaned
    cleaned_fallback = _clean_name(fallback)
    if cleaned_fallback:
        return cleaned_fallback
    return fallback[:30]


def parse_hex_color(value: str) -> Optional[tuple]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    try:
        font = ImageFont.truetype(FONT_PATH, size)
        font.set_variation_by_name("Bold" if bold else "Regular")
        return font
    except (OSError, ValueError):
        return ImageFont.load_default(size=size)


def _circular_avatar(avatar_bytes: bytes, size: int, ring_width: int = 4, ring_color=GOLD) -> Image.Image:
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(avatar, (0, 0), mask)
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        (ring_width // 2, ring_width // 2, size - ring_width // 2, size - ring_width // 2),
        outline=ring_color, width=ring_width
    )
    return Image.alpha_composite(out, ring)


def _rounded_triangle(width: int, height: int, fill, supersample: int = 4) -> Image.Image:
    """A right-pointing triangle with corners softened by supersample + downscale,
    since PIL has no native rounded-polygon primitive."""
    big_w, big_h = width * supersample, height * supersample
    big = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    ImageDraw.Draw(big).polygon([(0, 0), (0, big_h), (big_w, big_h // 2)], fill=fill)
    return big.resize((width, height), Image.LANCZOS)


def _progress_bar(draw: ImageDraw.ImageDraw, box, ratio: float, bar_bg, fill_color=BAR_FILL):
    x0, y0, x1, y1 = box
    radius = (y1 - y0) // 2
    draw.rounded_rectangle(box, radius=radius, fill=bar_bg)
    ratio = max(0.0, min(1.0, ratio))
    if ratio > 0:
        # Fill sits inset inside the track on all sides (not flush with the
        # top/bottom/left edges) so the gray track reads as a visible margin
        # around it, not just leftover space on the right.
        pad = max(2, (y1 - y0) // 5)
        fx0, fy0, fy1 = x0 + pad, y0 + pad, y1 - pad
        f_radius = (fy1 - fy0) // 2
        fill_w = (x1 - x0 - pad * 2) * ratio
        fx1 = max(fx0 + int(fill_w), fx0 + f_radius * 2)
        fx1 = min(fx1, x1 - pad)
        draw.rounded_rectangle((fx0, fy0, fx1, fy1), radius=f_radius, fill=fill_color)


def _base_canvas(w: int, h: int, bg_color, background_bytes: Optional[bytes]) -> Image.Image:
    is_default = False
    if not background_bytes:
        background_bytes = _load_default_background()
        is_default = background_bytes is not None
    img = Image.new("RGB", (w, h), bg_color)
    if background_bytes:
        try:
            bg = Image.open(io.BytesIO(background_bytes)).convert("RGB")
            scale = max(w / bg.width, h / bg.height)
            bg = bg.resize((max(1, int(bg.width * scale)), max(1, int(bg.height * scale))))
            x = (bg.width - w) // 2
            y = (bg.height - h) // 2
            bg = bg.crop((x, y, x + w, y + h))
            if is_default:
                # The bundled default is curated to already be legible —
                # no need to wash it down like arbitrary uploaded art.
                img = bg
            else:
                # Wash the card's own background color over the art at 60%
                # instead of just darkening toward black — keeps text legible
                # while tying the art into the card's accent/background color.
                overlay = Image.new("RGB", (w, h), bg_color)
                img = Image.blend(bg, overlay, 0.6)
        except Exception:
            pass
    return img


def _gg_text(total_gg: int, gg_into_level: int, gg_needed: int) -> str:
    # Headline number is the member's actual total GG, not progress-since-
    # last-threshold — showing gg_into_level alone reads as "0 GG" right
    # after leveling up, which is misleading even though it's correct math.
    if gg_needed:
        next_threshold = total_gg - gg_into_level + gg_needed
        return f"{total_gg:,} / {next_threshold:,} GG"
    return f"{total_gg:,} GG (MAX)"


def render_rank_card(name: str, avatar_bytes: bytes, level: int, rank: Optional[int],
                      total_gg: int, gg_into_level: int, gg_needed: int,
                      background_bytes: Optional[bytes] = None,
                      accent_color: Optional[tuple] = None) -> io.BytesIO:
    accent = accent_color or DEFAULT_ACCENT
    W, H = s(480), s(130)
    img = _base_canvas(W, H, RANK_BG, background_bytes)
    draw = ImageDraw.Draw(img)

    avatar_size = s(85)
    avatar = _circular_avatar(avatar_bytes, avatar_size, ring_width=s(4), ring_color=accent)
    img.paste(avatar, (W - avatar_size - s(18), (H - avatar_size) // 2), avatar)

    text_x = s(18)
    draw.text((text_x, s(12)), name, font=_font(True, s(20)), fill=accent)
    draw.text((text_x, s(38)), f"LEVEL: {level}", font=_font(True, s(15)), fill=WHITE)

    ratio = (gg_into_level / gg_needed) if gg_needed else 1.0
    bar_right = W - avatar_size - s(36)
    bar_box = (text_x, s(66), bar_right, s(76))
    _progress_bar(draw, bar_box, ratio, RANK_BAR_BG, fill_color=accent)

    rank_text = f"RANK: {rank}" if rank else "RANK: Unranked"
    draw.text((text_x, s(84)), rank_text, font=_font(False, s(12)), fill=WHITE)

    gg_text = _gg_text(total_gg, gg_into_level, gg_needed)
    bbox = draw.textbbox((0, 0), gg_text, font=_font(False, s(12)))
    draw.text((bar_right - (bbox[2] - bbox[0]), s(84)), gg_text, font=_font(False, s(12)), fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_levelup_card(name: str, avatar_bytes: bytes, old_level: int, new_level: int,
                         background_bytes: Optional[bytes] = None,
                         accent_color: Optional[tuple] = None) -> io.BytesIO:
    accent = accent_color or DEFAULT_ACCENT
    W, H = s(780), s(122)
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
    avatar = _circular_avatar(avatar_bytes, avatar_size, ring_width=s(4), ring_color=accent)
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
    line_gap = s(14)
    block_h = name_h + line_gap + levelup_h
    block_top = (H - block_h) // 2
    name_y = block_top - name_bbox[1]
    levelup_ink_top = block_top + name_h + line_gap
    levelup_y = levelup_ink_top - levelup_bbox[1]
    levelup_center_y = levelup_ink_top + levelup_h // 2

    draw.text((text_x, name_y), name, font=name_font, fill=accent)
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
    triangle = _rounded_triangle(arrow_w, tri_h, accent)
    img.paste(triangle, (cx, tri_mid_y - tri_h // 2), triangle)
    cx += arrow_w + gap
    draw.text((cx, cy), new_text, font=num_font, fill=accent)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list:
    if not text:
        return []
    words = text.split()
    lines = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_library_card(name: str, avatar_bytes: bytes, level: int, rank: Optional[int],
                         total_gg: int, member_since: str,
                         bio: Optional[str], favorite_genres: Optional[str],
                         books_checked_out: Optional[str] = None,
                         birthday: Optional[str] = None,
                         background_bytes: Optional[bytes] = None,
                         accent_color: Optional[tuple] = None) -> io.BytesIO:
    """Taller than the rank/level-up cards and variable height — the bottom
    grows to fit the bio/genres text, so layout is computed twice: once on a
    throwaway canvas to measure total height, then for real once H is known."""
    accent = accent_color or DEFAULT_ACCENT
    W = s(700)
    pad_x = s(40)
    content_w = W - pad_x * 2
    avatar_size = s(150)
    avatar_top_padding = s(25)
    # Wallpaper extends 75% down the avatar instead of just to its center,
    # so only a small sliver of the bottom of the avatar sits in the solid
    # body color — avatar position itself doesn't move, only the seam does.
    banner_h = avatar_top_padding + int(avatar_size * 0.75)
    bar_h = s(44)

    name_font = _font(True, s(34))
    label_font = _font(True, s(18))
    bar_font = _font(True, s(18))
    meta_font = _font(False, s(15))
    section_font = _font(True, s(20))
    body_font = _font(False, s(17))
    line_h = s(24)

    measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    name_bbox = measure.textbbox((0, 0), name, font=name_font)
    label_bbox = measure.textbbox((0, 0), "LIBRARY CARD", font=label_font)
    meta_bbox = measure.textbbox((0, 0), "Ag", font=meta_font)
    section_bbox = measure.textbbox((0, 0), "PATRON SUMMARY", font=section_font)
    meta_line_h = (meta_bbox[3] - meta_bbox[1]) + s(6)
    section_h = section_bbox[3] - section_bbox[1]

    bio_text = bio or "Patron has yet to tell us about themself..."
    bio_color = WHITE if bio else PLACEHOLDER
    bio_lines = _wrap_text(measure, bio_text, body_font, content_w)

    # Favorite Genres is the one section that's omitted entirely when empty,
    # rather than showing placeholder copy like the other two.
    genres_lines = _wrap_text(measure, favorite_genres, body_font, content_w) if favorite_genres else []

    books_text = books_checked_out or (
        "Patron should check out a book soon, or their library card may suspiciously be suspended..."
    )
    books_color = WHITE if books_checked_out else PLACEHOLDER
    books_lines = _wrap_text(measure, books_text, body_font, content_w)

    # --- accumulate total height ---
    y = banner_h + avatar_size // 2  # avatar straddles the banner/body seam
    y += s(20)
    y += (name_bbox[3] - name_bbox[1]) + s(8)
    y += (label_bbox[3] - label_bbox[1]) + s(16)
    y += bar_h + s(16)
    y += meta_line_h
    if birthday:
        y += meta_line_h
    y += s(28)
    y += section_h + s(10) + line_h * len(bio_lines)
    if favorite_genres:
        y += s(28)
        y += section_h + s(10) + line_h * len(genres_lines)
    y += s(28)
    y += section_h + s(10) + line_h * len(books_lines)
    y += s(30)
    H = y

    # --- draw for real ---
    banner = _base_canvas(W, banner_h, RANK_BG, background_bytes)
    img = Image.new("RGB", (W, H), RANK_BG)
    img.paste(banner, (0, 0))
    draw = ImageDraw.Draw(img)

    avatar = _circular_avatar(avatar_bytes, avatar_size, ring_width=s(5), ring_color=accent)
    avatar_x = (W - avatar_size) // 2
    avatar_y = avatar_top_padding
    img.paste(avatar, (avatar_x, avatar_y), avatar)

    cy = avatar_y + avatar_size + s(20)
    name_w = name_bbox[2] - name_bbox[0]
    draw.text(((W - name_w) // 2, cy - name_bbox[1]), name, font=name_font, fill=accent)
    cy += (name_bbox[3] - name_bbox[1]) + s(8)

    label_w = label_bbox[2] - label_bbox[0]
    draw.text(((W - label_w) // 2, cy - label_bbox[1]), "LIBRARY CARD", font=label_font, fill=WHITE)
    cy += (label_bbox[3] - label_bbox[1]) + s(16)

    bar_box = (pad_x, cy, W - pad_x, cy + bar_h)
    draw.rounded_rectangle(bar_box, radius=bar_h // 2, fill=accent)
    rank_text = str(rank) if rank else "Unranked"
    stat_text = f"LEVEL: {level}   |   RANK: {rank_text}   |   GG: {total_gg:,}"
    stat_bbox = draw.textbbox((0, 0), stat_text, font=bar_font)
    stat_w = stat_bbox[2] - stat_bbox[0]
    stat_h = stat_bbox[3] - stat_bbox[1]
    draw.text(
        ((W - stat_w) // 2, cy + bar_h // 2 - stat_h // 2 - stat_bbox[1]),
        stat_text, font=bar_font, fill=RANK_BG
    )
    cy += bar_h + s(16)

    since_text = f"MEMBER SINCE: {member_since}"
    since_w = draw.textbbox((0, 0), since_text, font=meta_font)[2]
    draw.text(((W - since_w) // 2, cy), since_text, font=meta_font, fill=MUTED)
    cy += meta_line_h
    if birthday:
        bday_text = f"BIRTHDAY: {birthday}"
        bday_w = draw.textbbox((0, 0), bday_text, font=meta_font)[2]
        draw.text(((W - bday_w) // 2, cy), bday_text, font=meta_font, fill=MUTED)
        cy += meta_line_h

    def _centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((W - w) // 2, y), text, font=font, fill=fill)

    cy += s(28)
    _centered(cy, "PATRON SUMMARY", section_font, accent)
    cy += section_h + s(10)
    for line in bio_lines:
        _centered(cy, line, body_font, bio_color)
        cy += line_h

    if favorite_genres:
        cy += s(28)
        _centered(cy, "FAVORITE GENRES", section_font, accent)
        cy += section_h + s(10)
        for line in genres_lines:
            _centered(cy, line, body_font, WHITE)
            cy += line_h

    cy += s(28)
    _centered(cy, "BOOKS CHECKED OUT", section_font, accent)
    cy += section_h + s(10)
    for line in books_lines:
        _centered(cy, line, body_font, books_color)
        cy += line_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
