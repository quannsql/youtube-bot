"""Batch measured repairs and an audited outro when the final shot is too short."""
import copy

import pytest

import youtube_shorts_bot as legacy
from movie_analysis.core import Cache, MovieConfig, read_json
from movie_analysis.narration import validate_context
from movie_analysis.review import validate_review_script
from movie_analysis.timing import NarrationTimingError, narration_schedule
from movie_analysis.timing_repair import assemble_compact_repair, repair_narration_timing, validate_timing_repair
from test_movie_analysis import FakeVideoAI, claim, verdict, SyntheticTTS


def fixture(source_seconds=255.65, starts=(0, 80, 110.1, 204.75, 255.15)):
    script = FakeVideoAI().generate("script")
    while len(script["beats"]) < 5:
        script["beats"].insert(-1, copy.deepcopy(script["beats"][1]))
    claims = [{**claim(f"s001_c{i + 1}"), "start": start, "end": min(start + 2, source_seconds)}
              for i, start in enumerate(starts)]
    for i, (beat, c) in enumerate(zip(script["beats"], claims), 1):
        beat["id"] = f"b{i:02d}"
        beat["role"] = "opening" if i == 1 else "closing" if i == 5 else "development"
        beat["visual_claim_id"] = c["id"]
        beat["sentences"][0]["claim_ids"] = [c["id"]]
    for i, words in ((1, 70), (3, 82), (4, 70)):
        script["beats"][i]["sentences"][0]["text"] = " ".join([{1: "nền", 3: "đỏ", 4: "rực"}[i]] * words) + "."
    evidence, context = {"accepted": claims}, validate_context({"title": "Synthetic Film"})
    script = validate_review_script(script, evidence, context, source_seconds, source_seconds)
    return script, evidence, context


def timing(script, evidence, durations, seconds):
    with pytest.raises(NarrationTimingError) as exc:
        narration_schedule(script["beats"], {c["id"]: c for c in evidence["accepted"]}, durations, seconds)
    return exc.value.feedback


def audit(script):
    result = FakeVideoAI().generate("script_audit")
    result["reviews"] = [{"id": f'{b["id"]}:{i}', "supported": True, "reason": "Synthetic audit"}
                         for b in script["beats"] for i, _ in enumerate(b["sentences"])]
    return result


def edits_for(feedback, script):
    return {"edits": {detail["id"]: {"words": ["Màu", "đỏ", "càng", "căng."], "kind": "inference",
        "claim_ids": [detail.get("replacement_visual_claim_id", detail["visual_claim_id"])]}
        for detail in feedback["beats"]}}


def test_all_overlong_windows_are_returned_together_and_closing_has_a_real_budget():
    script, evidence, _ = fixture()
    feedback = timing(script, evidence, [15, 40.8, 20, 79.4, 42.3], 255.65)
    assert [b["id"] for b in feedback["beats"]] == ["b02", "b04", "b05"]
    assert len(feedback["windows"]) == 3
    assert feedback["windows"][0]["available_seconds"] == pytest.approx(30.1)
    assert feedback["beats"][0]["maximum_words"] == 47
    closing = feedback["beats"][-1]
    assert closing["rewrite_as_closing_summary"] and closing["replacement_visual_claim_id"] == "s001_c4"
    assert "s001_c5" not in closing["allowed_claim_ids"]
    assert feedback["windows"][-1]["available_seconds"] == pytest.approx(20)
    assert closing["maximum_words"] > 1


def test_three_failing_beats_are_repaired_and_audited_in_one_pair_of_requests(tmp_path):
    script, evidence, context = fixture()
    feedback = timing(script, evidence, [15, 40.8, 20, 79.4, 42.3], 255.65)
    edits = edits_for(feedback, script)
    candidate = assemble_compact_repair(edits, script, feedback, evidence, context, 256, 255.65)
    calls = []

    def compact(stage, prompt, limits):
        calls.append(stage)
        assert set(limits) == {"b02", "b04", "b05"} and "future final shot" in prompt
        return edits

    def ask(stage, _prompt):
        assert stage == "script_audit"  # no generic full-script rewrite
        assert "MEASURED OUTRO AUDIT" in _prompt
        calls.append(stage)
        return audit(candidate)

    for _ in range(2):
        result = repair_narration_timing(tmp_path, Cache(tmp_path), "key", script, feedback, evidence, context,
                                        256, 255.65, {}, ask, lambda _: None, ask_compact=compact)
        assert result == candidate
    assert calls == ["timing_rewrite", "script_audit"]
    assert result["beats"][0] == script["beats"][0] and result["beats"][2] == script["beats"][2]
    assert result["beats"][-1]["visual_claim_id"] == "s001_c4"
    slots = narration_schedule(result["beats"], {c["id"]: c for c in evidence["accepted"]}, [15, 4, 20, 4, 4], 255.65)
    assert slots[-1]["start"] > 245 and slots[-1]["end"] <= 255.65


@pytest.mark.parametrize("unsafe", ["future_ref", "fact_kind", "wrong_anchor"])
def test_outro_rewrite_cannot_reveal_a_future_final_event_or_move_other_visuals(unsafe):
    script, evidence, context = fixture()
    feedback = timing(script, evidence, [15, 27, 20, 30, 42.3], 255.65)
    edits = edits_for(feedback, script)
    if unsafe == "future_ref":
        edits["edits"]["b05"]["claim_ids"].append("s001_c5")
    elif unsafe == "fact_kind":
        edits["edits"]["b05"]["kind"] = "fact"
    else:
        candidate = assemble_compact_repair(edits, script, feedback, evidence, context, 256, 255.65)
        candidate["beats"][2]["visual_claim_id"] = "s001_c2"
        candidate["beats"][2]["sentences"][0]["claim_ids"].append("s001_c2")
        with pytest.raises(legacy.BotError):
            validate_timing_repair(candidate, script, feedback, evidence, context, 256, 255.65)
        return
    with pytest.raises(legacy.BotError):
        assemble_compact_repair(edits, script, feedback, evidence, context, 256, 255.65)


def test_exact_deployed_state_has_already_shortened_b02_b04_and_only_rewrites_late_closing():
    script, evidence, context = fixture()
    script["beats"][1]["sentences"][0]["text"] = " ".join(["đỏ"] * 47)
    script["beats"][3]["sentences"][0]["text"] = " ".join(["đỏ"] * 47)
    script = validate_review_script(script, evidence, context, 256, 255.65)
    feedback = timing(script, evidence, [15, 27, 20, 30, 42.3], 255.65)
    assert [b["id"] for b in feedback["beats"]] == ["b05"]
    candidate = assemble_compact_repair(edits_for(feedback, script), script, feedback, evidence, context, 256, 255.65)
    assert candidate["beats"][:-1] == script["beats"][:-1]
    slots = narration_schedule(candidate["beats"], {c["id"]: c for c in evidence["accepted"]}, [15, 27, 20, 30, 4], 255.65)
    assert len(slots) == 5 and slots[-1]["start"] < 255.15


def test_real_full_clip_batch_repair_preserves_unchanged_tts_and_cached_resume(tmp_path, monkeypatch):
    import movie_analysis.pipeline as pipeline
    from movie_analysis.media import duration, enable_local_tools
    enable_local_tools()
    source = tmp_path / "source.mp4"
    legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=1:d=60",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=60", "-c:v", "libx264",
                "-c:a", "aac", "-shortest", str(source)], timeout_seconds=60)
    monkeypatch.setattr(legacy, "ensure_dejavu_font", lambda: None)
    monkeypatch.setattr(legacy, "CAPTION_FONTS_DIR", legacy.ROOT / "fonts")
    script, evidence, _ = fixture(60, (0, 10, 20, 40, 59.5))
    feedback = timing(script, evidence, [2, 15, 2, 18, 12], 60)
    edits = edits_for(feedback, script)
    directory = tmp_path / "job"

    class AI(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            if stage.startswith("deep:"):
                self.calls.append((stage, kwargs))
                return {"claims": copy.deepcopy(evidence["accepted"])}
            if stage.startswith(("verify:", "recheck:")):
                self.calls.append((stage, kwargs))
                return {"verdicts": [verdict(c["id"]) for c in evidence["accepted"]]}
            if stage == "timing_rewrite":
                self.calls.append((stage, kwargs))
                assert "b02" in args[0] and "b04" in args[0] and "b05" in args[0]
                return edits
            value = super().generate(stage, *args, **kwargs)
            if stage == "script":
                return copy.deepcopy(script)
            if stage == "script_audit":
                candidate = read_json(directory / "timing_compact_1.json")
                return audit(assemble_compact_repair(candidate["data"], script, feedback, evidence,
                    validate_context({"title": "Synthetic Film"}), 60, 60) if candidate else script)
            return value

    class TTS(SyntheticTTS):
        def speech(self, text, path):
            self.calls += 1
            seconds = 3
            for beat, measured in zip(script["beats"], (2, 15, 2, 18, 12)):
                if text == beat["voiceover"]:
                    seconds = measured
            legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-c:a", "libmp3lame", str(path)], timeout_seconds=30)

    def stop(*_args, **_kwargs):
        raise legacy.BotError("Synthetic old stop")

    monkeypatch.setattr(pipeline, "repair_narration_timing", stop)
    tts, cfg = TTS(), MovieConfig(max_scene_seconds=60)
    with pytest.raises(legacy.BotError, match="old stop"):
        pipeline.run_movie(source, directory, cfg, "owned", "Synthetic media", provider=AI(), tts=tts,
                           movie_title="Synthetic Film")
    assert tts.calls == 5
    saved = [read_json(directory / f"{bid}_voice.json") for bid in ("b01", "b03")]
    monkeypatch.setattr(pipeline, "repair_narration_timing", repair_narration_timing)
    resumed = AI()
    output = pipeline.run_movie(source, directory, cfg, "owned", "Synthetic media", provider=resumed, tts=tts,
                                movie_title="Synthetic Film")
    assert [s for s, _ in resumed.calls] == ["timing_rewrite", "script_audit"] and resumed.uploads == 0
    assert tts.calls == 8 and saved == [read_json(directory / f"{bid}_voice.json") for bid in ("b01", "b03")]
    report = read_json(directory / "render.json")
    assert duration(output) == pytest.approx(60, abs=.1) and report["held_end_seconds"] == 0
    assert report["source_audio_volume"] == .3 and read_json(directory / "quality.json")["passed"]
    assert read_json(directory / "script_approved.json")["script"]["beats"][-1]["visual_claim_id"] == "s001_c4"
    assert read_json(directory / "timeline.json")[-1]["start"] > 55
    cached = AI()
    pipeline.run_movie(source, directory, cfg, "owned", "Synthetic media", provider=cached, tts=tts,
                       movie_title="Synthetic Film")
    assert cached.calls == [] and tts.calls == 8
