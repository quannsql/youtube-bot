import importlib.util
import json
import base64
import sys
from pathlib import Path
import requests
import pytest

SPEC = importlib.util.spec_from_file_location("bot", Path(__file__).parents[1] / "youtube_shorts_bot.py")
bot = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = bot
SPEC.loader.exec_module(bot)


def test_similarity_normalizes_vietnamese_accents():
    assert bot.similarity("Đế chế La Mã biến mất", "De che La Ma bien mat") == 1


def test_plan_parser_accepts_valid_plan():
    plan = bot.ShortPlan.from_dict({
        "topic": "T", "angle": "A", "title": "Title", "description": "#Shorts", "tags": ["shorts"],
        "hook": "A short script.", "narration": "A short script.", "closing_line": "A short script.",
        "fact_note": "No exaggeration", "source_hints": ["Smithsonian"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene", "on_screen_text": "A mystery"}],
    })
    assert plan.scenes[0].duration == 4


def test_archive_blocks_exactly_repeated_plan(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Sự kiện Tunguska", "angle": "Vụ nổ năm 1908", "title": "Điều gì đã làm rung chuyển Siberia?",
        "description": "#Shorts", "tags": ["shorts"], "narration": "Một kịch bản ngắn.",
        "fact_note": "Không cường điệu", "source_hints": ["NASA"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene", "on_screen_text": "Bí ẩn"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(plan, tmp_path / "output")
    assert archive.duplicate_of(plan) is not None


def test_research_prompt_receives_archive_and_rejected_candidates(tmp_path):
    archived = bot.ShortPlan.from_dict({
        "topic": "Antikythera mechanism", "angle": "Ancient gears predicted eclipses",
        "title": "Ancient Gears Predicted Eclipses", "description": "#Shorts",
        "tags": ["shorts", "history"], "narration": "A short script.",
        "fact_note": "Avoids overclaiming precision", "source_hints": ["Museum"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    fresh_plan = {
        "topic": "The Green Sahara", "angle": "A desert shaped by monsoon shifts",
        "title": "When the Sahara Was Green", "description": "A desert was once a lake country. #Shorts",
        "tags": ["shorts", "science", "history"],
        "hook": "Twelve thousand years ago, the Sahara was green.",
        "narration": "Twelve thousand years ago, the Sahara was green. Monsoon rains fed lakes across North Africa, supporting grasslands and animals. Sediment evidence shows this humid period varied by region. Then Earth's orbit helped weaken the rains. A desert can be a climate snapshot, not a permanent identity.",
        "closing_line": "A desert can be a climate snapshot, not a permanent identity.",
        "scenes": [{"duration": 5, "visual_prompt": "Scene one"}, {"duration": 5, "visual_prompt": "Scene two"}, {"duration": 5, "visual_prompt": "Scene three"}, {"duration": 5, "visual_prompt": "Scene four"}],
        "fact_note": "Regional timing varied.", "source_hints": ["NOAA"],
    }
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(archived, tmp_path / "old")
    prompts = []

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"central_claim": "The Sahara had wet periods.", "evidence_points": ["lakes", "sediment", "monsoons"], "uncertainty": "regional timing", "fresh_angle": "climate snapshot", "source_leads": ["NOAA"], "avoid": ["exact dates"]},
                fresh_plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": fresh_plan},
            ]

        def chat(self, prompt, temperature=0.55):
            prompts.append(prompt)
            return json.dumps(self.responses.pop(0))

    bot.plan_short(
        FakeClient(),
        archive,
        "little-known discoveries",
        20,
        [{"topic": "Antikythera mechanism", "angle": "eclipses", "title": "Ancient Greek Gears That Predicted Eclipses"}],
    )

    assert "Existing archive" in prompts[0]
    assert "Ancient Gears Predicted Eclipses" in prompts[0]
    assert "Rejected candidates from this run" in prompts[0]
    assert "Ancient Greek Gears That Predicted Eclipses" in prompts[0]
    assert "not photorealistic" in prompts[1]
    assert "stylized animated documentary explainer" in prompts[1]
    assert "Preserve this visual direction" in prompts[2]


def test_choose_novel_plan_passes_duplicate_candidate_to_retry(tmp_path, monkeypatch):
    duplicate_plan = bot.ShortPlan.from_dict({
        "topic": "Antikythera mechanism", "angle": "Ancient gears predicted eclipses",
        "title": "Ancient Gears Predicted Eclipses", "description": "#Shorts",
        "tags": ["shorts"], "narration": "A short script.",
        "fact_note": "Avoids overclaiming precision", "source_hints": ["Museum"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    fresh_plan = bot.ShortPlan.from_dict({
        "topic": "The Green Sahara", "angle": "A desert shaped by monsoon shifts",
        "title": "When the Sahara Was Green", "description": "#Shorts",
        "tags": ["shorts"], "narration": "A short script.",
        "fact_note": "Regional timing varied", "source_hints": ["NOAA"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(duplicate_plan, tmp_path / "old")
    rejected_seen = []

    def fake_plan_short(_client, _archive, _theme, _duration, rejected=None):
        rejected_seen.append(list(rejected or []))
        return duplicate_plan if len(rejected_seen) == 1 else fresh_plan

    monkeypatch.setattr(bot, "plan_short", fake_plan_short)

    result = bot.choose_novel_plan(object(), archive, "little-known discoveries", 20, max_attempts=2)

    assert result == fresh_plan
    assert rejected_seen[0] == []
    assert rejected_seen[1][0]["title"] == "Ancient Gears Predicted Eclipses"
    assert rejected_seen[1][0]["matched_existing_title"] == "Ancient Gears Predicted Eclipses"


def test_archive_counts_jobs_created_today(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title", "description": "#Shorts", "tags": ["shorts"],
        "narration": "A short script.", "fact_note": "No exaggeration", "source_hints": ["Museum"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    for number in range(3):
        current = bot.ShortPlan.from_dict({**plan.to_dict(), "title": f"Title {number}"})
        archive.reserve(current, tmp_path / f"output-{number}")
    assert archive.jobs_created_today() == 3


def test_materialize_credential_file_from_base64(tmp_path, monkeypatch):
    destination = tmp_path / "credential.json"
    monkeypatch.setenv("TEST_CREDENTIAL_B64", base64.b64encode(b'{"credential": true}').decode())
    bot.materialize_credential_file("TEST_CREDENTIAL_B64", destination)
    assert destination.read_bytes() == b'{"credential": true}'


def test_settings_accepts_25_second_duration(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_GROK_API_KEY", "grok")
    monkeypatch.setenv("POLLINATIONS_VIDEO_API_KEY", "video")
    monkeypatch.setenv("SHORT_DURATION_SECONDS", "25")

    assert bot.Settings.from_env().duration == 25


def test_settings_reads_scheduled_daily_limit(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_GROK_API_KEY", "grok")
    monkeypatch.setenv("POLLINATIONS_VIDEO_API_KEY", "video")
    monkeypatch.setenv("SCHEDULED_DAILY_LIMIT", "6")

    assert bot.Settings.from_env().scheduled_daily_limit == 6


def test_ltx_scene_prompt_adds_animation_style_guardrails():
    prompt = bot.ltx_scene_prompt("A fossil leaf drifts across ancient continents.")

    assert prompt.startswith("A fossil leaf")
    assert "Style guardrails" in prompt
    assert "not photorealistic" in prompt
    assert "live-action" in prompt


def test_ltx_video_retries_transient_download_failure(tmp_path, monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        ok = True

        def iter_content(self, _chunk_size):
            yield b"x" * 2048

    def fake_request(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.Timeout("read timed out")
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "request", fake_request)
    settings = bot.Settings(
        grok_api_key="grok",
        video_api_key="video",
        video_scene_attempts=2,
        video_scene_retry_backoff_seconds=0,
    )
    destination = tmp_path / "scene_1.mp4"

    bot.Pollinations(settings).video("a prompt", 4, destination, seed=123)

    assert calls["count"] == 2
    assert destination.read_bytes() == b"x" * 2048
    assert not (tmp_path / "scene_1.mp4.part").exists()


def test_ltx_video_falls_back_to_grok_key_on_quota_error(tmp_path, monkeypatch):
    authorizations = []

    class FakeQuotaResponse:
        ok = False
        status_code = 429
        text = "quota exceeded"

    class FakeVideoResponse:
        ok = True

        def iter_content(self, _chunk_size):
            yield b"y" * 2048

    def fake_request(*_args, **kwargs):
        authorizations.append(kwargs["headers"]["Authorization"])
        if kwargs["headers"]["Authorization"] == "Bearer video":
            return FakeQuotaResponse()
        return FakeVideoResponse()

    monkeypatch.setattr(bot.requests, "request", fake_request)
    settings = bot.Settings(
        grok_api_key="grok",
        video_api_key="video",
        video_scene_attempts=1,
        video_scene_retry_backoff_seconds=0,
    )
    destination = tmp_path / "scene_1.mp4"

    bot.Pollinations(settings).video("a prompt", 4, destination, seed=123)

    assert authorizations == ["Bearer video", "Bearer grok"]
    assert destination.read_bytes() == b"y" * 2048


def test_tts_language_code_supports_english_and_vietnamese_voices():
    assert bot.GoogleChirpTTS.language_code_for_voice("en-US-Chirp3-HD-Achernar") == "en-US"
    assert bot.GoogleChirpTTS.language_code_for_voice("vi-VN-Standard-A") == "vi-VN"


def test_facebook_page_upload_posts_video(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"video")
    calls = {}

    class FakeResponse:
        ok = True
        text = '{"id":"fb123"}'

        def json(self):
            return {"id": "fb123"}

    def fake_post(url, data=None, files=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        calls["files"] = files
        calls["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(grok_api_key="grok", video_api_key="video", facebook_page_id="page1", facebook_page_access_token="token")
    social = bot.SocialPlan(title="Tieu de", description="Mo ta", tags=["tag"], narration="Loi doc")

    assert bot.upload_to_facebook_page(video, social, settings) == "fb123"
    assert calls["url"].endswith("/page1/videos")
    assert calls["data"]["description"] == "Mo ta"


def test_facebook_upload_retries_with_user_token_when_page_token_expired(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"video")
    posts = []
    gets = []

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code
            self.ok = status_code < 400
            self.text = json.dumps(payload)

        def json(self):
            return self.payload

    def fake_post(url, data=None, files=None, timeout=None):
        posts.append({"url": url, "data": data, "files": files, "timeout": timeout})
        if len(posts) == 1:
            return FakeResponse(
                {
                    "error": {
                        "message": "Error validating access token: Session has expired.",
                        "type": "OAuthException",
                        "code": 190,
                        "error_subcode": 463,
                    }
                },
                status_code=400,
            )
        return FakeResponse({"id": "fb123"})

    def fake_get(url, params=None, timeout=None):
        gets.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse({"data": [{"id": "page1", "name": "Page", "access_token": "fresh-page-token"}]})

    monkeypatch.setattr(bot.requests, "post", fake_post)
    monkeypatch.setattr(bot.requests, "get", fake_get)
    settings = bot.Settings(
        grok_api_key="grok",
        video_api_key="video",
        facebook_page_id="page1",
        facebook_page_access_token="expired-page-token",
        facebook_user_access_token="user-token",
    )
    social = bot.SocialPlan(title="Tieu de", description="Mo ta", tags=["tag"], narration="Loi doc")

    assert bot.upload_to_facebook_page(video, social, settings) == "fb123"
    assert [post["data"]["access_token"] for post in posts] == ["expired-page-token", "fresh-page-token"]
    assert gets[0]["params"]["access_token"] == "user-token"


def test_facebook_expired_page_token_error_is_actionable(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"video")

    class FakeResponse:
        ok = False
        status_code = 400
        text = '{"error":{"message":"Session has expired.","code":190,"error_subcode":463}}'

        def json(self):
            return {"error": {"message": "Session has expired.", "code": 190, "error_subcode": 463}}

    monkeypatch.setattr(bot.requests, "post", lambda *_args, **_kwargs: FakeResponse())
    settings = bot.Settings(
        grok_api_key="grok",
        video_api_key="video",
        facebook_page_id="page1",
        facebook_page_access_token="expired-page-token",
    )
    social = bot.SocialPlan(title="Tieu de", description="Mo ta", tags=["tag"], narration="Loi doc")

    with pytest.raises(bot.FacebookAPIError) as exc_info:
        bot.upload_to_facebook_page(video, social, settings)

    assert "access token da het han" in str(exc_info.value)
    assert "FACEBOOK_USER_ACCESS_TOKEN" in str(exc_info.value)


def test_tiktok_direct_post_uploads_whole_file(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"z" * 2048)
    posts = []
    puts = []

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code
            self.ok = status_code < 400
            self.text = json.dumps(payload)

        def json(self):
            return self.payload

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if url.endswith("/creator_info/query/"):
            return FakeResponse({"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok", "message": ""}})
        return FakeResponse({"data": {"publish_id": "tt123", "upload_url": "https://upload.example/video"}, "error": {"code": "ok", "message": ""}})

    def fake_put(url, headers=None, data=None, timeout=None):
        puts.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        return FakeResponse({}, status_code=201)

    monkeypatch.setattr(bot.requests, "post", fake_post)
    monkeypatch.setattr(bot.requests, "put", fake_put)
    settings = bot.Settings(grok_api_key="grok", video_api_key="video", tiktok_access_token="token")
    social = bot.SocialPlan(title="Tieu de", description="Mo ta #lichsu", tags=["lichsu"], narration="Loi doc")

    assert bot.upload_to_tiktok(video, social, settings) == "tt123"
    init_body = posts[1]["json"]
    assert init_body["source_info"]["source"] == "FILE_UPLOAD"
    assert init_body["source_info"]["total_chunk_count"] == 1
    assert puts[0]["headers"]["Content-Range"] == "bytes 0-2047/2048"


def test_twenty_second_story_accepts_shorter_narration(tmp_path):
    plan = {
        "topic": "Antikythera mechanism", "angle": "Ancient gears modeled the sky",
        "title": "Ancient Greek Gears Tracked the Sky", "description": "Ancient gears modeled the sky. #Shorts",
        "tags": ["shorts", "history", "science"],
        "hook": "A tiny bronze machine once modeled the sky.",
        "narration": "A tiny bronze machine once modeled the sky. Its gears tracked lunar cycles, eclipses, and festival calendars. Found in a shipwreck, it showed Greek engineers built portable astronomy long before telescopes changed time.",
        "closing_line": "Found in a shipwreck, it showed Greek engineers built portable astronomy long before telescopes changed time.",
        "scenes": [{"duration": 4, "visual_prompt": "Scene one"}, {"duration": 4, "visual_prompt": "Scene two"}, {"duration": 4, "visual_prompt": "Scene three"}, {"duration": 4, "visual_prompt": "Scene four"}, {"duration": 4, "visual_prompt": "Scene five"}],
        "fact_note": "Avoids claiming the mechanism predicted every event perfectly.", "source_hints": ["National Archaeological Museum, Athens"],
    }

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"central_claim": "The mechanism modeled cycles.", "evidence_points": ["gears", "lunar cycles", "eclipses"], "uncertainty": "some functions remain debated", "fresh_angle": "portable astronomy", "source_leads": ["National Archaeological Museum, Athens"], "avoid": ["modern computer overclaim"]},
                plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": plan},
            ]

        def chat(self, _prompt, temperature=0.55):
            return json.dumps(self.responses.pop(0))

    result = bot.plan_short(FakeClient(), bot.Archive(tmp_path / "shorts.db"), "ancient technology", 20)
    assert len(result.narration.split()) < 40
    assert sum(scene.duration for scene in result.scenes) == 20


def test_three_pass_planner_returns_a_20_second_story(tmp_path):
    plan = {
        "topic": "The Green Sahara", "angle": "A desert shaped by monsoon shifts",
        "title": "When the Sahara Was Green", "description": "A desert was once a lake country. #Shorts",
        "tags": ["shorts", "science", "history"],
        "hook": "Twelve thousand years ago, the Sahara was green.",
        "narration": "Twelve thousand years ago, the Sahara was green. Monsoon rains fed lakes across North Africa, supporting grasslands and animals. Sediment and archaeological evidence reveal this African Humid Period, though its timing varied by region. Then Earth's orbital shifts weakened the monsoons. A desert can be a climate snapshot, not a permanent identity.",
        "closing_line": "A desert can be a climate snapshot, not a permanent identity.",
        "scenes": [{"duration": 5, "visual_prompt": "Scene one"}, {"duration": 5, "visual_prompt": "Scene two"}, {"duration": 5, "visual_prompt": "Scene three"}, {"duration": 5, "visual_prompt": "Scene four"}],
        "fact_note": "Regional timing varied.", "source_hints": ["NOAA", "peer-reviewed paleoclimate research"],
    }

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"central_claim": "The Sahara had wet periods.", "evidence_points": ["lakes", "sediment", "monsoons"], "uncertainty": "regional timing", "fresh_angle": "climate snapshot", "source_leads": ["NOAA"], "avoid": ["exact dates"]},
                plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": plan},
            ]

        def chat(self, _prompt, temperature=0.55):
            return json.dumps(self.responses.pop(0))

    result = bot.plan_short(FakeClient(), bot.Archive(tmp_path / "shorts.db"), "green deserts", 20)
    assert result.hook == plan["hook"]
    assert sum(scene.duration for scene in result.scenes) == 20
