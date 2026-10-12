"""Measured narration placement and source sound; no external APIs."""
import array
import copy
import math
import shutil
import subprocess

import pytest

import youtube_shorts_bot as legacy
from movie_analysis.core import MovieConfig
from movie_analysis.media import enable_local_tools, duration, probe
from movie_analysis.timing import align_narration, narration_schedule
from movie_analysis.review import validate_review_script
from movie_analysis.narration import validate_context
from test_movie_analysis import FakeVideoAI, claim


def fixture():
    script = FakeVideoAI().generate("script")
    claims = {f"s{i}_c": {**claim(f"s{i}_c"), "start": start, "end": start + 2}
              for i, start in enumerate((0, 15, 30))}
    for i, beat in enumerate(script["beats"]):
        beat["visual_claim_id"] = f"s{i}_c"
        beat["sentences"][0]["claim_ids"] = [f"s{i}_c"]
    return script, claims


def test_measured_voice_anchors_and_final_take_are_near_source_end():
    script, claims = fixture()
    slots = narration_schedule(script["beats"], claims, [8, 8, 8], 45)
    assert slots[0]["start"] == 0 and slots[1]["start"] == 15
    assert slots[2]["start"] > 35 and 44 < slots[2]["end"] <= 45
    assert all(.9 <= s["tempo"] <= 1.2 for s in slots)
    assert all(a["end"] <= b["start"] for a, b in zip(slots, slots[1:]))
    assert slots[2]["start"] > sum([8, 8])  # no early concatenated ending


def test_repeated_anchors_share_a_window_and_long_narration_is_supported():
    script, claims = fixture()
    for beat in script["beats"]:
        beat["visual_claim_id"] = "s0_c"
    slots = narration_schedule(script["beats"], claims, [80, 80, 80], 300)
    assert slots[1]["start"] > slots[0]["end"]
    assert 296 < slots[-1]["end"] <= 300
    assert sum(s["end"] - s["start"] for s in slots) > 180


def test_overlong_beat_and_out_of_order_events_are_not_silently_moved():
    script, claims = fixture()
    with pytest.raises(legacy.BotError, match="cảnh chỉ có"):
        narration_schedule(script["beats"], claims, [30, 8, 8], 45)
    claims["s1_c"]["start"] = 40
    with pytest.raises(legacy.BotError, match="thứ tự"):
        narration_schedule(script["beats"], claims, [8, 8, 8], 45)
    with pytest.raises(legacy.BotError, match="thứ tự thời gian"):
        validate_review_script(copy.deepcopy(script), {"accepted": list(claims.values())},
                               validate_context({}), 45, 45)


@pytest.mark.parametrize("value", ["0", "-1", "1.1", "nan"])
def test_original_audio_volume_rejects_muting_or_invalid_amplification(monkeypatch, value):
    monkeypatch.setenv("MOVIE_SOURCE_AUDIO_VOLUME", value)
    with pytest.raises(legacy.BotError):
        MovieConfig.from_env()


def amplitude(path, timestamp, frequency):
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", str(path),
                             "-t", "0.5", "-ar", "16000", "-ac", "1", "-f", "f32le", "-"],
                            capture_output=True, check=True, timeout=30)
    samples = array.array("f", result.stdout)
    real = sum(v * math.cos(2 * math.pi * frequency * i / 16000) for i, v in enumerate(samples))
    imag = sum(v * math.sin(2 * math.pi * frequency * i / 16000) for i, v in enumerate(samples))
    return 2 * math.hypot(real, imag) / len(samples)


def test_real_ffmpeg_places_voices_at_story_times_with_natural_pauses(tmp_path):
    enable_local_tools()
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    script, claims = fixture()
    paths = []
    for i, frequency in enumerate((440, 660, 880)):
        path = tmp_path / f"voice-{i}.wav"
        legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=8", str(path)], timeout_seconds=30)
        paths.append(path)
    slots = narration_schedule(script["beats"], claims, [duration(p) for p in paths], 45)
    mixed = tmp_path / "aligned.m4a"
    align_narration(paths, slots, mixed, 45, legacy.run)
    assert duration(mixed) == pytest.approx(45, abs=.05)
    for slot, frequency in zip(slots, (440, 660, 880)):
        # atempo preserves pitch, including during the final scene.
        assert amplitude(mixed, slot["start"] + 1, frequency) > .08
    assert amplitude(mixed, 12, 440) < .001
    assert amplitude(mixed, 28, 660) < .001


def test_continuous_writer_uses_full_source_budget_and_audio_change_reuses_text(tmp_path, monkeypatch):
    from dataclasses import replace
    from movie_analysis.pipeline import run_movie
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic media fixture")
    monkeypatch.setattr(legacy, "require_tools", lambda: None)
    monkeypatch.setattr("movie_analysis.pipeline.inspect_source", lambda *_: {"duration": 255.65, "has_audio": True})
    scene = {"id": "s001", "start": 0, "end": 255.65, "fast_action": False, "shot_cuts": []}
    monkeypatch.setattr("movie_analysis.pipeline.detect_scenes", lambda *_: [scene])
    monkeypatch.setattr("movie_analysis.pipeline.build_scenes", lambda *_: [scene])
    class Writer(FakeVideoAI):
        def __init__(self):
            super().__init__()
            self.prompts = {}
        def generate(self, stage, *args, **kwargs):
            self.prompts[stage] = args[0]
            return super().generate(stage, *args, **kwargs)
    ai = Writer()
    cfg = MovieConfig(target_seconds=60)
    run_movie(source, tmp_path / "job", cfg, "owned", "Synthetic test", provider=ai, script_only=True)
    assert "256s" in ai.prompts["review_plan"]
    assert "255.650s" in ai.prompts["script"]
    assert "WHOLE duration" in ai.prompts["script_audit"]
    ai.calls.clear()
    run_movie(source, tmp_path / "job", replace(cfg, source_audio_volume=.45), "owned", "Synthetic test", provider=ai, script_only=True)
    assert ai.calls == []
