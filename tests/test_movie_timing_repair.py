"""Measured audio repair, factual gates and cached resume without real APIs."""
import copy

import pytest

import youtube_shorts_bot as legacy
from movie_analysis.core import Cache, MovieConfig, read_json
from movie_analysis.narration import validate_context
from movie_analysis.review import validate_review_script
from movie_analysis.timing import NarrationTimingError, narration_schedule
from movie_analysis.timing_repair import repair_narration_timing, validate_timing_repair
from test_movie_analysis import FakeVideoAI, SyntheticTTS, claim, verdict


def fixture(source_seconds=45):
    claims = [{**claim(f"s001_c{i + 1}"), "start": start, "end": start + 2}
              for i, start in enumerate((0, 15, source_seconds - 10))]
    evidence = {"accepted": claims}
    context = validate_context({"title": "Synthetic Film"})
    script = FakeVideoAI().generate("script")
    for beat, c in zip(script["beats"], claims):
        beat["visual_claim_id"] = c["id"]
        beat["sentences"][0]["claim_ids"] = [c["id"]]
    script = validate_review_script(script, evidence, context, source_seconds, source_seconds)
    with pytest.raises(NarrationTimingError) as error:
        narration_schedule(script["beats"], {c["id"]: c for c in claims}, [2, 2, 21.1], source_seconds)
    return script, evidence, context, error.value.feedback


def corrected(script):
    candidate = copy.deepcopy(script)
    candidate["beats"][2]["sentences"][0]["text"] = "Đỏ nóng thật."
    return candidate


@pytest.mark.parametrize("seconds", [45, 222.5])
def test_measured_error_identifies_only_overlong_closing_and_safe_word_cap(seconds):
    script, evidence, context, timing = fixture(seconds)
    assert timing["available_seconds"] == 10
    assert timing["window_end"] == seconds
    assert timing["beats"] == [{"id": "b03", "visual_claim_id": "s001_c3",
                               "measured_voice_seconds": 21.1, "word_count": 8, "maximum_words": 3}]
    candidate = validate_timing_repair(corrected(script), script, timing, evidence, context, seconds, seconds)
    assert candidate["beats"][:2] == script["beats"][:2]
    slots = narration_schedule(candidate["beats"], {c["id"]: c for c in evidence["accepted"]}, [2, 2, 6], seconds)
    assert seconds - 10 <= slots[-1]["start"] < seconds
    assert seconds - 1 < slots[-1]["end"] <= seconds


@pytest.mark.parametrize("change", ["unchanged", "other_beat", "move_scene", "extra_beat"])
def test_duration_repair_cannot_ignore_word_cap_or_change_unaffected_story(change):
    script, evidence, context, timing = fixture()
    candidate = corrected(script)
    if change == "unchanged":
        candidate = copy.deepcopy(script)
    elif change == "other_beat":
        candidate["beats"][0]["sentences"][0]["text"] += " Màu đỏ."
    elif change == "move_scene":
        candidate["beats"][2]["visual_claim_id"] = "s001_c2"
        candidate["beats"][2]["sentences"][0]["claim_ids"] = ["s001_c2"]
    else:
        extra = copy.deepcopy(candidate["beats"][1])
        extra["id"] = "b03"
        candidate["beats"].insert(2, extra)
        candidate["beats"][-1]["id"] = "b04"
    with pytest.raises(legacy.BotError):
        validate_timing_repair(candidate, script, timing, evidence, context, 45, 45)


def test_timing_repair_resumes_audit_transport_failure_without_rewriting(tmp_path):
    script, evidence, context, timing = fixture()
    calls = []
    failing = True

    def ask(stage, prompt):
        nonlocal failing
        calls.append(stage)
        if stage == "script":
            assert "maximum_words" in prompt and "Preserve every other beat verbatim" in prompt
            return corrected(script)
        if failing:
            failing = False
            raise legacy.BotError("Synthetic HTTP 503")
        return FakeVideoAI().generate("script_audit")

    def run():
        return repair_narration_timing(tmp_path, Cache(tmp_path), "script-key", script, timing, evidence,
                                       context, 45, 45, {}, ask, lambda _: None)

    with pytest.raises(legacy.BotError, match="503"):
        run()
    assert calls == ["script", "script_audit"]
    assert not (tmp_path / "script_approved.json").exists()
    calls.clear()
    result = run()
    assert calls == ["script_audit"]
    assert result["beats"][2]["voiceover"] == "Đỏ nóng thật."
    assert read_json(tmp_path / "script_approved.json")["script"] == result
    calls.clear()
    assert run() == result and calls == []


def test_timing_repair_is_bounded_and_cannot_skip_factual_audit(tmp_path):
    script, evidence, context, timing = fixture()
    calls = []

    def ask(stage, _prompt):
        calls.append(stage)
        if stage == "script":
            return corrected(script)
        audit = FakeVideoAI().generate("script_audit")
        audit["reviews"][-1].update(supported=False, reason="Synthetic unsupported conclusion")
        return audit

    for _ in range(2):
        with pytest.raises(legacy.BotError, match="không có bằng chứng"):
            repair_narration_timing(tmp_path, Cache(tmp_path), "script-key", script, timing, evidence,
                                    context, 45, 45, {}, ask, lambda _: None)
    # Two bounded revisions on the first run; the failed responses are reused
    # on resume instead of silently charging for the same rejected drafts.
    assert calls == ["script", "script_audit", "script", "script_audit"]
    assert not (tmp_path / "script_approved.json").exists()


def test_real_render_resume_shortens_only_failed_beat_and_reuses_other_audio(tmp_path, monkeypatch):
    import movie_analysis.pipeline as pipeline
    from movie_analysis.media import duration, enable_local_tools
    enable_local_tools()
    source = tmp_path / "source.mp4"
    legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=1:d=45",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=45", "-c:v", "libx264",
                "-c:a", "aac", "-shortest", str(source)], timeout_seconds=60)
    monkeypatch.setattr(legacy, "ensure_dejavu_font", lambda: None)
    monkeypatch.setattr(legacy, "CAPTION_FONTS_DIR", legacy.ROOT / "fonts")
    directory = tmp_path / "job"
    original, evidence, _context, _timing = fixture()

    class AI(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            if stage == "timing_rewrite":
                self.calls.append((stage, kwargs))
                return {"edits": {"b03": {"words": ["Đỏ", "nóng", "thật."], "kind": "joke", "claim_ids": ["s001_c3"]}}}
            if stage.startswith("deep:"):
                self.calls.append((stage, kwargs))
                return {"claims": copy.deepcopy(evidence["accepted"])}
            if stage.startswith(("verify:", "recheck:")):
                self.calls.append((stage, kwargs))
                return {"verdicts": [verdict(c["id"]) for c in evidence["accepted"]]}
            value = super().generate(stage, *args, **kwargs)
            if stage == "script":
                return copy.deepcopy(original)  # two long rewrites force the bounded compact fallback
            return value

    class TTS(SyntheticTTS):
        def speech(self, text, path):
            self.calls += 1
            seconds = 21.1 if text == original["beats"][2]["voiceover"] else 6 if text == "Đỏ nóng thật." else 2
            legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-c:a", "libmp3lame", str(path)], timeout_seconds=30)

    def old_stop(*_args, **_kwargs):
        raise legacy.BotError("Synthetic old render stopped")

    monkeypatch.setattr(pipeline, "repair_narration_timing", old_stop)
    tts = TTS()
    config = MovieConfig(max_scene_seconds=60)
    with pytest.raises(legacy.BotError, match="old render"):
        pipeline.run_movie(source, directory, config, "owned", "Synthetic source", provider=AI(), tts=tts,
                           movie_title="Synthetic Film")
    assert tts.calls == 3
    saved_audio = [read_json(directory / f"b{i:02d}_voice.json") for i in (1, 2)]
    monkeypatch.setattr(pipeline, "repair_narration_timing", repair_narration_timing)
    resumed = AI()
    output = pipeline.run_movie(source, directory, config, "owned", "Synthetic source", provider=resumed,
                                tts=tts, movie_title="Synthetic Film")
    assert [s for s, _ in resumed.calls] == ["timing_rewrite", "script_audit"] and resumed.uploads == 0
    assert tts.calls == 4
    assert saved_audio == [read_json(directory / f"b{i:02d}_voice.json") for i in (1, 2)]
    assert read_json(directory / "quality.json")["passed"]
    assert duration(output) == pytest.approx(45, abs=.1)
    closing = read_json(directory / "timeline.json")[-1]
    assert 35 <= closing["start"] < 45 and 44 < closing["end"] <= 45
    assert read_json(directory / "render.json")["held_end_seconds"] == 0
    assert read_json(directory / "render.json")["source_audio_volume"] == .30
    assert not (directory / "narration_timing_error.json").exists()
    cached = AI()
    pipeline.run_movie(source, directory, config, "owned", "Synthetic source", provider=cached, tts=tts,
                       movie_title="Synthetic Film")
    assert cached.calls == [] and cached.uploads == 0 and tts.calls == 4
