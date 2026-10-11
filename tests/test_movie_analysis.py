"""Evidence gates, paid-call accounting, resume and real FFmpeg smoke.

The smoke provider and TTS are synthetic test doubles, never a claimed AI evaluation.
"""
import copy
import io
import json

import pytest

import youtube_shorts_bot as legacy
from movie_analysis.core import (Cache, MovieConfig, accepted_evidence, job_lock, rights_manifest,
                                 validate_audit, validate_global, validate_scene, validate_script, validate_verdicts, read_json)
from movie_analysis.media import build_scenes, enable_local_tools, probe
from movie_analysis.pipeline import run_movie
from movie_analysis.provider import GeminiVideoClient


def claim(cid="s001_c1", kind="observed", confidence=0.98):
    return {"id": cid, "kind": kind, "modality": "visual", "start": 0.0, "end": 2.0,
            "text": "Khung hình có nền đỏ.", "evidence": "Màu đỏ phủ nền khung hình.", "confidence": confidence}


def verdict(cid="s001_c1", status="supported"):
    return {"id": cid, "status": status, "confidence": 0.98, "reason": "Nền đỏ nhìn thấy trực tiếp."}


def script():
    return {"title": "Màu đỏ và cảm giác căng thẳng", "angle": "Tác động của màu sắc", "description": "Test synthetic",
            "beats": [{"id": f"b{i:02d}", "headline": h, "visual_claim_id": "s001_c1",
                       "sentences": [{"text": text, "kind": kind, "claim_ids": ["s001_c1"]}]}
                      for i, h, text, kind in [(1, "Màu đỏ chiếm khung hình", "Khung hình có nền đỏ.", "fact"),
                                             (2, "Cảm giác từ màu sắc", "Theo góc nhìn này, màu đỏ có thể gợi cảm giác căng thẳng.", "inference")]]}


def test_complete_temporal_coverage_and_fast_shots():
    scenes = build_scenes(49, [1, 2, 3, 4, 18, 25, 36], 20, 10)
    assert scenes[0]["start"] == 0 and scenes[-1]["end"] == 49
    assert scenes[0]["fast_action"]
    assert all(b["start"] == a["end"] for a, b in zip(scenes, scenes[1:]))
    assert all(0 < s["end"] - s["start"] <= 20 for s in scenes)
    with pytest.raises(legacy.BotError):
        build_scenes(500, [], 20, 10)


def test_inference_external_uncertain_and_low_confidence_never_become_facts():
    claims = [claim(), claim("s001_c2", "inference"), claim("s001_c3", "external"),
              claim("s001_c4", confidence=0.6), claim("s001_c5")]
    evidence = accepted_evidence(claims, [verdict(c["id"], "uncertain" if c["id"] == "s001_c5" else "supported") for c in claims], 0.85)
    assert [c["id"] for c in evidence["accepted"]] == ["s001_c1"]
    assert len(evidence["rejected"]) == 4


@pytest.mark.parametrize("change", [{"start": float("nan")}, {"end": 25}, {"id": "other_c1"}, {"confidence": 1.2}, {"evidence": ""}])
def test_reject_invalid_scene_evidence(change):
    c = {**claim(), **change}
    with pytest.raises(legacy.BotError):
        validate_scene({"claims": [c]}, {"id": "s001", "start": 0, "end": 20})


def test_verifier_must_cover_every_claim_exactly_once():
    claims = [claim(), claim("s001_c2")]
    for values in ([verdict()], [verdict(), verdict()], [verdict(), verdict("imaginary")]):
        with pytest.raises(legacy.BotError):
            validate_verdicts({"verdicts": values}, claims)


def test_script_cannot_cite_rejected_claim_or_uncited_visual():
    evidence = {"accepted": [claim()]}
    data = script()
    validate_script(data, evidence)
    assert data["beats"][0]["voiceover"]
    for change in ({"claim_ids": ["unknown"]}, {"claim_ids": []}):
        bad = copy.deepcopy(data)
        bad["beats"][0]["sentences"][0].update(change)
        with pytest.raises(legacy.BotError):
            validate_script(bad, evidence)
    bad = copy.deepcopy(data)
    bad["beats"][0]["visual_claim_id"] = "unknown"
    with pytest.raises(legacy.BotError):
        validate_script(bad, evidence)


def test_script_audit_does_not_accept_truthy_strings_or_missing_sentences():
    data = script()
    audit = {"title_supported": True, "style_ok": True,
             "reviews": [{"id": "b01:0", "supported": "true", "reason": "x"},
                         {"id": "b02:0", "supported": True, "reason": "x"}]}
    with pytest.raises(legacy.BotError):
        validate_audit(audit, data)
    audit["reviews"] = audit["reviews"][1:]
    with pytest.raises(legacy.BotError):
        validate_audit(audit, data)


def test_rights_default_and_explicit_basis():
    assert not rights_manifest("unknown", "", "abc")["allow_source_visuals"]
    assert not rights_manifest("restricted", "", "abc")["allow_source_visuals"]
    with pytest.raises(legacy.BotError):
        rights_manifest("licensed", "", "abc")
    assert rights_manifest("owned", "Synthetic clip created locally", "abc")["allow_source_visuals"]


def test_documentary_cleanup_preserves_movie_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "DATA_DIR", tmp_path)
    generated = tmp_path / "generated"
    movie = generated / "movies" / "review-job"
    movie.mkdir(parents=True)
    (movie / "evidence.json").write_text('{"accepted":[]}', encoding="utf-8")
    old = generated / "long-failed"
    old.mkdir()
    (old / "debris.mp4").write_bytes(b"debris")
    assert legacy.purge_stale_generated_dirs(keep=None, max_age_hours=0) > 0
    assert (movie / "evidence.json").is_file()
    assert not old.exists()


def test_content_cache_and_os_lock(tmp_path):
    cache = Cache(tmp_path)
    calls = []
    produce = lambda: calls.append(1) or {"ok": True}
    assert cache.stage("a", ["source", "model"], produce)["ok"]
    cache.stage("a", ["source", "model"], produce)
    assert len(calls) == 1
    cache.stage("a", ["source", "new-model"], produce)
    assert len(calls) == 2
    with job_lock(tmp_path):
        with pytest.raises(legacy.BotError):
            with job_lock(tmp_path):
                pass
    with job_lock(tmp_path):
        pass


def test_rejected_response_is_preserved_and_revalidated_without_another_api_call(tmp_path):
    cache = Cache(tmp_path)
    calls = []
    def produce():
        calls.append(1)
        return {"summary": "A short clip", "events": [{"start": "0", "end": "146"}]}
    with pytest.raises(legacy.BotError, match="145.821"):
        cache.stage("global", ["source"], produce, lambda d: validate_global(d, 145.821315, 0.05))
    assert not (tmp_path / "global.json").exists()
    assert read_json(tmp_path / "global.response.json")["data"]["events"][0]["end"] == "146"
    result = cache.stage("global", ["source"], produce, lambda d: validate_global(d, 145.821315, 0.5))
    assert result["events"][0]["end"] == 145.821315 and result["events"][0]["start"] == 0
    assert calls == [1]
    assert not (tmp_path / "global.validation_error.json").exists()


def test_global_rounding_and_explicit_time_formats_are_normalized():
    data = {"summary": "A clip", "events": [{"start": "00:00", "end": "02:26"}],
            "dialogue": [{"start": "144s", "end": "146"}]}
    result = validate_global(data, 145.821315)
    assert result["events"] == [{"start": 0.0, "end": 145.821315}]
    assert result["dialogue"][0]["end"] == 145.821315


@pytest.mark.parametrize("start,end", [(0, 150), (146, 147), (10, 5), (-2, 1), (0, "02:99"), (0, float("nan"))])
def test_global_does_not_silently_rescale_substantively_wrong_timeline(start, end):
    with pytest.raises(legacy.BotError):
        validate_global({"summary": "A clip", "events": [{"start": start, "end": end}]}, 145.821315)


def test_scene_rounding_is_bounded_by_sampling_rate_and_normalized():
    c = {**claim(), "start": "20", "end": "00:40"}
    result = validate_scene({"claims": [c]}, {"id": "s001", "start": 20.02, "end": 39.98}, 0.0625)
    assert result["claims"][0]["start"] == 20.02 and result["claims"][0]["end"] == 39.98


def test_invalid_global_is_reanalyzed_and_fixed_then_cached(synthetic_clip, tmp_path):
    class GlobalRepairAI(FakeVideoAI):
        def generate(self, stage, prompt, **kwargs):
            data = super().generate(stage, prompt, **kwargs)
            if stage == "global" and sum(s == "global" for s, _ in self.calls) == 1:
                data["events"][0]["end"] = 14
            return data
    ai = GlobalRepairAI()
    directory = tmp_path / "repair-job"
    evidence = run_movie(synthetic_clip, directory, MovieConfig(), analyze_only=True, provider=ai)
    assert evidence.is_file()
    assert sum(s == "global" for s, _ in ai.calls) == 2
    assert read_json(directory / "global_attempt_1.response.json")["data"]["events"][0]["end"] == 14
    assert read_json(directory / "global.json")["data"]["events"][0]["end"] == 4
    resumed = FakeVideoAI()
    run_movie(synthetic_clip, directory, MovieConfig(), analyze_only=True, provider=resumed)
    assert resumed.calls == []


class Response:
    def __init__(self, code, data):
        self.status_code, self.data = code, data

    def json(self):
        return self.data


def test_provider_payload_retry_and_persistent_quota(tmp_path, monkeypatch):
    monkeypatch.setattr("movie_analysis.provider.time.sleep", lambda _: None)
    class HTTP:
        def __init__(self):
            self.requests = []
        def request(self, method, url, **kwargs):
            self.requests.append((url, kwargs))
            if url.endswith(":countTokens"):
                return Response(200, {"totalTokens": 100})
            if sum(u.endswith(":generateContent") for u, _ in self.requests) == 1:
                return Response(503, {})
            return Response(200, {"usageMetadata": {"totalTokenCount": 150}, "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": '{"ok":true}'}]}}]})
    cfg = MovieConfig(max_calls=2)
    http = HTTP()
    provider = GeminiVideoClient("test-key", cfg, tmp_path, http)
    assert provider.generate("test", "prompt", {"uri": "file-uri"}, (10, 20), fps=8)["ok"]
    ledger = read_json(tmp_path / "usage.json")
    assert len(ledger["calls"]) == 2
    assert ledger["calls"][0]["charged_tokens"] == 100 + cfg.max_output_tokens
    assert ledger["calls"][1]["charged_tokens"] == 150
    sent = http.requests[-1][1]["json"]["contents"][0]["parts"][0]
    assert sent["videoMetadata"] == {"fps": 8, "startOffset": "10.000s", "endOffset": "20.000s"}
    resumed = GeminiVideoClient("test-key", cfg, tmp_path, http)
    with pytest.raises(legacy.BotError, match="giới hạn"):
        resumed.generate("blocked", "prompt")
    assert sum(u.endswith(":generateContent") for u, _ in http.requests) == 2


@pytest.mark.parametrize("quota_id,retry_after,expected_requests,expected_sleep", [
    ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "3s", 1, []),
    ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "45.5s", 2, [45.5]),
    ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "120s", 1, []),
    ("", None, 1, []),
])
def test_provider_quota_diagnostics_and_bounded_retry(tmp_path, monkeypatch, quota_id,
                                                     retry_after, expected_requests, expected_sleep):
    sleeps = []
    monkeypatch.setattr("movie_analysis.provider.time.sleep", sleeps.append)
    class HTTP:
        calls = 0
        def request(self, method, url, **kwargs):
            if url.endswith(":countTokens"):
                return Response(200, {"totalTokens": 100})
            self.calls += 1
            if self.calls == 1:
                details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                    {"quotaId": quota_id, "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                     "quotaValue": "20", "description": "secret-test-key https://example.com/?key=secret-test-key"}]}]
                if retry_after is not None:
                    details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_after})
                return Response(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota secret-test-key exceeded.",
                                               "details": details}, "arbitrary": "do not persist me"})
            return Response(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok":true}'}]}}]})
    http = HTTP()
    client = GeminiVideoClient("secret-test-key", MovieConfig(), tmp_path, http)
    if expected_requests == 1:
        with pytest.raises(legacy.BotError, match="HTTP 429") as exc:
            client.generate("verify:s006", "prompt")
        assert "secret-test-key" not in str(exc.value)
    else:
        assert client.generate("verify:s006", "prompt")["ok"]
    assert http.calls == expected_requests and sleeps == expected_sleep
    info = read_json(tmp_path / "last_api_error.json")
    assert info["stage"] == "verify:s006" and info["http_status"] == 429
    saved = (tmp_path / "last_api_error.json").read_text() + (tmp_path / "usage.json").read_text()
    assert "secret-test-key" not in saved and "example.com" not in saved and "do not persist me" not in saved
    assert read_json(tmp_path / "usage.json")["calls"][0]["error"] == info


def test_count_tokens_quota_is_reported_without_generate_request(tmp_path):
    class HTTP:
        calls = 0
        def request(self, method, url, **kwargs):
            self.calls += 1
            assert url.endswith(":countTokens")
            return Response(429, {"error": {"message": "Quota exceeded"}})
    http = HTTP()
    client = GeminiVideoClient("test-key", MovieConfig(), tmp_path, http)
    with pytest.raises(legacy.BotError, match="countTokens"):
        client.generate("test", "prompt")
    assert http.calls == 1 and client.usage["calls"] == []
    assert read_json(tmp_path / "last_api_error.json")["quota_scope"] == "unknown"


def test_zero_quota_does_not_retry_even_with_minute_hint(tmp_path, monkeypatch):
    monkeypatch.setattr("movie_analysis.provider.time.sleep", lambda _: pytest.fail("should stop"))
    client = GeminiVideoClient("test-key", MovieConfig(), tmp_path)
    info = client._error_details(Response(429, {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "RequestsPerMinute", "quotaValue": "0"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "10s"}]}}), "test")
    assert client._retry_delay(info, 0) is None


def test_provider_rejects_truncated_json_but_records_usage(tmp_path):
    class HTTP:
        def request(self, method, url, **kwargs):
            return Response(200, {"totalTokens": 10}) if url.endswith(":countTokens") else Response(200, {
                "usageMetadata": {"totalTokenCount": 15}, "candidates": [{"finishReason": "MAX_TOKENS"}]})
    client = GeminiVideoClient("test-key", MovieConfig(), tmp_path, HTTP())
    with pytest.raises(legacy.BotError, match="cắt"):
        client.generate("truncated", "prompt")
    assert read_json(tmp_path / "usage.json")["calls"][0]["charged_tokens"] == 15


class FakeVideoAI:
    def __init__(self, fail_audit_once=False):
        self.calls, self.tts_calls, self.uploads = [], [], 0
        self.fail_audit_once = fail_audit_once
    def file(self, *_):
        self.uploads += 1
        return {"uri": "synthetic-test-file"}
    def record_tts(self, *data):
        self.tts_calls.append(data)
    def generate(self, stage, *args, **kwargs):
        self.calls.append((stage, kwargs))
        if stage == "global":
            return {"summary": "Nền đỏ trong clip thử tổng hợp.", "events": [{"start": 0, "end": 4, "description": "Nền đỏ"}], "characters": []}
        if stage.startswith("deep:"):
            return {"claims": [claim()]}
        if stage.startswith(("verify:", "recheck:")):
            return {"verdicts": [verdict()]}
        if stage == "review_plan":
            return {"central_question": "Màu đỏ có gì thú vị?", "thesis": "Một sắc đỏ gợi cảm giác nóng.",
                    "hook": "Đỏ thế này thì nóng mắt thật!", "introduction": "Clip thử Synthetic Film.",
                    "closing": "Bạn thấy nóng hay vui?", "sections": [
                        {"role": role, "point": "Bình luận về sắc đỏ.", "claim_ids": [claim()["id"]]}
                        for role in ("opening", "development", "closing")]}
        if stage == "script":
            result = script()
            result["beats"].append(copy.deepcopy(result["beats"][1]))
            result["beats"][2]["id"] = "b03"
            result["beats"][2]["sentences"][0]["text"] = "Bạn thấy sắc đỏ này nóng hay vui?"
            for beat, role in zip(result["beats"], ("opening", "development", "closing")):
                beat["role"] = role
            result["beats"][0]["sentences"][0]["text"] = "Synthetic Film đỏ thế này thì nóng mắt thật! Khung hình có nền đỏ."
            if sum(s == "script" for s, _ in self.calls) > 1:
                result["beats"][0]["sentences"][0]["text"] = "Synthetic Film: Khung hình đỏ làm bạn thấy nóng hay vui?"
            return result
        if stage == "script_audit":
            failed = self.fail_audit_once
            self.fail_audit_once = False
            return {"title_supported": True, "style_ok": True, "reviews": [
                {"id": "b01:0", "supported": not failed, "reason": "Synthetic audit"},
                {"id": "b02:0", "supported": True, "reason": "Qualified inference"},
                {"id": "b03:0", "supported": True, "reason": "Question reacts to observed colour"}],
                "quality": {key: {"score": 4, "reason": "Synthetic editorial fixture"} for key in
                            ("story", "natural_vietnamese", "humour", "opening", "closing")}, "repair_notes": []}
        raise AssertionError(stage)


class SyntheticTTS:
    def __init__(self):
        self.calls = 0
    def speech(self, text, path):
        self.calls += 1
        legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame", str(path)], timeout_seconds=30)


@pytest.fixture
def synthetic_clip(tmp_path, monkeypatch):
    enable_local_tools()
    import shutil
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg integration requires ffmpeg and ffprobe")
    source = tmp_path / "source.mp4"
    legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=4",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=4", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)], timeout_seconds=30)
    monkeypatch.setattr(legacy, "ensure_dejavu_font", lambda: None)
    monkeypatch.setattr(legacy, "CAPTION_FONTS_DIR", legacy.ROOT / "fonts")
    return source


def test_real_ffmpeg_end_to_end_resume_rights_and_voice_regeneration(synthetic_clip, tmp_path):
    ai, tts = FakeVideoAI(fail_audit_once=True), SyntheticTTS()
    directory = tmp_path / "job"
    cfg = MovieConfig(target_seconds=20, visual_mode="evidence")
    output = run_movie(synthetic_clip, directory, cfg, provider=ai, tts=tts)
    assert output.is_file() and read_json(directory / "quality.json")["passed"]
    assert read_json(directory / "job.json")["status"] == "review"
    assert all(t["visual_type"] == "original_graphic" for t in read_json(directory / "timeline.json"))
    assert tts.calls == 3 and ai.uploads == 1
    assert sum(stage == "script" for stage, _ in ai.calls) == 2  # audit repair actually ran
    assert probe(output)["streams"][0]["width"] == 1080
    # Resume repaired script without paying to recreate the failed first attempt.
    resumed = FakeVideoAI()
    run_movie(synthetic_clip, directory, cfg, provider=resumed, tts=tts)
    assert resumed.calls == [] and resumed.uploads == 0 and tts.calls == 3
    run_movie(synthetic_clip, directory, cfg, "owned", "Synthetic clip created by the test", provider=resumed, tts=tts)
    assert all(t["visual_type"] == "source_excerpt" for t in read_json(directory / "timeline.json"))
    assert resumed.calls == [] and tts.calls == 3
    from dataclasses import replace
    run_movie(synthetic_clip, directory, replace(cfg, visual_mode="continuous"), "owned",
              "Synthetic clip created by the test", provider=resumed, tts=tts)
    receipt = read_json(directory / "render.json")
    assert receipt["visual_mode"] == "continuous" and receipt["source_duration"] == pytest.approx(4)
    assert receipt["held_end_seconds"] == pytest.approx(2, abs=.1)
    assert resumed.calls == [] and tts.calls == 3
    run_movie(synthetic_clip, directory, cfg, "owned", "Synthetic clip created by the test", regenerate_voice=True,
              provider=resumed, tts=tts, regeneration_id="voice-request-1")
    assert resumed.calls == [] and tts.calls == 6
    run_movie(synthetic_clip, directory, cfg, "owned", "Synthetic clip created by the test", regenerate_voice=True,
              provider=resumed, tts=tts, regeneration_id="voice-request-1")
    assert tts.calls == 6


@pytest.mark.parametrize("voice_seconds,source_audio", [(1, False), (4, True)])
def test_continuous_playback_keeps_all_scenes_and_holds_only_the_end(tmp_path, voice_seconds, source_audio):
    import subprocess
    import shutil
    from PIL import Image
    from movie_analysis.media import continuous_media, duration
    enable_local_tools()
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg integration requires ffmpeg")
    source, voice = tmp_path / "ordered.mp4", tmp_path / "voice.mp3"
    command = ["ffmpeg", "-y"]
    for colour in ("red", "lime", "blue"):
        command += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=30:d=1"]
    if source_audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=220:duration=3"]
    command += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]"]
    if source_audio:
        command += ["-map", "3:a", "-c:a", "aac"]
    legacy.run(command + ["-c:v", "libx264", str(source)], timeout_seconds=30)
    legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={voice_seconds}",
                "-c:a", "libmp3lame", str(voice)], timeout_seconds=30)
    expected = max(duration(source), duration(voice))
    visuals, mixed = continuous_media(source, tmp_path, voice, expected, duration(source))
    assert abs(duration(visuals) - expected) < .1
    assert abs(duration(mixed) - expected) < .1
    # Even with shorter narration, the final blue scene must survive. Longer narration must hold blue.
    for index, timestamp in enumerate((.5, 1.5, 2.5)):
        frame = tmp_path / f"frame-{index}.png"
        legacy.run(["ffmpeg", "-y", "-ss", str(timestamp), "-i", str(visuals),
                    "-frames:v", "1", str(frame)], timeout_seconds=30)
        pixel = Image.open(frame).convert("RGB").getpixel((540, 960))
        assert pixel[index] > 200 and all(pixel[j] < 20 for j in range(3) if j != index)
    if voice_seconds > 3:
        tail = tmp_path / "tail.png"
        legacy.run(["ffmpeg", "-y", "-ss", "3.5", "-i", str(visuals), "-frames:v", "1", str(tail)], timeout_seconds=30)
        assert Image.open(tail).convert("RGB").getpixel((540, 960))[2] > 200
        # Check original audio remains quietly underneath the review voice, with neither track replayed.
        import array
        import math
        result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mixed), "-ss", "0.3", "-t", "0.5",
                                 "-ar", "16000", "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True)
        samples = array.array("f", result.stdout)
        def amplitude(frequency):
            real = sum(v * math.cos(2 * math.pi * frequency * i / 16000) for i, v in enumerate(samples))
            imag = sum(v * math.sin(2 * math.pi * frequency * i / 16000) for i, v in enumerate(samples))
            return 2 * math.hypot(real, imag) / len(samples)
        assert .05 < amplitude(220) / amplitude(440) < .25


def test_upload_review_auth_and_csrf(synthetic_clip, monkeypatch, tmp_path):
    import web_app
    web_app.reset_archive()
    monkeypatch.setattr(web_app, "Archive", lambda: legacy.Archive(tmp_path / "queue.db"))
    monkeypatch.setattr(web_app, "ACCESS_TOKEN", "test-secret")
    monkeypatch.setattr(legacy, "DATA_DIR", tmp_path)
    client = web_app.app.test_client()
    assert client.get("/movies").status_code == 302
    with client.session_transaction() as sess:
        sess["authed"] = True
    assert client.get("/movies").status_code == 200
    with client.session_transaction() as sess:
        csrf = sess["movie_csrf"]
    assert client.post("/movies/submit").status_code == 400
    response = client.post("/movies/submit", data={"csrf": csrf, "target_seconds": "60", "rights_status": "unknown",
                           "video": (io.BytesIO(synthetic_clip.read_bytes()), "my movie.mp4")})
    assert response.status_code == 302
    assert client.get(response.headers["Location"]).status_code == 200
    idea_id = int(response.headers["Location"].rsplit("/", 1)[1])
    row = web_app.get_archive().get_idea(idea_id)
    assert row["mode"] == "movie" and not row["publish"]
    assert client.get(f"/movies/{idea_id}/media/source.mp4").status_code == 404
    assert client.get(f"/movies/{idea_id}/media/movie_review.mp4").status_code == 404
    web_app.get_archive().update_idea(idea_id, "failed", error="Synthetic failure")
    response = client.post(f"/movies/{idea_id}/retry", data={"csrf": csrf, "action": "resume"})
    assert response.status_code == 302
    assert client.post(f"/movies/{idea_id}/retry", data={"csrf": csrf, "action": "resume"}).status_code == 409
    web_app.reset_archive()
