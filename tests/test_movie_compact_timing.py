"""Constrained compact rewrites, legacy cache recovery and factual audit gates."""
import copy
import json
import re

import pytest

from youtube_shorts_bot import BotError
from movie_analysis.core import Cache, MovieConfig, read_json
from movie_analysis.narration import validate_context
from movie_analysis.timing import NarrationTimingError, narration_schedule
from movie_analysis.timing_repair import assemble_compact_repair, compact_rewrite_schema, repair_narration_timing
from movie_analysis.review import validate_review_script
from test_movie_analysis import FakeVideoAI, Response, claim
from test_movie_timing_repair import fixture


def edit():
    return {"edits": {"b03": {"words": ["Đỏ", "nóng", "thật."], "kind": "joke", "claim_ids": ["s001_c3"]}}}


@pytest.mark.parametrize("change", ["over_cap", "embedded_space", "unknown_beat", "unknown_ref", "bad_kind"])
def test_compact_edits_cannot_evade_cap_or_change_provenance(change):
    script, evidence, context, timing = fixture()
    response = edit()
    if change == "over_cap":
        response["edits"]["b03"]["words"].append("quá")
    elif change == "embedded_space":
        response["edits"]["b03"]["words"][0] = "Đỏ nóng thật"
    elif change == "unknown_beat":
        response["edits"]["b01"] = response["edits"].pop("b03")
    elif change == "unknown_ref":
        response["edits"]["b03"]["claim_ids"] = ["s001_c2"]
    else:
        response["edits"]["b03"]["kind"] = {}
    with pytest.raises(BotError):
        assemble_compact_repair(response, script, timing, evidence, context, 45, 45)


def test_old_two_failed_word_cap_rewrites_resume_with_only_compact_and_audit(tmp_path):
    script, evidence, context, timing = fixture()
    calls = []

    def ask(stage, _prompt):
        calls.append(stage)
        return copy.deepcopy(script) if stage == "script" else FakeVideoAI().generate("script_audit")

    def compact(stage, prompt, limits):
        calls.append(stage)
        assert limits == {"b03": 3} and "ONE space-separated unit" in prompt
        return edit()

    def run(callback=None):
        return repair_narration_timing(tmp_path, Cache(tmp_path), "key", script, timing, evidence,
                                      context, 45, 45, {}, ask, lambda _: None, ask_compact=callback)

    with pytest.raises(BotError, match="tối đa 3 từ"):
        run()  # emulate deployed v1: invalid raw replies remain cached
    assert calls == ["script", "script"]
    calls.clear()
    result = run(compact)
    assert calls == ["timing_rewrite", "script_audit"]
    assert result["beats"][:2] == script["beats"][:2]
    assert result["beats"][2]["voiceover"] == "Đỏ nóng thật."
    assert read_json(tmp_path / "script_approved.json")["script"] == result
    calls.clear()
    assert run(compact) == result and calls == []


def test_compact_audit_transport_failure_resumes_without_another_edit(tmp_path):
    script, evidence, context, timing = fixture()
    calls = []
    failing = True

    def ask(stage, _prompt):
        nonlocal failing
        calls.append(stage)
        if stage == "script":
            return copy.deepcopy(script)
        if failing:
            failing = False
            raise BotError("Synthetic HTTP 503")
        return FakeVideoAI().generate("script_audit")

    def compact(stage, _prompt, _limits):
        calls.append(stage)
        return edit()

    def run():
        return repair_narration_timing(tmp_path, Cache(tmp_path), "key", script, timing, evidence,
                                      context, 45, 45, {}, ask, lambda _: None, ask_compact=compact)

    with pytest.raises(BotError, match="503"):
        run()
    assert calls == ["script", "script", "timing_rewrite", "script_audit"]
    calls.clear()
    assert run()["beats"][2]["voiceover"] == "Đỏ nóng thật."
    assert calls == ["script_audit"]


def test_compact_cannot_bypass_factual_audit_and_all_failed_edits_are_cached(tmp_path):
    script, evidence, context, timing = fixture()
    calls = []

    def ask(stage, _prompt):
        calls.append(stage)
        if stage == "script":
            return copy.deepcopy(script)
        result = FakeVideoAI().generate("script_audit")
        result["reviews"][-1].update(supported=False, reason="Synthetic unsupported replacement")
        return result

    def compact(stage, _prompt, _limits):
        calls.append(stage)
        return edit()

    for _ in range(2):
        with pytest.raises(BotError, match="không có bằng chứng"):
            repair_narration_timing(tmp_path, Cache(tmp_path), "key", script, timing, evidence,
                                   context, 45, 45, {}, ask, lambda _: None, ask_compact=compact)
    assert calls == ["script", "script", "timing_rewrite", "script_audit", "timing_rewrite", "script_audit"]
    assert not (tmp_path / "script_approved.json").exists()


def test_request_schema_bounds_each_beat_and_never_allows_space_inside_word(tmp_path):
    from movie_analysis.openai_writer import OpenAIWriterClient
    from movie_analysis.provider import GeminiVideoClient

    class HTTP:
        payload = None
        def request(self, *_args, **kwargs):
            self.payload = kwargs["json"]
            return Response(200, {"status": "completed", "usage": {"total_tokens": 100}, "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(edit())}]}]})

    http = HTTP()
    client = OpenAIWriterClient("fake-key", MovieConfig(), GeminiVideoClient("fake-key", MovieConfig(), tmp_path),
                               "gpt-5.4-mini", http)
    client.generate("timing_rewrite", "fixture", allowed_claim_ids=["s001_c3"], timing_word_limits={"b02": 47, "b03": 3})
    schema = http.payload["text"]["format"]["schema"]["properties"]["edits"]
    assert schema["required"] == ["b02", "b03"] and schema["additionalProperties"] is False
    for bid, cap in (("b02", 47), ("b03", 3)):
        props = schema["properties"][bid]["properties"]
        assert props["words"]["maxItems"] == cap
        assert re.fullmatch(props["words"]["items"]["pattern"], "toang")
        assert not re.fullmatch(props["words"]["items"]["pattern"], "sắp toang")
        assert props["claim_ids"]["items"]["enum"] == ["s001_c3"]


def test_log_case_b02_40_seconds_has_47_word_cap_and_unaffected_beats_remain_identical(tmp_path):
    script = FakeVideoAI().generate("script")
    script["beats"].insert(2, copy.deepcopy(script["beats"][1]))
    starts = (0, 20, 50.1, 114)
    claims = [{**claim(f"s001_c{i + 1}"), "start": start, "end": start + 2} for i, start in enumerate(starts)]
    for index, (beat, c) in enumerate(zip(script["beats"], claims), 1):
        beat["id"] = f"b{index:02d}"
        beat["role"] = "opening" if index == 1 else "closing" if index == 4 else "development"
        beat["visual_claim_id"] = c["id"]
        beat["sentences"][0]["claim_ids"] = [c["id"]]
    script["beats"][1]["sentences"][0]["text"] = " ".join(["màu", "đỏ", "trong", "cảnh", "này"] * 14)
    evidence, context = {"accepted": claims}, validate_context({"title": "Synthetic Film"})
    script = validate_review_script(script, evidence, context, 120, 120)
    with pytest.raises(NarrationTimingError) as error:
        narration_schedule(script["beats"], {c["id"]: c for c in claims}, [5, 40.8, 5, 5], 120)
    timing = error.value.feedback
    assert timing["available_seconds"] == pytest.approx(30.1)
    assert timing["beats"][0]["id"] == "b02" and timing["beats"][0]["maximum_words"] == 47
    candidate = assemble_compact_repair({"edits": {"b02": {"words": ["Cảnh", "này", "căng", "thật."],
        "kind": "inference", "claim_ids": ["s001_c2"]}}}, script, timing, evidence, context, 120, 120)
    assert all(candidate["beats"][i] == script["beats"][i] for i in (0, 2, 3))
    slots = narration_schedule(candidate["beats"], {c["id"]: c for c in claims}, [5, 4, 5, 5], 120)
    assert slots[1]["start"] >= 20 and slots[1]["end"] <= 50.1
