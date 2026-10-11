"""Verify movie branding placement and cached render changes with real FFmpeg, no APIs."""
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

import youtube_shorts_bot as legacy
from movie_analysis.branding import branding_overlay, fitted_title, footage_box
from movie_analysis.core import MovieConfig, read_json
from movie_analysis.pipeline import movie_settings, run_movie
from test_movie_analysis import FakeVideoAI, SyntheticTTS, synthetic_clip


@pytest.mark.parametrize("stream", [
    {"width": 1920, "height": 1080}, {"width": 1080, "height": 1920},
    {"width": 1000, "height": 1000}, {"width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]},
    {"width": 720, "height": 576, "sample_aspect_ratio": "16:15"},
])
def test_complete_footage_fits_with_space_for_logo_and_title(stream):
    box = footage_box(stream)
    assert box["width"] <= 1080 and box["height"] <= 960
    assert box["top"] >= 480
    assert box["top"] + box["height"] <= 1440
    assert box["width"] % 2 == box["height"] % 2 == 0


def test_long_vietnamese_titles_and_literal_filter_characters_fit():
    for title in ("Giáo sư X: Ngày tận thế — Cái kết bất ngờ", "W" * 200, r"Phim 'A:B' [x]; {y} \\ test"):
        lines, font, line_height = fitted_title(title, 900, 104)
        assert len(lines) * line_height <= 104
        assert all(font.getlength(line) <= 900 for line in lines)
        assert "".join(lines).replace(" ", "") == title.replace(" ", "")


def test_blank_title_is_omitted_and_transparent_logo_is_rejected(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (10, 10), "white").save(logo)
    box = footage_box({"width": 1920, "height": 1080})
    layout = branding_overlay(tmp_path / "overlay.png", logo, "", box, MovieConfig())
    assert layout["title"] is None
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(logo)
    with pytest.raises(legacy.BotError, match="trong suốt"):
        branding_overlay(tmp_path / "bad.png", logo, "", box, MovieConfig())


def test_movie_can_choose_its_own_logo_without_changing_documentary_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERLAY_LOGO_FILE", "overlay-logo.png")
    monkeypatch.setenv("MOVIE_LOGO_FILE", str(tmp_path / "movie-only.png"))
    assert movie_settings().overlay_logo == tmp_path / "movie-only.png"


def test_black_bands_movie_title_and_small_logo_render_without_new_ai_or_tts(synthetic_clip, tmp_path, monkeypatch):
    logo = tmp_path / "movie-logo.png"
    image = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((100, 75, 299, 124), fill="magenta")
    image.save(logo)
    monkeypatch.setenv("MOVIE_LOGO_FILE", str(logo))
    ai, tts = FakeVideoAI(), SyntheticTTS()
    job = tmp_path / "job"
    cfg = MovieConfig()
    output = run_movie(synthetic_clip, job, cfg, "owned", "Synthetic test source", movie_title="Synthetic Film", provider=ai, tts=tts)
    layout = read_json(job / "movie_branding.json")
    assert layout["background"] == "#000000" and layout["movie_title"] == "Synthetic Film"
    assert layout["logo"]["width"] == 70
    assert layout["logo"]["height"] <= 48
    assert layout["captions_burned_in"] is False
    assert layout["logo"]["top"] + layout["logo"]["height"] + cfg.logo_gap == layout["footage"]["top"]
    assert layout["title"]["top"] > layout["footage"]["top"] + layout["footage"]["height"]
    assert layout["title"]["bottom"] <= 1580
    frame = tmp_path / "frame.png"
    legacy.run(["ffmpeg", "-y", "-ss", "1", "-i", str(output), "-frames:v", "1", str(frame)], timeout_seconds=30)
    rendered = Image.open(frame).convert("RGB")
    assert max(rendered.getpixel((1000, 100))) < 4  # old logo corner is now black
    assert max(rendered.getpixel((10, 1800))) < 4
    # Narration is audible but its captions are absent from the lower black band.
    assert max(rendered.crop((80, 1600, 1000, 1780)).getextrema()[channel][1]
               for channel in range(3)) < 4
    position = layout["logo"]
    r, g, b = rendered.getpixel((position["left"] + position["width"] // 2, position["top"] + position["height"] // 2))
    assert r > 200 and b > 200 and g < 30
    title_pixels = rendered.crop((90, layout["title"]["top"], 990, layout["title"]["bottom"]))
    assert any(min(title_pixels.getpixel((x, y))) > 200
               for y in range(title_pixels.height) for x in range(title_pixels.width))
    # A logo-size edit invalidates only the render; analysis/script/voice stay cached.
    resumed = FakeVideoAI()
    run_movie(synthetic_clip, job, replace(cfg, logo_width=35), "owned", "Synthetic test source",
              movie_title="Synthetic Film", provider=resumed, tts=tts)
    assert read_json(job / "movie_branding.json")["logo"]["width"] == 35
    assert resumed.calls == [] and resumed.uploads == 0 and tts.calls == 3
