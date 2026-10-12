"""Editorial validation and character recognition gates; no real paid requests."""
import copy

import pytest
from youtube_shorts_bot import BotError
from movie_analysis.core import Cache, MovieConfig, spoken_text, read_json, validate_script
from movie_analysis.characters import identity_frames, recognize_characters, validate_matches
from movie_analysis.narration import validate_context
from movie_analysis.review import validate_plan, validate_review_audit, validate_review_script
from movie_analysis.pipeline import run_movie
from test_movie_analysis import FakeVideoAI, claim, script, synthetic_clip


def ctx():
    return validate_context({"title": "Synthetic Film", "facts": [
        {"id": "ctx_person", "text": "Nhân vật thử nghiệm tên Đỏ.", "source": "synthetic_fixture"}],
        "characters": [{"id": "char_red", "name": "Đỏ", "spoken_name": "Đỏ",
                        "recognition_notes": "Nhận diện thử nghiệm, không phải người thật.", "context_ids": ["ctx_person"]}]})


def test_spoken_citations_removed_without_losing_metadata_or_dialogue():
    data = script()
    data["beats"][0]["sentences"][0]["text"] = "Khung hình đỏ. [s001_c1,ctx_person]"
    result = validate_script(data, {"accepted": [claim()]})
    assert result["beats"][0]["voiceover"] == "Khung hình đỏ."
    assert result["beats"][0]["sentences"][0]["claim_ids"] == ["s001_c1"]
    assert spoken_text("[tiếng cười] Toang! [ctx_doom]") == "[tiếng cười] Toang!"
    data = script()
    data["beats"][0]["sentences"][0]["text"] = "[s001_c1]"
    with pytest.raises(BotError, match="chỉ chứa mã"):
        validate_script(data, {"accepted": [claim()]})


def test_plan_requires_structure_and_real_visual_references():
    evidence = {"accepted": [claim()]}
    data = FakeVideoAI().generate("review_plan")
    assert validate_plan(copy.deepcopy(data), evidence, ctx())
    for mutate in (lambda p: p["sections"][0].update(role="development"),
                   lambda p: p["sections"][1].update(claim_ids=["ctx_person"]),
                   lambda p: p["sections"][1].update(claim_ids=["hallucination"])):
        bad = copy.deepcopy(data)
        mutate(bad)
        with pytest.raises(BotError):
            validate_plan(bad, evidence, ctx())


def test_review_requires_introduction_and_quality_even_when_facts_pass():
    ai = FakeVideoAI()
    data = ai.generate("script")
    evidence = {"accepted": [claim()]}
    valid = validate_review_script(copy.deepcopy(data), evidence, ctx(), 30)
    audit = ai.generate("script_audit")
    assert validate_review_audit(audit, valid)
    bad = copy.deepcopy(audit)
    bad["quality"]["natural_vietnamese"] = {"score": 2, "reason": "Kể cảnh máy móc."}
    with pytest.raises(BotError, match="natural_vietnamese=2"):
        validate_review_audit(bad, valid)
    bad["quality"]["natural_vietnamese"]["score"] = True
    with pytest.raises(BotError, match="điểm 0–5"):
        validate_review_audit(bad, valid)
    data["beats"][0]["sentences"][0]["text"] = "Khung hình có nền đỏ."
    with pytest.raises(BotError, match="giới thiệu phim"):
        validate_review_script(data, evidence, ctx(), 30)
    data = ai.generate("script")
    data["beats"][0]["sentences"][0]["text"] = "Synthetic Film " + "đỏ " * 150
    with pytest.raises(BotError, match="tối đa 138 cho 30s"):
        validate_review_script(data, evidence, ctx(), 30)


def test_plan_identity_context_expands_only_verified_clip_provenance():
    evidence = {"accepted": [claim()]}
    context = ctx()
    context["facts"].append({"id": "ctx_identity_red", "text": "Verified identity", "source": "video_identity_verified", "claim_ids": [claim()["id"]]})
    parts = [{"role": role, "point": "A relevant point", "claim_ids": ["ctx_identity_red"]}
             for role in ["opening", "development", "closing"]]
    data = {**{key: "A clear point" for key in ["central_question", "thesis", "hook", "introduction", "closing"]}, "sections": parts}
    assert all(claim()["id"] in s["claim_ids"] for s in validate_plan(data, evidence, context)["sections"])
    bad = copy.deepcopy(data)
    bad["sections"][1]["claim_ids"] = ["ctx_identity_unknown"]
    with pytest.raises(BotError):
        validate_plan(bad, evidence, context)


def test_failed_editorial_audit_resumes_targeted_repair_without_video_calls(synthetic_clip, tmp_path):
    class Failing(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            result = super().generate(stage, *args, **kwargs)
            if stage == "script_audit":
                result["reviews"][0].update(supported=False, reason="Synthetic unsupported sentence")
                result["repair_notes"] = ["Correct only the synthetic unsupported sentence"]
            return result
    directory = tmp_path / "repair-resume"
    with pytest.raises(BotError):
        run_movie(synthetic_clip, directory, MovieConfig(), provider=Failing(), script_only=True)
    assert (directory / "script_repair.json").exists()
    class Repaired(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            result = super().generate(stage, *args, **kwargs)
            if stage == "script":
                result["beats"][0]["sentences"][0]["text"] += " Màu đỏ hiện rõ."
            return result
    resumed = Repaired()
    run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True)
    assert [s for s, _ in resumed.calls] == ["script", "script_audit"]
    assert resumed.uploads == 0 and not (directory / "script_repair.json").exists()
    assert (directory / "script_approved.json").exists()


@pytest.mark.parametrize("cues,confidence,status,accepted", [
    (["face", "setting"], .98, "supported", True),
    (["setting"], .99, "supported", False),
    (["face", "setting"], .90, "supported", False),
    (["face", "setting"], .98, "uncertain", False),
])
def test_identity_needs_direct_cues_and_independent_agreement(tmp_path, cues, confidence, status, accepted):
    calls = []
    def ask(stage, *_args, **_kwargs):
        calls.append(stage)
        if stage == "character_identity":
            return {"matches": [{"character_id": "char_red", "claim_ids": [claim()["id"]],
                                 "confidence": confidence, "cues": cues, "reason": "Synthetic identification"}]}
        return {"verdicts": [{"id": "char_red", "confidence": .98, "status": status, "reason": "Synthetic independent check"}]}
    evidence = {"accepted": [claim()]}
    config = MovieConfig()
    original = ctx()
    original["characters"][0]["playful_names"] = ["thằng Đỏ"]
    result = recognize_characters(Cache(tmp_path), tmp_path, "hash", config, evidence, original, ask)
    assert bool(result["recognized_characters"]) == accepted
    assert len(original["facts"]) == 1  # no contaminating input context/cache
    assert any(f["id"] == "ctx_identity_red" for f in result["facts"]) == accepted
    if accepted:
        assert result["recognized_characters"][0]["playful_names"] == ["thằng Đỏ"]
    original["characters"][0]["playful_names"] = ["ông Đỏ"]
    renamed = recognize_characters(Cache(tmp_path), tmp_path, "hash", config, evidence, original, ask)
    if accepted:
        assert renamed["recognized_characters"][0]["playful_names"] == ["ông Đỏ"]
    assert calls == ["character_identity", "character_identity_verify"]  # cached paid passes


def test_roster_and_matches_require_known_sources():
    bad = ctx()
    bad["characters"][0]["context_ids"] = ["ctx_missing"]
    with pytest.raises(BotError):
        validate_context(bad)
    with pytest.raises(BotError):
        validate_matches({"matches": [{"character_id": "char_unknown"}]}, {"accepted": [claim()]}, ctx())
    with pytest.raises(BotError):
        validate_context({"facts": [{"id": "ctx_identity_fake", "text": "Fake verified identity", "source": "user"}]})


@pytest.mark.parametrize("names", ["con khỉ", [""], ["x" * 101], ["x"] * 7, [None]])
def test_playful_names_are_validated_without_becoming_identity_evidence(names):
    bad = ctx()
    bad["characters"][0]["playful_names"] = names
    with pytest.raises(BotError, match="playful_names"):
        validate_context(bad)


def test_openai_identity_cannot_accept_audio_cues(tmp_path):
    from dataclasses import replace
    def ask(stage, *_args, **_kwargs):
        if stage == "character_identity":
            return {"matches": [{"character_id": "char_red", "claim_ids": [claim()["id"]],
                                 "confidence": .99, "cues": ["voice", "setting"], "reason": "Synthetic wrong voice cue"}]}
        return {"verdicts": [{"id": "char_red", "status": "supported", "confidence": .99, "reason": "Synthetic"}]}
    result = recognize_characters(Cache(tmp_path), tmp_path, "hash", replace(MovieConfig(), identity_provider="openai"),
                                  {"accepted": [claim()]}, ctx(), ask)
    assert result["recognized_characters"] == []


def test_openai_vision_request_and_actual_usage_replace_reservation(tmp_path):
    import json
    from movie_analysis.openai_writer import OpenAIWriterClient
    from movie_analysis.provider import GeminiVideoClient
    from test_movie_analysis import Response
    class HTTP:
        payload = None
        def request(self, *_args, **kwargs):
            self.payload = kwargs["json"]
            return Response(200, {"status": "completed", "usage": {"total_tokens": 100},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"matches": []})}]}]})
    frame = tmp_path / "frame_1.000s.jpg"
    frame.write_bytes(b"synthetic JPEG transport fixture")
    ledger = GeminiVideoClient("fake-key", MovieConfig(), tmp_path)
    http = HTTP()
    client = OpenAIWriterClient("fake-key", MovieConfig(), ledger, "gpt-5.4-mini", http)
    assert client.generate("character_identity", "inspect stills", image_paths=[frame],
                           allowed_claim_ids=[claim()["id"]]) == {"matches": []}
    assert http.payload["input"][0]["content"][-1]["type"] == "input_image"
    refs = http.payload["text"]["format"]["schema"]["properties"]["matches"]["items"]["properties"]["claim_ids"]
    assert refs["items"]["enum"] == [claim()["id"]]
    props = http.payload["text"]["format"]["schema"]["properties"]["matches"]["items"]["properties"]
    assert "enum" not in props["character_id"] and "enum" not in props["reason"]
    assert ledger.usage["calls"][0]["charged_tokens"] == 100
    with pytest.raises(BotError):
        client.generate("script", "text only", image_paths=[frame])


def test_openai_identity_uses_frames_and_resumes_without_paid_calls(synthetic_clip, tmp_path):
    from dataclasses import replace
    from movie_analysis.core import write_json
    class VisionFake(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            if stage.startswith("character_identity"):
                self.calls.append((stage, kwargs))
                assert kwargs["image_paths"] and all(p.is_file() for p in kwargs["image_paths"])
                if stage == "character_identity":
                    return {"matches": [{"character_id": "char_red", "claim_ids": [claim()["id"]],
                                         "confidence": .98, "cues": ["face", "setting"], "reason": "Synthetic"}]}
                return {"verdicts": [{"id": "char_red", "status": "supported", "confidence": .98, "reason": "Synthetic"}]}
            return super().generate(stage, *args, **kwargs)
    video, text = FakeVideoAI(), VisionFake()
    config = replace(MovieConfig(), writer_provider="openai", identity_provider="openai")
    context_file = tmp_path / "context.json"
    write_json(context_file, ctx())
    directory = tmp_path / "job"
    run_movie(synthetic_clip, directory, config, provider=video, text_provider=text, context_file=context_file, script_only=True)
    assert [s for s, _ in text.calls] == ["character_identity", "character_identity_verify", "review_plan", "script", "script_audit"]
    assert not any(s.startswith("character_identity") for s, _ in video.calls)
    resumed_video, resumed_text = FakeVideoAI(), VisionFake()
    run_movie(synthetic_clip, directory, config, provider=resumed_video, text_provider=resumed_text, script_only=True)
    assert resumed_video.calls == resumed_text.calls == []


def test_invalid_writer_output_is_repaired_and_cached_without_reanalysis(synthetic_clip, tmp_path):
    class PoorFirstWriter(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            value = super().generate(stage, *args, **kwargs)
            if stage == "script" and sum(s == stage for s, _ in self.calls) == 1:
                value["beats"][0]["role"] = "development"
            return value
    ai = PoorFirstWriter()
    directory = tmp_path / "job"
    output = run_movie(synthetic_clip, directory, MovieConfig(), provider=ai, script_only=True)
    assert output.is_file() and sum(s == "script" for s, _ in ai.calls) == 2
    resumed = FakeVideoAI()
    run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True)
    assert resumed.calls == []
    assert read_json(directory / "script_approved.json")["script"]["beats"][0]["role"] == "opening"


def test_dropped_introduction_is_restored_from_sourced_title_and_audited():
    context = ctx()
    context["facts"].append({"id": "ctx_title", "source": "user_provided",
                            "text": "Tên phim/trailer do người dùng xác nhận: Synthetic Film"})
    data = FakeVideoAI().generate("script")
    data["beats"][0]["sentences"][0]["text"] = "Khung hình có nền đỏ."
    evidence = {"accepted": [claim()]}
    result = validate_review_script(data, evidence, context, 30)
    introduction = result["beats"][0]["sentences"][0]
    assert introduction == {"text": "Đây là Synthetic Film.", "kind": "context", "claim_ids": ["ctx_title"]}
    assert result["beats"][0]["sentences"][1]["claim_ids"] == [claim()["id"]]
    assert validate_review_script(copy.deepcopy(result), evidence, context, 30) == result
    # A previous audit cannot approve the newly inserted sentence implicitly.
    with pytest.raises(BotError, match="mọi câu"):
        validate_review_audit(FakeVideoAI().generate("script_audit"), result)
    for change in ({"source": "unverified"}, {"text": "Tên một phim khác"}):
        bad_context = copy.deepcopy(context)
        bad_context["facts"][-1].update(change)
        with pytest.raises(BotError, match='giới thiệu phim.*Synthetic Film'):
            validate_review_script(missing_title_script(), evidence, bad_context, 30)


def missing_title_script():
    data = FakeVideoAI().generate("script")
    data["beats"][0]["sentences"][0]["text"] = "Khung hình có nền đỏ."
    return data


def test_resume_missing_title_failure_reuses_video_and_requires_new_audit(synthetic_clip, tmp_path, monkeypatch):
    import movie_analysis.pipeline as pipeline
    directory = tmp_path / "missing-title-resume"

    class Editor(FakeVideoAI):
        resume_mode = False

        def generate(self, stage, *args, **kwargs):
            value = super().generate(stage, *args, **kwargs)
            if stage == "script" and (self.resume_mode or sum(s == "script" for s, _ in self.calls) > 1):
                value["beats"][0]["sentences"][0]["text"] = "Khung hình có nền đỏ."
            if stage == "script_audit":
                draft = read_json(directory / "script.json")["data"]
                failed = not self.resume_mode and sum(s == "script" for s, _ in self.calls) == 1
                value["reviews"] = [{"id": f'{b["id"]}:{i}', "supported": not failed,
                                     "reason": "Synthetic factual check"}
                                    for b in draft["beats"] for i, _ in enumerate(b["sentences"])]
                value["repair_notes"] = ["Remove unsupported assertion"] if failed else []
            return value

    def old_validator(data, evidence, context, seconds, source_seconds=None):
        if "Synthetic Film" not in data["beats"][0]["sentences"][0]["text"]:
            raise BotError("Mở đầu review thiếu lời giới thiệu phim.")
        return validate_review_script(data, evidence, context, seconds, source_seconds)

    monkeypatch.setattr(pipeline, "validate_review_script", old_validator)
    with pytest.raises(BotError, match="giới thiệu phim"):
        run_movie(synthetic_clip, directory, MovieConfig(), provider=Editor(), script_only=True,
                  movie_title="Synthetic Film")
    assert "previous_audit" in read_json(directory / "script_repair.json")["feedback"]
    monkeypatch.setattr(pipeline, "validate_review_script", validate_review_script)
    resumed = Editor()
    # Return another title-less corrected draft even on this provider's first call.
    resumed.resume_mode = True
    run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True,
              movie_title="Synthetic Film")
    assert [s for s, _ in resumed.calls] == ["script", "script_audit"]
    assert resumed.uploads == 0
    approved = read_json(directory / "script_approved.json")
    assert approved["script"]["beats"][0]["sentences"][0]["claim_ids"] == ["ctx_title"]
    assert len(approved["audit"]["reviews"]) == 4
    assert not (directory / "script_repair.json").exists()
    cached = Editor()
    run_movie(synthetic_clip, directory, MovieConfig(), provider=cached, script_only=True,
              movie_title="Synthetic Film")
    assert cached.calls == [] and cached.uploads == 0


def test_identity_model_override_does_not_reanalyze_video(synthetic_clip, tmp_path):
    from dataclasses import replace
    from movie_analysis.core import write_json
    class NativeIdentity(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            if stage.startswith("character_identity"):
                self.calls.append((stage, kwargs))
                assert kwargs["model_override"] in {"model-one", "model-two"}
                if stage == "character_identity":
                    return {"matches": [{"character_id": "char_red", "claim_ids": [claim()["id"]],
                                         "confidence": .98, "cues": ["face", "setting"], "reason": "Synthetic"}]}
                return {"verdicts": [{"id": "char_red", "status": "supported", "confidence": .98, "reason": "Synthetic"}]}
            return super().generate(stage, *args, **kwargs)
    context_file = tmp_path / "context.json"
    write_json(context_file, ctx())
    directory = tmp_path / "job"
    cfg = replace(MovieConfig(), identity_model="model-one")
    ai = NativeIdentity()
    run_movie(synthetic_clip, directory, cfg, provider=ai, context_file=context_file, script_only=True)
    changed = NativeIdentity()
    run_movie(synthetic_clip, directory, replace(cfg, identity_model="model-two"), provider=changed, script_only=True)
    assert [stage for stage, _ in changed.calls] == ["character_identity", "character_identity_verify"]
