"""Incomplete audit recovery and factual gates; no real AI/TTS requests."""
import copy
import json

import pytest

from youtube_shorts_bot import BotError
from movie_analysis.audit import complete_review_audit
from movie_analysis.core import AuditCoverageError, Cache, MovieConfig, read_json, validate_audit_coverage
from movie_analysis.narration import audit_prompt, validate_context
from movie_analysis.review import validate_review_audit
from test_movie_analysis import FakeVideoAI, claim, synthetic_clip


def draft():
    return FakeVideoAI().generate("script")


def audit():
    return FakeVideoAI().generate("script_audit")


@pytest.mark.parametrize("reviews", [None, [None], [{"id": []}], [],
    [{"id": "b01:0"}, {"id": "b01:0"}, {"id": "b03:0"}],
    [{"id": "b01:1"}, {"id": "b02:1"}, {"id": "b03:1"}],
    [{"id": "b01:0"}, {"id": "b02:0"}, {"id": "unknown"}]])
def test_bad_coverage_is_recoverable_format_error_not_factual_rejection(reviews):
    with pytest.raises(AuditCoverageError, match="mọi câu"):
        validate_audit_coverage({**audit(), "reviews": reviews}, draft())


def test_ledger_maps_every_sentence_entry_including_restored_intro_and_multi_sentence_text():
    script = draft()
    script["beats"][0]["sentences"].insert(0, {"text": "Đây là Synthetic Film.", "kind": "context",
        "claim_ids": ["ctx_title"]})
    script["beats"][0]["sentences"][1]["text"] = "Một câu. Rồi thêm một câu nữa!"
    prompt = audit_prompt(script, {"accepted": [claim()]}, validate_context({"title": "Synthetic Film"}))
    ledger = json.loads(prompt.split("\nSENTENCE LEDGER: ")[1])
    assert [s["id"] for s in ledger] == ["b01:0", "b01:1", "b02:0", "b03:0"]
    assert ledger[0]["kind"] == "context" and ledger[1]["text"] == "Một câu. Rồi thêm một câu nữa!"
    assert "exactly 4 reviews" in prompt and "Never split or combine" in prompt


def test_cached_incomplete_audit_is_corrected_without_repeating_original_request(tmp_path):
    initial = audit()
    initial["reviews"].pop()
    cache = Cache(tmp_path)
    cache.stage("script_audit", ["same dependencies"], lambda: initial)  # old code accepted receipt
    original_raw = (tmp_path / "script_audit.response.json").read_bytes()
    calls = []

    def ask(stage, prompt):
        calls.append(stage)
        assert "AUDIT COVERAGE REPAIR" in prompt and "Thiếu: b03:0" in prompt
        return audit()

    for _ in range(2):
        result = complete_review_audit(cache, "script_audit", ["same dependencies"], draft(), "ledger fixture", ask)
        assert validate_review_audit(result, draft())
    assert calls == ["script_audit"]
    assert (tmp_path / "script_audit.response.json").read_bytes() == original_raw


@pytest.mark.parametrize("rejection", ["sentence", "title", "style", "quality"])
def test_coverage_repair_cannot_turn_existing_rejection_into_approval(tmp_path, rejection):
    initial = audit()
    initial["reviews"].pop()
    if rejection == "sentence":
        initial["reviews"][0].update(supported=False, reason="Unsupported claim must be rewritten")
    elif rejection == "quality":
        initial["quality"]["story"] = {"score": 2, "reason": "Needs an actual story"}
    else:
        initial["title_supported" if rejection == "title" else "style_ok"] = False
    answers = iter([initial, audit()])  # corrected reviewer tries to approve everything
    result = complete_review_audit(Cache(tmp_path), "script_audit", [], draft(), "fixture", lambda *_: next(answers))
    with pytest.raises(BotError):
        validate_review_audit(result, draft())


def test_incomplete_audit_repairs_are_bounded_and_cached_on_resume(tmp_path):
    calls = []
    callbacks = []

    def ask(stage, _prompt):
        calls.append(stage)
        result = audit()
        result["reviews"] = result["reviews"][:-1]
        return result

    for _ in range(2):
        with pytest.raises(AuditCoverageError, match="Thiếu: b03:0"):
            complete_review_audit(Cache(tmp_path), "script_audit", [], draft(), "fixture", ask,
                                  on_incomplete=lambda value: callbacks.append(value))
    assert calls == ["script_audit"] * 3
    assert len(callbacks) == 6 and not (tmp_path / "script_approved.json").exists()


def test_old_job_resumes_only_incomplete_audit_without_rewriting_or_reanalysis(synthetic_clip, tmp_path, monkeypatch):
    import movie_analysis.pipeline as pipeline
    directory = tmp_path / "job"

    class Incomplete(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            result = super().generate(stage, *args, **kwargs)
            if stage == "script_audit":
                result["reviews"].pop()
            return result

    def old_audit(cache, name, dependencies, _script, prompt, ask, **_kwargs):
        return cache.stage(name, dependencies, lambda: ask("script_audit", prompt))

    monkeypatch.setattr(pipeline, "complete_review_audit", old_audit)
    with pytest.raises(AuditCoverageError):
        pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=Incomplete(), script_only=True)
    saved_script = copy.deepcopy(read_json(directory / "script_repair.json")["feedback"]["script"])
    receipts = [directory / "global.json", directory / "s001_deep.json", directory / "script.json"]
    # The saved script/analysis are exactly the artifacts being resumed.
    assert all(p.exists() for p in receipts)
    saved_receipts = {p: p.read_bytes() for p in receipts}
    monkeypatch.setattr(pipeline, "complete_review_audit", complete_review_audit)
    resumed = FakeVideoAI()
    pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True)
    assert [s for s, _ in resumed.calls] == ["script_audit"] and resumed.uploads == 0
    approved = read_json(directory / "script_approved.json")
    assert approved["script"] == saved_script and validate_review_audit(approved["audit"], saved_script)
    assert all(p.read_bytes() == content for p, content in saved_receipts.items())
    assert not (directory / "script_repair.json").exists()
    resumed = FakeVideoAI()
    pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True)
    assert resumed.calls == []


def test_audit_correction_network_failure_keeps_writer_cached_on_resume(synthetic_clip, tmp_path):
    import movie_analysis.pipeline as pipeline
    directory = tmp_path / "job"

    class Interrupted(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            if stage == "script_audit" and "AUDIT COVERAGE REPAIR" in args[0]:
                self.calls.append((stage, kwargs))
                raise BotError("Synthetic HTTP 429")
            result = super().generate(stage, *args, **kwargs)
            if stage == "script_audit":
                result["reviews"].pop()
            return result

    with pytest.raises(BotError, match="429"):
        pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=Interrupted(), script_only=True)
    assert not (directory / "script_approved.json").exists()
    assert read_json(directory / "script_repair.json")["feedback"]["audit"]["reviews"]
    resumed = FakeVideoAI()
    pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True)
    assert [s for s, _ in resumed.calls] == ["script_audit"] and resumed.uploads == 0


def test_factual_rejection_after_coverage_repair_still_requires_writer_revision(synthetic_clip, tmp_path):
    import movie_analysis.pipeline as pipeline

    class Rejected(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            result = super().generate(stage, *args, **kwargs)
            if stage == "script_audit" and sum(s == "script" for s, _ in self.calls) == 1:
                if "AUDIT COVERAGE REPAIR" not in args[0]:
                    result["reviews"].pop()
                    result["reviews"][0].update(supported=False, reason="Must rewrite unsupported detail")
            return result

    provider = Rejected()
    pipeline.run_movie(synthetic_clip, tmp_path / "job", MovieConfig(), provider=provider, script_only=True)
    assert [s for s, _ in provider.calls if s in {"script", "script_audit"}] == [
        "script", "script_audit", "script_audit", "script", "script_audit"]


def test_measured_timing_repair_completes_audit_without_repeating_writer(tmp_path):
    from movie_analysis.timing_repair import repair_narration_timing
    from test_movie_timing_repair import fixture, corrected
    script, evidence, context, timing = fixture()
    calls = []

    def ask(stage, _prompt):
        calls.append(stage)
        if stage == "script":
            return corrected(script)
        result = audit()
        if calls == ["script", "script_audit"]:
            result["reviews"].pop()
        return result

    result = repair_narration_timing(tmp_path, Cache(tmp_path), "key", script, timing, evidence, context,
                                     45, 45, {}, ask, lambda _: None)
    assert result["beats"][:2] == script["beats"][:2]
    assert result["beats"][2]["voiceover"] == "Đỏ nóng thật."
    assert calls == ["script", "script_audit", "script_audit"]


@pytest.mark.parametrize("rejection", ["sentence", "title", "style", "quality"])
def test_corrected_audit_can_reject_previously_approved_content(tmp_path, rejection):
    initial = audit()
    initial["reviews"].pop()
    corrected = audit()
    if rejection == "sentence":
        corrected["reviews"][0].update(supported=False, reason="Newly detected unsupported detail")
    elif rejection == "quality":
        corrected["quality"]["story"] = {"score": 2, "reason": "Needs an actual story"}
    else:
        corrected["title_supported" if rejection == "title" else "style_ok"] = False
    answers = iter([initial, corrected])
    result = complete_review_audit(Cache(tmp_path), "script_audit", [], draft(), "fixture", lambda *_: next(answers))
    with pytest.raises(BotError):
        validate_review_audit(result, draft())


def test_unrepairable_audit_stops_before_approval_and_does_not_rewrite_on_resume(synthetic_clip, tmp_path):
    import movie_analysis.pipeline as pipeline
    directory = tmp_path / "job"

    class Incomplete(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            result = super().generate(stage, *args, **kwargs)
            if stage == "script_audit":
                result["reviews"].pop()
            return result

    first = Incomplete()
    with pytest.raises(AuditCoverageError):
        pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=first, script_only=True)
    assert [s for s, _ in first.calls if s in {"script", "script_audit"}] == [
        "script", "script_audit", "script_audit", "script_audit"]
    assert not (directory / "script_approved.json").exists()
    assert read_json(directory / "job.json")["stage"] == "script_audit"
    resumed = Incomplete()
    with pytest.raises(AuditCoverageError):
        pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True)
    assert resumed.calls == [] and resumed.uploads == 0
