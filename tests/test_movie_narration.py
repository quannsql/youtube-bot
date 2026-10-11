"""Context provenance, writer routing/budgets and review without paid calls."""
import copy
import json

import pytest

import youtube_shorts_bot as legacy
from movie_analysis.core import MovieConfig, read_json, validate_script
from movie_analysis.narration import load_context, validate_context, writer_prompt
from movie_analysis.openai_writer import OpenAIWriterClient
from movie_analysis.pipeline import run_movie
from movie_analysis.provider import GeminiVideoClient
from test_movie_analysis import FakeVideoAI, Response, claim, script, synthetic_clip


def context():
    return {"title": "Synthetic Film", "style": "Hài hước hơi lầy",
            "facts": [{"id": "ctx_premise", "text": "Đây là một phim thử nghiệm về màu sắc.", "source": "user_provided"}]}


def test_context_citations_cannot_replace_observation_or_visuals():
    data = script()
    evidence = {"accepted": [claim()]}
    data["beats"][0]["sentences"].append({"text": "Đây là một phim thử nghiệm về màu sắc.",
                                          "kind": "context", "claim_ids": ["ctx_premise"]})
    assert validate_script(copy.deepcopy(data), evidence, context())
    with pytest.raises(legacy.BotError, match="chưa được kiểm chứng"):
        validate_script(copy.deepcopy(data), evidence)
    wrong = copy.deepcopy(data)
    wrong["beats"][0]["sentences"][-1]["kind"] = "fact"
    with pytest.raises(legacy.BotError, match="bằng chứng từ clip"):
        validate_script(wrong, evidence, context())
    wrong = copy.deepcopy(data)
    wrong["beats"][0]["visual_claim_id"] = "ctx_premise"
    with pytest.raises(legacy.BotError, match="Visual"):
        validate_script(wrong, evidence, context())


def test_context_persists_sources_and_style_and_requires_provenance(tmp_path):
    original = load_context(tmp_path, "Synthetic Film", "Nội dung đã biết", "Hài hơi lầy")
    assert load_context(tmp_path) == original
    assert original["facts"][0]["source"] == "user_provided"
    changed = load_context(tmp_path, "Different Film")
    assert changed["facts"] == [{"id": "ctx_title", "text": "Tên phim/trailer do người dùng xác nhận: Different Film", "source": "user_provided"}]
    with pytest.raises(legacy.BotError, match="nguồn"):
        validate_context({"facts": [{"id": "ctx_1", "text": "An unsupported plot"}]})
    with pytest.raises(legacy.BotError, match="không trùng"):
        validate_context({"facts": context()["facts"] * 2})
    prompt = writer_prompt(30, {"accepted": [claim()]}, context(), {"events": []})
    assert "Hài hước hơi lầy" in prompt and "not the full film" in prompt and "Do not narrate each" in prompt


def test_writer_never_receives_unverified_global_dialogue():
    hypothesis = {"dialogue": [{"text": "INVENTED_UNVERIFIED_DIALOGUE"}], "summary": "UNVERIFIED_CAUSE"}
    prompt = writer_prompt(60, {"accepted": [claim()]}, context(), hypothesis)
    assert "INVENTED_UNVERIFIED_DIALOGUE" not in prompt and "UNVERIFIED_CAUSE" not in prompt
    assert claim()["text"] in prompt


def test_script_only_uses_cached_analysis_and_context_changes_only_text(synthetic_clip, tmp_path):
    directory = tmp_path / "job"
    cfg = MovieConfig()
    analyzed = FakeVideoAI()
    run_movie(synthetic_clip, directory, cfg, analyze_only=True, provider=analyzed)
    ai = FakeVideoAI()
    path = run_movie(synthetic_clip, directory, cfg, provider=ai, script_only=True,
                     movie_title="Synthetic Film", narration_style="Hài hơi lầy")
    assert path.name == "narration_vi.txt" and "Khung hình" in path.read_text(encoding="utf-8")
    assert [s for s, _ in ai.calls] == ["review_plan", "script", "script_audit"] and ai.uploads == 0
    assert not (directory / "movie_review.mp4").exists()
    assert read_json(directory / "job.json")["status"] == "script_ready"
    resumed = FakeVideoAI()
    run_movie(synthetic_clip, directory, cfg, provider=resumed, script_only=True)
    assert resumed.calls == []
    run_movie(synthetic_clip, directory, cfg, provider=resumed, script_only=True, narration_style="Giọng mới")
    assert [s for s, _ in resumed.calls] == ["review_plan", "script", "script_audit"] and resumed.uploads == 0


def test_openai_routes_only_text_passes(synthetic_clip, tmp_path):
    directory = tmp_path / "job"
    video = FakeVideoAI()
    text = FakeVideoAI()
    cfg = MovieConfig(writer_provider="openai", writer_model="gpt-5.4-mini")
    run_movie(synthetic_clip, directory, cfg, provider=video, text_provider=text, script_only=True)
    assert all(stage not in {"review_plan", "script", "script_audit"} for stage, _ in video.calls)
    assert [s for s, _ in text.calls] == ["review_plan", "script", "script_audit"] and text.uploads == 0
    resumed_video, resumed_text = FakeVideoAI(), FakeVideoAI()
    run_movie(synthetic_clip, directory, cfg, provider=resumed_video, text_provider=resumed_text, script_only=True)
    assert resumed_video.calls == resumed_text.calls == []


def test_openai_writer_structured_output_usage_and_budget(tmp_path):
    class HTTP:
        requests = []
        def request(self, method, url, **kwargs):
            self.requests.append((url, kwargs))
            return Response(200, {"status": "completed", "usage": {"input_tokens": 40, "output_tokens": 60, "total_tokens": 100},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(script())}]}]})
    cfg = MovieConfig(max_calls=1)
    ledger = GeminiVideoClient("fake-gemini-key", cfg, tmp_path)
    http = HTTP()
    client = OpenAIWriterClient("fake-openai-key", cfg, ledger, "gpt-5.4-mini", http)
    assert client.generate("script", "Return JSON") == script()
    sent = http.requests[0][1]["json"]
    assert sent["store"] is False and sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["schema"]["additionalProperties"] is False and "temperature" not in sent
    assert ledger.usage["calls"][0]["provider"] == "openai"
    assert ledger.usage["calls"][0]["charged_tokens"] == 100
    with pytest.raises(legacy.BotError, match="giới hạn"):
        client.generate("script_audit", "Return JSON")
    assert len(http.requests) == 1


def test_openai_text_schemas_require_known_visual_and_citation_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "DATA_DIR", tmp_path)
    class HTTP:
        def request(self, *_args, **kwargs):
            self.sent = kwargs["json"]
            return Response(200, {"status": "completed", "usage": {"total_tokens": 10},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]})
    http = HTTP()
    cfg = MovieConfig()
    client = OpenAIWriterClient("fake-key", cfg, GeminiVideoClient("fake-key", cfg, tmp_path), "gpt-5.4-mini", http)
    for stage in ("review_plan", "script"):
        client.generate(stage, "JSON", allowed_claim_ids=["s001_1", "ctx_premise"], allowed_visual_ids=["s001_1"])
        schema = http.sent["text"]["format"]["schema"]
        node = schema["properties"]["sections" if stage == "review_plan" else "beats"]["items"]
        props = node["properties"]
        assert "visual_claim_id" in node["required"] and props["visual_claim_id"]["enum"] == ["s001_1"]
        if stage == "script":
            props = props["sentences"]["items"]["properties"]
            assert "enum" not in props["text"]
        else:
            assert "enum" not in props["point"]
        assert props["claim_ids"]["items"]["enum"] == ["s001_1", "ctx_premise"]


@pytest.mark.parametrize("response,match", [
    ({"status": "incomplete", "usage": {"total_tokens": 20}}, "chưa hoàn tất"),
    ({"status": "completed", "output": [{"content": [{"type": "refusal", "refusal": "refused"}]}]}, "JSON hợp lệ"),
])
def test_openai_incomplete_or_refused_is_not_accepted(tmp_path, response, match):
    class HTTP:
        def request(self, *_args, **_kwargs):
            return Response(200, response)
    cfg = MovieConfig()
    ledger = GeminiVideoClient("fake-key", cfg, tmp_path)
    with pytest.raises(legacy.BotError, match=match):
        OpenAIWriterClient("fake-key", cfg, ledger, "gpt-5.4-mini", HTTP()).generate("script", "JSON")
    assert len(read_json(tmp_path / "usage.json")["calls"]) == 1


def test_openai_insufficient_quota_stops_and_redacts_key(tmp_path, monkeypatch):
    monkeypatch.setattr("movie_analysis.openai_writer.time.sleep", lambda _: pytest.fail("must stop"))
    class HTTP:
        def request(self, *_args, **_kwargs):
            return Response(429, {"error": {"code": "insufficient_quota", "message": "No credit for sk-test-secret"}})
    cfg = MovieConfig()
    ledger = GeminiVideoClient("fake-key", cfg, tmp_path)
    with pytest.raises(legacy.BotError, match="HTTP 429") as exc:
        OpenAIWriterClient("sk-test-secret", cfg, ledger, "gpt-5.4-mini", HTTP()).generate("script", "JSON")
    assert "sk-test-secret" not in str(exc.value)
    assert "sk-test-secret" not in (tmp_path / "usage.json").read_text()
    assert len(ledger.usage["calls"]) == 1
