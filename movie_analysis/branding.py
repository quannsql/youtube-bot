"""Movie-only branding: black bands, a compact logo above footage, and a literal film title."""
from __future__ import annotations

from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont, ImageOps

import youtube_shorts_bot as legacy
from .core import write_json

WIDTH, HEIGHT, VIEWPORT_HEIGHT = 1080, 1920, 960
BRANDING_VERSION = "movie-branding-v2"


def footage_box(stream):
    """Fit the full displayed frame into the central band, with room for labels."""
    width, height = stream["width"], stream["height"]
    try:
        numerator, denominator = str(stream.get("sample_aspect_ratio", "1:1")).replace("/", ":").split(":")
        aspect = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        aspect = 1
    if math.isfinite(aspect) and aspect > 0:
        width *= aspect
    # FFmpeg autorotates phone clips. Match that displayed shape for positioning.
    rotation = next((s.get("rotation", 0) for s in stream.get("side_data_list", []) if "rotation" in s),
                    stream.get("tags", {}).get("rotate", 0))
    if abs(float(rotation)) % 180 == 90:
        width, height = height, width
    scale = min(WIDTH / width, VIEWPORT_HEIGHT / height)
    w, h = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
    return {"left": (WIDTH - w) // 2, "top": (HEIGHT - h) // 2, "width": w, "height": h}


def fitted_title(text, max_width, max_height):
    """Word-wrap Vietnamese text, shrinking to fit; long words wrap by character."""
    font_file = legacy.ROOT / "fonts" / "DejaVuSans-Bold.ttf"
    if not font_file.is_file():
        font_file = legacy.ROOT / "fonts" / "DejaVuSans.ttf"
    for size in range(48, 11, -2):
        font = ImageFont.truetype(str(font_file), size)
        lines, line = [], ""
        for word in text.split():
            candidate = (line + " " + word).strip()
            if font.getlength(candidate) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            for char in word:
                if line and font.getlength(line + char) > max_width:
                    lines.append(line)
                    line = ""
                line += char
        if line:
            lines.append(line)
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + 8
        if len(lines) * line_height <= max_height:
            return lines, font, line_height
    raise legacy.BotError("Tên phim quá dài để hiển thị trong vùng dưới video; rút gọn tên phim/trailer.")


def branding_overlay(destination: Path, logo_path: Path, movie_title: str, box, config):
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    try:
        with Image.open(logo_path) as source:
            logo = ImageOps.exif_transpose(source).convert("RGBA")
            # Transparent margins belong to the asset, not the visible logo size.
            bounds = logo.getchannel("A").getbbox()
            if not bounds:
                raise legacy.BotError("Logo PNG hoàn toàn trong suốt.")
            logo = logo.crop(bounds)
            logo.thumbnail((min(config.logo_width, 360), min(48, max(1, box["top"] - config.logo_gap))), Image.Resampling.LANCZOS)
    except (OSError, ValueError) as exc:
        raise legacy.BotError("Không đọc được file logo cho review phim.") from exc
    x, y = WIDTH - 36 - logo.width, max(0, box["top"] - config.logo_gap - logo.height)
    overlay.alpha_composite(logo, (x, y))
    title = " ".join(movie_title.split())
    title_box = None
    if title:
        top = box["top"] + box["height"] + 36
        lines, font, line_height = fitted_title(title, 900, min(180, 1580 - top))
        draw = ImageDraw.Draw(overlay)
        for i, line in enumerate(lines):
            draw.text((WIDTH / 2, top + i * line_height), line, font=font, fill="white", anchor="mt")
        title_box = {"top": top, "bottom": top + len(lines) * line_height, "lines": lines}
    overlay.save(destination)
    layout = {"version": BRANDING_VERSION, "background": "#000000", "footage": box,
              "logo": {"left": x, "top": y, "width": logo.width, "height": logo.height},
              "movie_title": title, "title": title_box, "captions_burned_in": False}
    write_json(destination.with_suffix(".json"), layout)
    return layout


def mux_movie(visuals, narration, overlay, output, seconds):
    # Film title is painted in a PNG, so punctuation/backslashes/user content
    # never enter the FFmpeg filter language. Narration captions are export-only.
    filters = ("[0:v][2:v]overlay=0:0:format=auto[v];"
               f"[1:a]apad=pad_dur={seconds}[a]")
    legacy.run(["ffmpeg", "-y", "-i", str(visuals), "-i", str(narration),
                "-loop", "1", "-i", str(overlay), "-filter_complex", filters,
                "-map", "[v]", "-map", "[a]", "-t", str(seconds),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-movflags", "+faststart", str(output)], timeout_seconds=max(600, seconds * 10))
