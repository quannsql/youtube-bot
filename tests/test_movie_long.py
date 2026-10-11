"""Long-source admission, timestamp scope, partial resume and real FFmpeg output.

AI/TTS responses are synthetic: these checks make no external API requests.
"""
from dataclasses import replace
import re

import pytest

import youtube_shorts_bot as legacy
from movie_analysis.characters import recognize_character_chunks
from movie_analysis.chunks import analyze_chunks, scene_windows, session_seconds, validate_chunk, windows
from movie_analysis.core import Cache, MovieConfig, read_json, validate_global, write_json
from movie_analysis.live_provider import GeminiLiveVideoClient, live_reservation
from movie_analysis.media import build_scenes, enable_local_tools, duration
from movie_analysis.pipeline import run_movie
from test_movie_analysis import FakeVideoAI, SyntheticTTS, claim
from test_movie_review import ctx


def config(**changes):
    return replace(MovieConfig(), video_provider="gemini_live", model="gemini-3.8-live",
                   verifier_model="gemini-3.8-live", **changes)


@pytest.mark.parametrize("seconds", [120, 180, 300])
def test_long_windows_cover_source_without_exceeding_session_budget(seconds):
    cfg = config()
    parts = windows(seconds, session_seconds(cfg))
    assert parts[0]["start"] == 0 and parts[-1]["end"] == seconds
    assert all(a["end"] == b["start"] for a, b in zip(parts, parts[1:]))
    assert all(live_reservation("instructions" * 300, p["end"] - p["start"],
               round(p["end"] - p["start"]), cfg.max_output_tokens) <= cfg.live_tpm * .8 for p in parts)
    scenes = build_scenes(seconds, list(range(0, seconds, 9)), cfg.max_scene_seconds, cfg.max_scenes)
    groups = scene_windows(scenes, session_seconds(cfg))
    assert len(scenes) <= cfg.max_scenes
    assert groups[-1]["end"] == seconds
    for scene in scenes:
        assert any(w["start"] <= scene["start"] < scene["end"] <= w["end"] for w in groups)


def test_lower_tpm_shortens_sessions_and_impossible_quota_stops():
    assert session_seconds(config(live_tpm=40000)) < 45
    with pytest.raises(legacy.BotError, match="TPM"):
        session_seconds(config(live_tpm=20000))


def test_global_chunks_validate_absolute_scope_and_resume_after_interruption(tmp_path):
    calls = []
    fail = [True]
    def ask(stage, prompt, span):
        calls.append(span["id"])
        if span["id"] == "c003" and fail[0]:
            raise legacy.BotError("Simulated 503")
        assert "ABSOLUTE" in prompt
        return {"summary": "Synthetic window", "characters": [], "events": [
            {"start": span["start"], "end": span["end"], "description": "Synthetic"}], "dialogue": []}
    info = {"duration": 300, "has_audio": True}
    with pytest.raises(legacy.BotError, match="503"):
        analyze_chunks(Cache(tmp_path), tmp_path, info, config(), ["source"], ask, lambda *_: None)
    fail[0] = False
    merged = analyze_chunks(Cache(tmp_path), tmp_path, info, config(), ["source"], ask, lambda *_: None)
    assert calls.count("c001") == calls.count("c002") == 1
    assert calls.count("c003") == 2
    assert merged["events"][-1]["end"] == 300
    assert read_json(tmp_path / "analysis_chunks.json")["chunks"][-1]["end"] == 300
    before = list(calls)
    analyze_chunks(Cache(tmp_path), tmp_path, info, config(), ["source"], ask, lambda *_: None)
    assert calls == before
    with pytest.raises(legacy.BotError):
        validate_global({"summary": "Wrong origin", "events": [{"start": 0, "end": 10}]}, 90, .5, 45)


def test_global_invalid_window_retries_only_that_window(tmp_path):
    calls = []
    def ask(stage, prompt, span):
        calls.append(span["id"])
        start = 0 if span["id"] == "c002" and calls.count("c002") == 1 else span["start"]
        return {"summary": "Synthetic", "events": [{"start": start, "end": span["end"]}]}
    result = analyze_chunks(Cache(tmp_path), tmp_path, {"duration": 120, "has_audio": False},
                            config(), [], ask, lambda *_: None)
    assert calls == ["c001", "c002", "c002", "c003"]
    assert result["events"][1]["start"] == 45


@pytest.mark.parametrize("extra", [{"characters": ["unnamed man"]}, {"angles": "not a list"}, {"uncertainties": [None]}])
def test_global_chunk_rejects_malformed_context_before_caching(extra):
    with pytest.raises(legacy.BotError):
        validate_chunk({"summary": "Synthetic", "events": [{"start": 45, "end": 90}], **extra},
                       {"start": 45, "end": 90})


@pytest.mark.parametrize("second_supported", [True, False])
def test_character_names_merge_only_independently_verified_windows(tmp_path, second_supported):
    first = claim()
    second = {**claim("s002_c1"), "start": 50, "end": 52}
    evidence = {"accepted": [first, second]}
    parts = windows(90, 45)
    calls = []
    def ask(stage, prompt, span, **kwargs):
        calls.append((stage, span["id"]))
        reference = first["id"] if span["start"] == 0 else second["id"]
        other = second["id"] if span["start"] == 0 else first["id"]
        assert reference in prompt and other not in prompt
        if stage == "character_identity":
            return {"matches": [{"character_id": "char_red", "claim_ids": [reference],
                "confidence": .99, "cues": ["face", "setting"], "reason": "Synthetic cues"}]}
        return {"verdicts": [{"id": "char_red", "status": "supported" if span["start"] == 0 or second_supported else "uncertain",
                              "confidence": .99, "reason": "Independent synthetic check"}]}
    result = recognize_character_chunks(tmp_path, "source", config(), evidence, ctx(), parts, ask, lambda *_: None)
    refs = result["recognized_characters"][0]["claim_ids"]
    assert refs == [first["id"], second["id"]] if second_supported else refs == [first["id"]]
    assert result["facts"][-1]["claim_ids"] == refs
    def blocked(*_args, **_kwargs):
        raise AssertionError("Cached identity must not call API again")
    cached = recognize_character_chunks(tmp_path, "source", config(), evidence, ctx(), parts, blocked, lambda *_: None)
    assert cached == result


def test_live_rejects_oversized_prompt_before_extracting_or_sending(tmp_path, monkeypatch):
    client = GeminiLiveVideoClient("fake-key", config(), tmp_path, sdk_client=object(), rate_budget=object())
    monkeypatch.setattr(client, "_prepare", lambda *_: pytest.fail("No extraction before admission"))
    with pytest.raises(legacy.BotError, match="TPM"):
        client.generate("global:c001", "x" * 100000, remote={"duration": 300}, span=(0, 45))
    assert client.usage["calls"] == []


def test_live_admits_long_source_but_never_prepares_a_whole_long_session(tmp_path, monkeypatch):
    monkeypatch.setattr("movie_analysis.live_provider.probe", lambda *_: {
        "format": {"duration": "300"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})
    client = GeminiLiveVideoClient("fake-key", config(), tmp_path, sdk_client=object(), rate_budget=object())
    remote = client.file(tmp_path / "clip.mp4", "hash")
    assert remote["duration"] == 300
    with pytest.raises(legacy.BotError, match="chia đoạn"):
        client._prepare(remote, None, 1)


def test_live_transport_retry_reserves_each_attempt_without_exceeding_count(tmp_path, monkeypatch):
    class Budget:
        def __init__(self):
            self.reservations = []
        def reserve(self, tokens):
            self.reservations.append(tokens)
    budget = Budget()
    client = GeminiLiveVideoClient("fake-key", config(max_calls=2), tmp_path, sdk_client=object(), rate_budget=budget)
    monkeypatch.setattr(client, "_prepare", lambda *_: {"seconds": 20, "frames": [], "audio": b"", "fps": 1})
    monkeypatch.setattr("movie_analysis.live_provider.time.sleep", lambda *_: None)
    attempts = []
    async def exchange(*_args):
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("Simulated transport timeout")
        return {"summary": "ok"}
    monkeypatch.setattr(client, "_exchange", exchange)
    assert client.generate("global:c001", "prompt") == {"summary": "ok"}
    rows = read_json(tmp_path / "usage.json")["calls"]
    assert [r["status"] for r in rows] == ["failed", "received"]
    assert len(budget.reservations) == 2
    assert all(t >= client.cfg.max_output_tokens for t in budget.reservations)
    with pytest.raises(legacy.BotError, match="MAX_CALLS"):
        client.generate("global:c002", "prompt")


def test_two_minute_pipeline_resumes_mid_identity_without_repeating_analysis(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic pipeline fixture; media is mocked")
    profile = tmp_path / "profile.json"
    write_json(profile, ctx())
    monkeypatch.setattr("movie_analysis.pipeline.inspect_source", lambda *_: {"duration": 120, "has_audio": True})
    monkeypatch.setattr("movie_analysis.pipeline.detect_scenes", lambda *_: build_scenes(120, [], 20, 30))
    monkeypatch.setattr(legacy, "require_tools", lambda: None)

    class IdentityAI(FakeVideoAI):
        def __init__(self, fail=False):
            super().__init__()
            self.fail = fail
        def generate(self, stage, *args, **kwargs):
            span = kwargs.get("span")
            if stage.startswith("global:"):
                self.calls.append((stage, kwargs))
                return {"summary": "Synthetic", "events": [{"start": span[0], "end": span[1]}]}
            if stage.startswith("deep:"):
                self.calls.append((stage, kwargs))
                return {"claims": [{**claim(stage.split(":")[1] + "_c1"), "start": span[0], "end": span[0] + 2}]}
            if stage.startswith("verify:"):
                self.calls.append((stage, kwargs))
                return {"verdicts": [{"id": stage.split(":")[1] + "_c1", "status": "supported", "confidence": .99, "reason": "Synthetic"}]}
            if stage.startswith("character_identity"):
                self.calls.append((stage, kwargs))
                assert span and span[1] - span[0] <= 45
                if stage == "character_identity":
                    refs = re.findall(r'"id": "(s\d{3}_c1)"', args[0])
                    return {"matches": [{"character_id": "char_red", "claim_ids": [refs[0]],
                        "confidence": .99, "cues": ["face", "setting"], "reason": "Synthetic"}]}
                if self.fail and span[0] == 40:
                    raise legacy.BotError("Simulated identity interruption")
                return {"verdicts": [{"id": "char_red", "status": "supported", "confidence": .99, "reason": "Synthetic"}]}
            return super().generate(stage, *args, **kwargs)

    job = tmp_path / "job"
    with pytest.raises(legacy.BotError, match="interruption"):
        run_movie(source, job, config(), provider=IdentityAI(fail=True), context_file=profile, script_only=True)
    resumed = IdentityAI()
    output = run_movie(source, job, config(), provider=resumed, context_file=profile, script_only=True)
    assert output.is_file()
    stages = [s for s, _ in resumed.calls]
    assert stages == ["character_identity_verify", "character_identity", "character_identity_verify",
                      "review_plan", "script", "script_audit"]
    accepted = read_json(job / "characters.json")["accepted"]
    assert len(accepted) == 3
    assert read_json(job / "global.json")["data"]["events"][-1]["end"] == 120


def test_real_five_minute_source_analysis_render_and_cached_resume(tmp_path, monkeypatch):
    enable_local_tools()
    source = tmp_path / "five-minutes.mp4"
    legacy.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=1:d=150",
                "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=1:d=150", "-f", "lavfi", "-i",
                "sine=frequency=220:duration=300", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", "-t", "300", str(source)], timeout_seconds=120)
    monkeypatch.setattr(legacy, "ensure_dejavu_font", lambda: None)
    monkeypatch.setattr(legacy, "CAPTION_FONTS_DIR", legacy.ROOT / "fonts")

    class LongAI(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            span = kwargs.get("span")
            if stage.startswith("global:"):
                self.calls.append((stage, kwargs))
                assert span and span[1] - span[0] <= 45
                return {"summary": "Synthetic colours", "characters": [], "events": [
                    {"start": span[0], "end": span[1], "description": "Synthetic colour window"}]}
            if stage.startswith("deep:"):
                self.calls.append((stage, kwargs))
                sid = stage.split(":")[1]
                return {"claims": [{**claim(sid + "_c1"), "start": span[0], "end": min(span[0] + 2, span[1])}]}
            if stage.startswith(("verify:", "recheck:")):
                self.calls.append((stage, kwargs))
                return {"verdicts": [{"id": stage.split(":")[1] + "_c1", "status": "supported",
                                     "confidence": .99, "reason": "Synthetic confirmation"}]}
            return super().generate(stage, *args, **kwargs)

    ai, tts = LongAI(), SyntheticTTS()
    job = tmp_path / "job"
    output = run_movie(source, job, config(), "owned", "Synthetic fixture created locally", provider=ai, tts=tts)
    assert abs(duration(output) - 300) < .4
    assert read_json(job / "quality.json")["passed"]
    assert read_json(job / "render.json")["held_end_seconds"] == 0
    assert len([s for s, _ in ai.calls if s.startswith("global:")]) == 7
    assert all(k["span"] and k["span"][1] - k["span"][0] <= 45 for s, k in ai.calls
               if s.startswith(("global:", "deep:", "verify:")))
    assert read_json(job / "scenes.json")[-1]["end"] == 300
    frame = tmp_path / "end.jpg"
    legacy.run(["ffmpeg", "-y", "-ss", "295", "-i", str(output), "-frames:v", "1", str(frame)], timeout_seconds=30)
    from PIL import Image
    assert Image.open(frame).convert("RGB").getpixel((540, 960))[2] > 200
    cached_ai, cached_tts = LongAI(), SyntheticTTS()
    run_movie(source, job, config(), "owned", "Synthetic fixture created locally", provider=cached_ai, tts=cached_tts)
    assert cached_ai.calls == [] and cached_ai.uploads == 0 and cached_tts.calls == 0
