"""Shrinks an uploaded image so Discord will take it as an emoji (256 KB max; 128 px is plenty)."""
import io

from PIL import Image, ImageSequence

EMOJI_MAX_BYTES = 256 * 1024
_TARGET_BYTES = 250 * 1024          # a little headroom under the limit
_SIZES = (128, 96, 72, 56, 40, 32)  # longest side, tried in order until it fits
_MAX_FRAMES = 200                   # more than this and a GIF can't reasonably fit


def _fit(img: Image.Image, longest: int) -> Image.Image:
    out = img.copy()
    out.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return out


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _encode_gif(frames: list[Image.Image], durations: list[int]) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   disposal=2, optimize=True)
    return buf.getvalue()


def compress_emoji(data: bytes) -> tuple[bytes, str]:
    """Returns (image bytes small enough for an emoji, 'png' or 'gif'). Animated images stay animated.
    Raises ValueError if the file isn't an image or can't be made small enough."""
    try:
        img = Image.open(io.BytesIO(data))
        if not getattr(img, "is_animated", False):
            img.load()
    except Exception as e:                       # not an image, truncated, decompression bomb, ...
        raise ValueError("couldn't read that image") from e

    if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
        if img.n_frames > _MAX_FRAMES:
            raise ValueError(f"that animation has {img.n_frames} frames, which is too many to fit")
        frames, durations = [], []
        for frame in ImageSequence.Iterator(img):
            durations.append(frame.info.get("duration", 80) or 80)
            frames.append(frame.convert("RGBA"))
        for longest in _SIZES:
            out = _encode_gif([_fit(f, longest) for f in frames], durations)
            if len(out) <= _TARGET_BYTES:
                return out, "gif"
        raise ValueError("that animation is too long or detailed to fit, even at the smallest size")

    base = img.convert("RGBA")
    for longest in _SIZES:
        small = _fit(base, longest)
        out = _encode_png(small)
        if len(out) <= _TARGET_BYTES:
            return out, "png"
        out = _encode_png(small.quantize(colors=256, method=Image.Quantize.FASTOCTREE))
        if len(out) <= _TARGET_BYTES:
            return out, "png"
    raise ValueError("that image is too detailed to fit, even at the smallest size")
