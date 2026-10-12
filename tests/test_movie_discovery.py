"""Automatic fictional-character discovery; all model results are test doubles."""
import copy
import json
from dataclasses import replace

import pytest

from youtube_shorts_bot import BotError
from movie_analysis.characters import (identity_frames, recognize_characters, recognize_character_chunks,
                                       validate_discovery)
from movie_analysis.core import Cache, MovieConfig, read_json
from movie_analysis.narration import validate_context
from test_movie_analysis import FakeVideoAI, Response, claim, synthetic_clip


def candidate(name="Batman", **changes):
    return {"name": name, "spoken_name": name, "claim_ids": [claim()["id"]], "confidence": .99,
            "cues": ["distinctive_costume", "setting"], "reason": "Synthetic fictional-character cues", **changes}


def test_auto_discovers_batman_superman_without_roster_and_only_passes_verified_names(tmp_path):
    context = validate_context({"title": "Synthetic Film"})
    original = copy.deepcopy(context)
    calls = []

    def ask(stage, prompt, **kwargs):
        calls.append(stage)
        if stage == "character_identity_discover":
            return {"matches": [candidate(), candidate("Superman")]}
        assert "UNVERIFIED hypotheses" in prompt
        assert len(kwargs["character_ids"]) == 2
        return {"verdicts": [{"id": i, "status": "supported", "confidence": .99,
                             "reason": "Synthetic independent costume check"} for i in kwargs["character_ids"]]}

    evidence = {"accepted": [claim()]}
    result = recognize_characters(Cache(tmp_path), tmp_path, "hash", MovieConfig(), evidence, context, ask)
    assert [p["name"] for p in result["recognized_characters"]] == ["Batman", "Superman"]
    assert all(f["source"] == "video_identity_verified" for f in result["facts"])
    assert result["characters"] == [] and context == original  # no invented background/roster persistence
    report = read_json(tmp_path / "characters.json")
    assert report["mode"] == "automatic" and report["status"] == "verified"
    calls.clear()
    assert recognize_characters(Cache(tmp_path), tmp_path, "hash", MovieConfig(), evidence, context, ask) == result
    assert calls == []


@pytest.mark.parametrize("changes,status", [
    ({"confidence": .8}, "supported"), ({"cues": ["setting", "relationship"]}, "supported"),
    ({}, "uncertain"), ({"cues": ["voice", "setting"]}, "supported"),
])
def test_unverified_discovery_never_becomes_writer_context(tmp_path, changes, status):
    context = validate_context({"title": "Batman vs Superman"})

    def ask(stage, _prompt, **kwargs):
        if stage == "character_identity_discover":
            return {"matches": [candidate(**changes)]}
        return {"verdicts": [{"id": kwargs["character_ids"][0], "status": status,
                             "confidence": .99, "reason": "Synthetic independent uncertainty"}]}

    result = recognize_characters(Cache(tmp_path), tmp_path, "hash", MovieConfig(identity_provider="openai"),
                                  {"accepted": [claim()]}, context, ask)
    assert result["facts"] == [] and result["recognized_characters"] == []
    report = read_json(tmp_path / "characters.json")
    assert report["status"] == "no_verified_matches"
    assert len(report["rejected"]) == 1 and report["rejected"][0]["rejection_reason"]


@pytest.mark.parametrize("matches", [
    [candidate(claim_ids=["ctx_title"])], [candidate(claim_ids=["missing_id"])],
    [candidate(name="")], [candidate(confidence=1.1)], [candidate(cues=["made_up_cue"])],
    [candidate(), candidate(name=" BATMAN ")], [candidate(str(i)) for i in range(11)],
])
def test_discovery_rejects_invalid_or_duplicate_unbounded_candidates(matches):
    with pytest.raises(BotError):
        validate_discovery({"matches": matches}, {"accepted": [claim()]})


def test_empty_discovery_is_saved_and_does_not_pay_for_verification(tmp_path):
    calls = []

    def ask(stage, _prompt, **_kwargs):
        calls.append(stage)
        assert stage == "character_identity_discover"
        return {"matches": []}

    for _ in range(2):
        result = recognize_characters(Cache(tmp_path), tmp_path, "hash", MovieConfig(), {"accepted": [claim()]},
                                      validate_context({}), ask)
        assert result["recognized_characters"] == []
    assert calls == ["character_identity_discover"]
    assert read_json(tmp_path / "characters.json")["status"] == "no_candidates"


def test_gpt_with_audio_only_evidence_reports_missing_frames_without_call(tmp_path):
    def blocked(*_args, **_kwargs):
        raise AssertionError("No visual evidence must not call GPT")

    recognize_characters(Cache(tmp_path), tmp_path, "hash", MovieConfig(identity_provider="openai"),
                         {"accepted": [{**claim(), "modality": "audio"}]}, validate_context({}), blocked)
    assert read_json(tmp_path / "characters.json")["status"] == "no_visual_evidence"


def test_discovery_transport_resume_keeps_first_pass(tmp_path):
    calls = []
    failing = True

    def ask(stage, _prompt, **kwargs):
        nonlocal failing
        calls.append(stage)
        if stage == "character_identity_discover":
            return {"matches": [candidate()]}
        if failing:
            failing = False
            raise BotError("Synthetic HTTP 503")
        return {"verdicts": [{"id": kwargs["character_ids"][0], "status": "supported",
                             "confidence": .99, "reason": "Synthetic independently confirmed"}]}

    def run():
        return recognize_characters(Cache(tmp_path), tmp_path, "hash", MovieConfig(), {"accepted": [claim()]},
                                    validate_context({}), ask)
    with pytest.raises(BotError, match="503"):
        run()
    calls.clear()
    assert run()["recognized_characters"][0]["name"] == "Batman"
    assert calls == ["character_identity_verify"]


def test_chunk_discovery_merges_names_but_does_not_extend_unverified_appearance(tmp_path):
    evidence = {"accepted": [claim(), {**claim("s002_c1"), "start": 50, "end": 52}]}
    windows = [{"id": "c001", "start": 0, "end": 45}, {"id": "c002", "start": 45, "end": 90}]

    def ask(stage, prompt, window, **kwargs):
        ref = "s001_c1" if window["id"] == "c001" else "s002_c1"
        other = "s002_c1" if window["id"] == "c001" else "s001_c1"
        assert ref in prompt and other not in prompt
        if stage == "character_identity_discover":
            return {"matches": [candidate(claim_ids=[ref])]}
        return {"verdicts": [{"id": kwargs["character_ids"][0],
            "status": "supported" if window["id"] == "c001" else "uncertain", "confidence": .99,
            "reason": "Synthetic window-specific verification"}]}

    result = recognize_character_chunks(tmp_path, "hash", MovieConfig(), evidence, validate_context({}),
                                         windows, ask, lambda _: None)
    assert result["recognized_characters"][0]["claim_ids"] == ["s001_c1"]
    assert result["facts"][-1]["claim_ids"] == ["s001_c1"]


def test_gpt_discovery_request_accepts_images_and_limits_only_claim_ids(tmp_path):
    from movie_analysis.openai_writer import OpenAIWriterClient
    from movie_analysis.provider import GeminiVideoClient

    class HTTP:
        payload = None
        def request(self, *_args, **kwargs):
            self.payload = kwargs["json"]
            return Response(200, {"status": "completed", "usage": {"total_tokens": 100}, "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"matches": []})}]}]})

    frame = tmp_path / "frame_001.000s.jpg"
    frame.write_bytes(b"synthetic JPEG transport fixture")
    http = HTTP()
    client = OpenAIWriterClient("fake-key", MovieConfig(), GeminiVideoClient("fake-key", MovieConfig(), tmp_path),
                               "gpt-5.4-mini", http)
    assert client.generate("character_identity_discover", "inspect", image_paths=[frame],
                           allowed_claim_ids=["s001_c1"], allowed_character_ids=["old_roster"]) == {"matches": []}
    props = http.payload["text"]["format"]["schema"]["properties"]["matches"]["items"]["properties"]
    assert props["claim_ids"]["items"]["enum"] == ["s001_c1"]
    assert "enum" not in props["name"] and "enum" not in props["reason"] and "character_id" not in props
    assert http.payload["input"][0]["content"][-1]["type"] == "input_image"


def test_both_audio_visual_claims_can_supply_real_identity_frames(synthetic_clip, tmp_path):
    paths = identity_frames(synthetic_clip, tmp_path, {"accepted": [{**claim(), "modality": "both"}]})
    assert len(paths) == 3 and all(p.is_file() and p.stat().st_size for p in paths)


def test_old_job_without_roster_resumes_with_gpt_names_and_cached_analysis(synthetic_clip, tmp_path, monkeypatch):
    import movie_analysis.pipeline as pipeline
    import movie_analysis.characters as characters
    directory = tmp_path / "job"
    cfg = MovieConfig(writer_provider="openai", identity_provider="openai", writer_model="gpt-5.4-mini")
    monkeypatch.setattr(pipeline, "recognize_characters", lambda _cache, _dir, _hash, _cfg, _evidence, context, _ask: context)
    pipeline.run_movie(synthetic_clip, directory, cfg, provider=FakeVideoAI(), text_provider=FakeVideoAI(),
                       script_only=True, movie_title="Synthetic Film")
    monkeypatch.setattr(pipeline, "recognize_characters", characters.recognize_characters)

    class Vision(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            if stage.startswith("character_identity"):
                self.calls.append((stage, kwargs))
                assert 1 <= len(kwargs["image_paths"]) <= 24 and all(p.is_file() for p in kwargs["image_paths"])
                if stage == "character_identity_discover":
                    return {"matches": [candidate(), candidate("Superman")]}
                return {"verdicts": [{"id": i, "status": "supported", "confidence": .99,
                                      "reason": "Synthetic independent recognition"} for i in kwargs["allowed_character_ids"]]}
            result = super().generate(stage, *args, **kwargs)
            if stage == "script":
                assert "Batman" in args[0] and "Superman" in args[0] and "ctx_identity_auto_" in args[0]
                result["beats"][0]["sentences"][0]["text"] = "Synthetic Film: Batman và Superman trong cảnh thử nghiệm."
            return result

    video, vision = FakeVideoAI(), Vision()
    result = pipeline.run_movie(synthetic_clip, directory, cfg, provider=video, text_provider=vision,
                                script_only=True, movie_title="Synthetic Film")
    assert video.calls == [] and video.uploads == 0
    assert [s for s, _ in vision.calls] == ["character_identity_discover", "character_identity_verify",
                                          "review_plan", "script", "script_audit"]
    assert "Batman và Superman" in result.read_text(encoding="utf-8")
    resumed_video, resumed_vision = FakeVideoAI(), Vision()
    pipeline.run_movie(synthetic_clip, directory, cfg, provider=resumed_video, text_provider=resumed_vision,
                       script_only=True, movie_title="Synthetic Film")
    assert resumed_video.calls == resumed_vision.calls == []


def test_two_minute_gpt_identity_scopes_frames_and_ids_per_window_and_caches(tmp_path, monkeypatch):
    import movie_analysis.pipeline as pipeline
    import youtube_shorts_bot as legacy
    from movie_analysis.media import build_scenes
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic mocked two-minute media")
    monkeypatch.setattr(legacy, "require_tools", lambda: None)
    monkeypatch.setattr(pipeline, "inspect_source", lambda *_: {"duration": 120, "has_audio": True})
    monkeypatch.setattr(pipeline, "detect_scenes", lambda *_: build_scenes(120, [], 20, 30))
    groups = []

    def frames(_source, directory, evidence):
        refs = evidence["accepted"]
        assert refs and max(c["end"] for c in refs) - min(c["start"] for c in refs) <= 45
        paths = []
        for c in refs:
            path = directory / f"frame_{c['start']:09.3f}s.jpg"
            path.write_bytes(b"synthetic transport JPEG")
            paths.append(path)
        groups.append([c["id"] for c in refs])
        return paths

    monkeypatch.setattr(pipeline, "identity_frames", frames)

    class Video(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            span = kwargs.get("span")
            if stage.startswith("global:"):
                self.calls.append((stage, kwargs))
                return {"summary": "Synthetic", "events": [{"start": span[0], "end": span[1]}]}
            if stage.startswith("deep:"):
                self.calls.append((stage, kwargs))
                return {"claims": [{**claim(stage.split(":")[1] + "_c1"), "start": span[0],
                                    "end": span[0] + 2, "modality": "both"}]}
            if stage.startswith("verify:"):
                self.calls.append((stage, kwargs))
                return {"verdicts": [{"id": stage.split(":")[1] + "_c1", "status": "supported",
                                     "confidence": .99, "reason": "Synthetic"}]}
            return super().generate(stage, *args, **kwargs)

    class Vision(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            if stage.startswith("character_identity"):
                self.calls.append((stage, kwargs))
                assert 1 <= len(kwargs["image_paths"]) <= 24
                assert kwargs["allowed_claim_ids"] == groups[-1]
                if stage == "character_identity_discover":
                    return {"matches": [candidate("Batman" if len(groups) < 3 else "Superman",
                                                  claim_ids=[groups[-1][0]])]}
                return {"verdicts": [{"id": kwargs["allowed_character_ids"][0], "status": "supported",
                                     "confidence": .99, "reason": "Synthetic independent check"}]}
            return super().generate(stage, *args, **kwargs)

    cfg = replace(MovieConfig(), video_provider="gemini_live", model="gemini-3.8-live",
                  verifier_model="gemini-3.8-live", identity_provider="openai", writer_provider="openai",
                  writer_model="gpt-5.4-mini")
    directory = tmp_path / "job"
    video, vision = Video(), Vision()
    pipeline.run_movie(source, directory, cfg, provider=video, text_provider=vision, script_only=True)
    assert len(groups) == 3 and len({ref for group in groups for ref in group}) == 6
    assert not any(s.startswith("character_identity") for s, _ in video.calls)
    assert [s for s, _ in vision.calls] == ["character_identity_discover", "character_identity_verify"] * 3 + [
        "review_plan", "script", "script_audit"]
    report = read_json(directory / "characters.json")
    assert report["model"] == "gpt-5.4-mini" and report["mode"] == "automatic"
    video, vision = Video(), Vision()
    pipeline.run_movie(source, directory, cfg, provider=video, text_provider=vision, script_only=True)
    assert video.calls == vision.calls == [] and len(groups) == 3
