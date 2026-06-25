"""Generate factual, artistic English YouTube Shorts with Pollinations and FFmpeg.

The script intentionally separates planning, rendering, and publishing.  It never
uploads unless --publish is supplied, but a scheduled task can use that flag.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
_configured_data_dir = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT)
DATA_DIR = _configured_data_dir if _configured_data_dir.is_absolute() else ROOT / _configured_data_dir
LOG = logging.getLogger("shorts_bot")

# Windows PowerShell sessions can still inherit cp1252. Keep CLI output
# deterministic instead of failing on non-ASCII text in paths or user themes.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


class BotError(RuntimeError):
    pass


class PollinationsTransientError(BotError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PollinationsQuotaError(PollinationsTransientError):
    pass


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def looks_like_quota_error(status_code: int, detail: str) -> bool:
    lowered = detail.lower()
    quota_terms = ("quota", "credit", "balance", "pollen", "limit", "rate", "too many", "insufficient")
    return status_code in {402, 429} or (status_code in {400, 403} and any(term in lowered for term in quota_terms))


def materialize_credential_file(env_name: str, destination: Path) -> None:
    """Decode a Railway secret variable to the Volume when a file is unavailable."""
    encoded = os.getenv(env_name, "")
    if not encoded:
        return
    try:
        content = base64.b64decode("".join(encoded.split()), validate=True)
    except ValueError as exc:
        raise BotError(f"{env_name} không phải Base64 hợp lệ.") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def materialize_railway_credentials() -> None:
    materialize_credential_file(
        "GOOGLE_TTS_SERVICE_ACCOUNT_JSON_B64",
        DATA_DIR / os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_FILE", "google_tts_service_account.json"),
    )
    materialize_credential_file(
        "YOUTUBE_CLIENT_SECRETS_JSON_B64",
        DATA_DIR / os.getenv("YOUTUBE_CLIENT_SECRETS", "client_secrets.json"),
    )
    materialize_credential_file(
        "YOUTUBE_TOKEN_JSON_B64",
        DATA_DIR / os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json"),
    )


@dataclass(frozen=True)
class Settings:
    grok_api_key: str
    video_api_key: str
    base_url: str = "https://gen.pollinations.ai"
    pollinations_connect_timeout: int = 30
    pollinations_read_timeout: int = 600
    video_scene_attempts: int = 3
    video_scene_retry_backoff_seconds: int = 20
    ltx_fallback_to_grok_key: bool = True
    language: str = "en"
    duration: int = 20
    text_model: str = "grok-large"
    video_model: str = "ltx-2"
    google_tts_service_account: Path = DATA_DIR / "google_tts_service_account.json"
    google_tts_voice: str = "en-US-Chirp3-HD-Achernar"
    google_tts_speaking_rate: float = 1.05
    youtube_client_secrets: Path = DATA_DIR / "client_secrets.json"
    youtube_token: Path = DATA_DIR / "youtube_token.json"
    youtube_privacy: str = "private"
    social_tts_voice: str = "vi-VN-Standard-A"
    social_tts_speaking_rate: float = 1.05
    publish_facebook: bool = False
    facebook_graph_version: str = "v25.0"
    facebook_page_id: str = ""
    facebook_page_access_token: str = ""
    publish_tiktok: bool = False
    tiktok_access_token: str = ""
    tiktok_privacy_level: str = "SELF_ONLY"
    tiktok_disable_duet: bool = False
    tiktok_disable_comment: bool = False
    tiktok_disable_stitch: bool = False

    @classmethod
    def from_env(cls, duration_override: int | None = None) -> "Settings":
        grok_api_key = os.getenv("POLLINATIONS_GROK_API_KEY", "").strip()
        video_api_key = os.getenv("POLLINATIONS_VIDEO_API_KEY", "").strip()
        if not grok_api_key or not video_api_key:
            raise BotError("Thiếu POLLINATIONS_GROK_API_KEY hoặc POLLINATIONS_VIDEO_API_KEY.")
        duration = duration_override or int(os.getenv("SHORT_DURATION_SECONDS", "20"))
        if not 10 <= duration <= 20:
            raise BotError("SHORT_DURATION_SECONDS phải nằm trong 10–20 giây.")
        return cls(
            grok_api_key=grok_api_key,
            video_api_key=video_api_key,
            base_url=os.getenv("POLLINATIONS_BASE_URL", "https://gen.pollinations.ai").rstrip("/"),
            pollinations_connect_timeout=int(os.getenv("POLLINATIONS_CONNECT_TIMEOUT_SECONDS", "30")),
            pollinations_read_timeout=int(os.getenv("POLLINATIONS_READ_TIMEOUT_SECONDS", "600")),
            video_scene_attempts=max(1, int(os.getenv("LTX_SCENE_ATTEMPTS", "3"))),
            video_scene_retry_backoff_seconds=max(0, int(os.getenv("LTX_SCENE_RETRY_BACKOFF_SECONDS", "20"))),
            ltx_fallback_to_grok_key=env_bool("LTX_FALLBACK_TO_GROK_KEY", True),
            language=os.getenv("SHORT_LANGUAGE", "en"),
            duration=duration,
            google_tts_service_account=DATA_DIR / os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_FILE", "google_tts_service_account.json"),
            google_tts_voice=os.getenv("GOOGLE_TTS_VOICE", "en-US-Chirp3-HD-Achernar"),
            google_tts_speaking_rate=float(os.getenv("GOOGLE_TTS_SPEAKING_RATE", "1.05")),
            youtube_client_secrets=DATA_DIR / os.getenv("YOUTUBE_CLIENT_SECRETS", "client_secrets.json"),
            youtube_token=DATA_DIR / os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json"),
            youtube_privacy=os.getenv("YOUTUBE_PRIVACY_STATUS", "private"),
            social_tts_voice=os.getenv("SOCIAL_TTS_VOICE", "vi-VN-Standard-A"),
            social_tts_speaking_rate=float(os.getenv("SOCIAL_TTS_SPEAKING_RATE", os.getenv("GOOGLE_TTS_SPEAKING_RATE", "1.05"))),
            publish_facebook=env_bool("PUBLISH_FACEBOOK", False),
            facebook_graph_version=os.getenv("FACEBOOK_GRAPH_VERSION", "v25.0"),
            facebook_page_id=os.getenv("FACEBOOK_PAGE_ID", "").strip(),
            facebook_page_access_token=os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip(),
            publish_tiktok=env_bool("PUBLISH_TIKTOK", False),
            tiktok_access_token=os.getenv("TIKTOK_ACCESS_TOKEN", "").strip(),
            tiktok_privacy_level=os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY").strip() or "SELF_ONLY",
            tiktok_disable_duet=env_bool("TIKTOK_DISABLE_DUET", False),
            tiktok_disable_comment=env_bool("TIKTOK_DISABLE_COMMENT", False),
            tiktok_disable_stitch=env_bool("TIKTOK_DISABLE_STITCH", False),
        )


@dataclass
class Scene:
    duration: float
    visual_prompt: str
    on_screen_text: str = ""


@dataclass
class ShortPlan:
    topic: str
    angle: str
    title: str
    description: str
    tags: list[str]
    hook: str
    narration: str
    closing_line: str
    scenes: list[Scene]
    fact_note: str
    source_hints: list[str]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ShortPlan":
        required = {"topic", "angle", "title", "description", "tags", "narration", "scenes", "fact_note", "source_hints"}
        missing = required - value.keys()
        if missing:
            raise BotError(f"Grok trả kế hoạch thiếu trường: {', '.join(sorted(missing))}")
        scenes = [Scene(**scene) for scene in value["scenes"]]
        if not scenes or any(not scene.visual_prompt or scene.duration <= 0 for scene in scenes):
            raise BotError("Kế hoạch có cảnh không hợp lệ.")
        narration = str(value["narration"])
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", narration) if part.strip()]
        return cls(
            topic=str(value["topic"]), angle=str(value["angle"]), title=str(value["title"])[:100],
            description=str(value["description"])[:5000], tags=[str(x).lstrip("#") for x in value["tags"]][:15],
            hook=str(value.get("hook") or sentences[0]), narration=narration,
            closing_line=str(value.get("closing_line") or sentences[-1]), scenes=scenes,
            fact_note=str(value["fact_note"]), source_hints=[str(x) for x in value["source_hints"]][:4],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SocialPlan:
    title: str
    description: str
    tags: list[str]
    narration: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SocialPlan":
        required = {"title", "description", "tags", "narration"}
        missing = required - value.keys()
        if missing:
            raise BotError(f"Grok trả kế hoạch social thiếu trường: {', '.join(sorted(missing))}")
        return cls(
            title=str(value["title"])[:100],
            description=str(value["description"])[:2200],
            tags=[str(tag).lstrip("#") for tag in value["tags"]][:15],
            narration=str(value["narration"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalized_words(text: str) -> set[str]:
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return {word for word in re.findall(r"[a-z0-9]+", value) if len(word) > 2}


def similarity(a: str, b: str) -> float:
    left, right = normalized_words(a), normalized_words(b)
    return len(left & right) / len(left | right) if left and right else 0.0


def narration_word_bounds(duration: int) -> tuple[int, int]:
    return max(24, round(duration * 1.5)), duration * 4 + 4


class Archive:
    def __init__(self, path: Path = DATA_DIR / "shorts.db") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS shorts (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, topic TEXT NOT NULL,
            angle TEXT NOT NULL, title TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, youtube_id TEXT, output_path TEXT
        )""")
        self.conn.commit()

    def recent_context(self, limit: int = 40) -> list[dict[str, str]]:
        rows = self.conn.execute(
            "SELECT topic, angle, title, status FROM shorts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def duplicate_of(self, plan: ShortPlan, threshold: float = 0.52) -> sqlite3.Row | None:
        candidate = f"{plan.topic} {plan.angle} {plan.title}"
        exact = hashlib.sha256(candidate.lower().encode()).hexdigest()
        row = self.conn.execute("SELECT * FROM shorts WHERE fingerprint = ?", (exact,)).fetchone()
        if row:
            return row
        for previous in self.conn.execute("SELECT * FROM shorts ORDER BY id DESC LIMIT 150"):
            previous_text = f"{previous['topic']} {previous['angle']} {previous['title']}"
            if similarity(candidate, previous_text) >= threshold:
                return previous
        return None

    def reserve(self, plan: ShortPlan, output_path: Path) -> int:
        fingerprint = hashlib.sha256(f"{plan.topic} {plan.angle} {plan.title}".lower().encode()).hexdigest()
        cursor = self.conn.execute(
            "INSERT INTO shorts (created_at, topic, angle, title, fingerprint, status, output_path) VALUES (?, ?, ?, ?, ?, 'rendering', ?)",
            (datetime.now(UTC).isoformat(), plan.topic, plan.angle, plan.title, fingerprint, str(output_path)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def mark(self, record_id: int, status: str, youtube_id: str | None = None) -> None:
        self.conn.execute("UPDATE shorts SET status = ?, youtube_id = COALESCE(?, youtube_id) WHERE id = ?", (status, youtube_id, record_id))
        self.conn.commit()

    def jobs_created_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM shorts WHERE substr(created_at, 1, 10) = ?", (today,)
        ).fetchone()
        return int(row["count"])


class Pollinations:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.grok_headers = {"Authorization": f"Bearer {settings.grok_api_key}"}
        self.video_headers = {"Authorization": f"Bearer {settings.video_api_key}"}

    def _request(self, method: str, url: str, headers: dict[str, str], **kwargs: Any) -> requests.Response:
        LOG.debug("Calling Pollinations %s %s", method, url.split("?", maxsplit=1)[0])
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=(self.s.pollinations_connect_timeout, self.s.pollinations_read_timeout),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise PollinationsTransientError(f"Pollinations request failed: {exc}") from exc
        if not response.ok:
            detail = response.text[:800]
            if looks_like_quota_error(response.status_code, detail):
                raise PollinationsQuotaError(f"Pollinations {response.status_code}: {detail}", response.status_code)
            if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
                raise PollinationsTransientError(f"Pollinations {response.status_code}: {detail}", response.status_code)
            raise BotError(f"Pollinations {response.status_code}: {detail}")
        return response

    def chat(self, prompt: str, temperature: float = 0.55) -> str:
        LOG.info("Generating the English content plan with Grok Large…")
        response = self._request("POST", f"{self.s.base_url}/v1/chat/completions", headers=self.grok_headers, json={
            "model": self.s.text_model,
            "messages": [{"role": "system", "content": "You return only valid JSON when asked."}, {"role": "user", "content": prompt}],
            "temperature": temperature,
        }).json()
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BotError(f"Phản hồi Grok không đúng định dạng: {response}") from exc

    def video(self, prompt: str, duration: float, destination: Path, seed: int) -> None:
        params = {"model": self.s.video_model, "duration": round(duration, 2), "aspectRatio": "9:16", "width": 720, "height": 1280, "seed": seed, "safe": "true"}
        partial = destination.with_name(f"{destination.name}.part")
        key_candidates = [("video key", self.video_headers)]
        if self.s.ltx_fallback_to_grok_key and self.s.grok_api_key != self.s.video_api_key:
            key_candidates.append(("grok key fallback", self.grok_headers))
        last_error: Exception | None = None
        for attempt in range(1, self.s.video_scene_attempts + 1):
            if partial.exists():
                partial.unlink()
            for key_label, headers in key_candidates:
                LOG.info(
                    "Requesting LTX-2 scene (%ss): %s with %s (attempt %d/%d)",
                    duration,
                    destination.name,
                    key_label,
                    attempt,
                    self.s.video_scene_attempts,
                )
                try:
                    response = self._request(
                        "GET",
                        f"{self.s.base_url}/video/{quote(prompt, safe='')}",
                        headers=headers,
                        params=params,
                        stream=True,
                    )
                    bytes_written = 0
                    with partial.open("wb") as handle:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                handle.write(chunk)
                                bytes_written += len(chunk)
                    if bytes_written < 1024:
                        raise PollinationsTransientError(f"LTX-2 returned only {bytes_written} bytes for {destination.name}")
                    partial.replace(destination)
                    LOG.info("Downloaded %s (%.1f MB)", destination.name, bytes_written / (1024 * 1024))
                    return
                except PollinationsQuotaError as exc:
                    last_error = exc
                    LOG.warning("LTX-2 %s is out of quota or rate-limited: %s", key_label, exc)
                    if partial.exists():
                        partial.unlink()
                    continue
                except PollinationsTransientError as exc:
                    last_error = exc
                    LOG.warning("LTX-2 scene failed: %s", exc)
                    break
                except requests.RequestException as exc:
                    last_error = exc
                    LOG.warning("LTX-2 scene stream failed: %s", exc)
                    break
                finally:
                    if partial.exists():
                        partial.unlink()
            if attempt < self.s.video_scene_attempts:
                sleep_for = self.s.video_scene_retry_backoff_seconds * attempt
                LOG.info("Retrying %s in %ss…", destination.name, sleep_for)
                time.sleep(sleep_for)
        raise BotError(f"LTX-2 failed after {self.s.video_scene_attempts} attempts for {destination.name}: {last_error}")


class GoogleChirpTTS:
    """Google Cloud Chirp 3 HD narration; independent from the Pollinations key."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    @staticmethod
    def language_code_for_voice(voice: str) -> str:
        parts = voice.split("-")
        return "-".join(parts[:2]) if len(parts) >= 2 else voice

    def speech(self, text: str, destination: Path, voice: str | None = None, speaking_rate: float | None = None) -> None:
        if not self.s.google_tts_service_account.exists():
            raise BotError(
                "Thiếu service-account JSON cho Google TTS: "
                f"{self.s.google_tts_service_account}. Xem README để tạo file này."
            )
        try:
            from google.cloud import texttospeech
        except ImportError as exc:
            raise BotError("Thiếu google-cloud-texttospeech. Chạy: pip install -r requirements.txt") from exc
        try:
            selected_voice = voice or self.s.google_tts_voice
            client = texttospeech.TextToSpeechClient.from_service_account_file(
                str(self.s.google_tts_service_account)
            )
            response = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(
                    language_code=self.language_code_for_voice(selected_voice),
                    name=selected_voice,
                ),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.MP3,
                    speaking_rate=speaking_rate or self.s.google_tts_speaking_rate,
                ),
            )
        except Exception as exc:
            raise BotError(f"Google Chirp 3 HD không tạo được giọng đọc: {exc}") from exc
        destination.write_bytes(response.audio_content)


PLAN_SCHEMA = '''{
  "topic":"short English topic", "angle":"specific surprising angle", "title":"<=100 chars",
  "description":"English description with #Shorts", "tags":["shorts","history"],
  "hook":"first spoken sentence, <=12 words", "narration":"English narration that begins with hook and ends with closing_line",
  "closing_line":"last spoken sentence, <=14 words",
  "scenes":[{"duration":4,"visual_prompt":"English cinematic prompt, no text or logos","on_screen_text":"<=5 English words"}],
  "fact_note":"what uncertainty was avoided", "source_hints":["institution or primary-source lead"]
}'''

RESEARCH_SCHEMA = '''{
  "central_claim":"one defensible claim", "evidence_points":["fact 1","fact 2","fact 3"],
  "uncertainty":"what must be qualified or omitted", "fresh_angle":"a non-repetitive narrative angle",
  "source_leads":["credible primary institution, archive, museum, or research body"],
  "avoid":["specific overclaim or cliché to avoid"]
}'''


def extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise BotError(f"Grok không trả JSON: {text[:400]}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise BotError(f"Grok trả JSON lỗi: {exc}") from exc


def research_brief(client: Pollinations, theme: str) -> dict[str, Any]:
    prompt = f'''Act as a meticulous research editor for an English science/history YouTube Short.
Theme: {theme}
Use your reasoning internally before responding. Return only JSON using this schema:
{RESEARCH_SCHEMA}
Rules: Choose one topic that can be explained with established evidence. Do not invent sources, data, fossil finds, dates, quotations, or expert opinions. A source_lead is only a lead for later verification, never a claim that you accessed it. Prefer a surprising, specific angle over a broad textbook summary.'''
    LOG.info("Research pass: selecting a defensible, novel story angle…")
    return extract_json(client.chat(prompt, temperature=0.35))


def plan_short(client: Pollinations, archive: Archive, theme: str, duration: int) -> ShortPlan:
    past = archive.recent_context()
    brief = research_brief(client, theme)
    prompt = f'''Act as a senior documentary writer. Create ONE highly watchable {duration}-second English-language YouTube Short plan from the editorial brief below.
Theme: {theme}
Audience: curious global English-speaking viewers. Topics may cover discovery, history, geography, science, or technology.
Use a sharp curiosity hook in the first 1.5 seconds, a clear escalation or reversal in the middle, and a concise closing line that makes the viewer think. The narration must start verbatim with hook and end verbatim with closing_line.
Facts: only use the supplied evidence points. Preserve the uncertainty exactly when relevant. Never turn a source lead into a citation or claim it was consulted.
Visuals: artistic documentary, painterly animation or restrained historical animation; intentional slight movement discontinuity is acceptable; vertical 9:16; no text, logos, watermarks, or readable signs inside video.
Split scenes into 4 or 5 scenes whose total duration is exactly {duration}; each scene must be 3–6 seconds. For a {duration}-second Short, use roughly {max(30, round(duration * 1.75))}–{duration * 3 + 15} spoken English words.
Every string in the returned JSON must be English, including topic, title, description, tags, narration, on_screen_text, fact_note, and source_hints.
The existing archive below must not be repeated or merely reframed. Return raw JSON only using exactly this schema:
{PLAN_SCHEMA}
Editorial brief: {json.dumps(brief, ensure_ascii=False)}
Existing archive: {json.dumps(past, ensure_ascii=False)}'''
    LOG.info("Writing pass: turning the research brief into a short-form story…")
    draft = ShortPlan.from_dict(extract_json(client.chat(prompt, temperature=0.65)))
    review_prompt = f'''Act as the final fact and retention editor. Think deeply but return only JSON.
Improve the draft below into a stronger {duration}-second English YouTube Short. Return exactly:
{{"quality_check":{{"hook_score":1,"clarity_score":1,"factual_risk":"short note","changes":["short note"]}},"plan":{PLAN_SCHEMA}}}
The plan must retain only claims supported by the editorial brief. Reject hype, vague filler, fake certainty, generic endings, and repetition. Make the hook immediately intriguing, the middle concrete, and the closing line memorable. The narration must begin with hook and end with closing_line. Keep scene durations totaling exactly {duration}.
Editorial brief: {json.dumps(brief, ensure_ascii=False)}
Draft: {json.dumps(draft.to_dict(), ensure_ascii=False)}'''
    LOG.info("Quality pass: checking factual precision, hook, pacing, and ending…")
    reviewed = extract_json(client.chat(review_prompt, temperature=0.25))
    if not isinstance(reviewed.get("plan"), dict):
        raise BotError("Grok quality pass thiếu trường plan.")
    plan = ShortPlan.from_dict(reviewed["plan"])
    quality = reviewed.get("quality_check", {})
    LOG.info("Quality pass complete — hook %s/10, clarity %s/10.", quality.get("hook_score", "?"), quality.get("clarity_score", "?"))
    scene_total = sum(scene.duration for scene in plan.scenes)
    words = len(re.findall(r"\b\w+\b", plan.narration, flags=re.UNICODE))
    if abs(scene_total - duration) > 0.1:
        raise BotError(f"Grok chia cảnh {scene_total:g}s, không đúng mục tiêu {duration}s.")
    minimum_words, maximum_words = narration_word_bounds(duration)
    if not minimum_words <= words <= maximum_words:
        raise BotError(f"Kịch bản có {words} từ, ngoài khoảng phù hợp cho Short {duration}s.")
    if not plan.narration.lower().startswith(plan.hook.lower()) or not plan.narration.lower().endswith(plan.closing_line.lower()):
        raise BotError("Kịch bản phải bắt đầu bằng hook và kết thúc bằng closing_line.")
    LOG.info("Plan ready: %r (%d scenes, %d words)", plan.title, len(plan.scenes), words)
    return plan


def plan_social_vietnamese(client: Pollinations, plan: ShortPlan, duration: int) -> SocialPlan:
    prompt = f'''Translate and adapt this English YouTube Short plan into Vietnamese for Facebook and TikTok.
Return raw JSON only with this schema:
{{"title":"Vietnamese title, <=100 characters","description":"Vietnamese caption with 3-6 relevant hashtags","tags":["short Vietnamese or English hashtag without #"],"narration":"Vietnamese voice-over"}}
Rules:
- Keep every factual claim equivalent to the English plan; do not add dates, names, statistics, sources, or certainty.
- Make the Vietnamese narration natural, concise, and suitable for a {duration}-second short video.
- The narration should be roughly {max(28, round(duration * 1.6))}-{duration * 4 + 8} Vietnamese words.
- The caption should disclose that the video is AI-assisted when appropriate.
English plan: {json.dumps(plan.to_dict(), ensure_ascii=False)}'''
    LOG.info("Creating Vietnamese social caption and narration…")
    social = SocialPlan.from_dict(extract_json(client.chat(prompt, temperature=0.35)))
    words = len(re.findall(r"\b\w+\b", social.narration, flags=re.UNICODE))
    if words < max(20, round(duration * 1.2)):
        raise BotError(f"Kịch bản tiếng Việt có {words} từ, quá ngắn cho Short {duration}s.")
    LOG.info("Vietnamese social plan ready: %r (%d words)", social.title, words)
    return social


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise BotError("Không tìm thấy trong PATH: " + ", ".join(missing))


def run(command: list[str]) -> None:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise BotError(f"FFmpeg lỗi: {' '.join(command)}\n{result.stderr[-1500:]}")


def media_duration(path: Path) -> float:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], text=True, capture_output=True)
    if result.returncode:
        raise BotError(f"Không đọc được thời lượng {path.name}")
    return float(result.stdout.strip())


def render(plan: ShortPlan, client: Pollinations, tts: GoogleChirpTTS, output_dir: Path, target_duration: int) -> Path:
    require_tools()
    LOG.info("Rendering %d scenes into %s", len(plan.scenes), output_dir)
    clips: list[Path] = []
    for index, scene in enumerate(plan.scenes, start=1):
        clip = output_dir / f"scene_{index}.mp4"
        client.video(scene.visual_prompt, scene.duration, clip, seed=int(time.time()) + index)
        if clip.stat().st_size < 1024:
            raise BotError(f"Cảnh {index} không phải video hợp lệ.")
        clip_seconds = media_duration(clip)
        LOG.info("Scene %d ready: %.2fs, %.1f MB", index, clip_seconds, clip.stat().st_size / (1024 * 1024))
        clips.append(clip)

    concat = output_dir / "clips.txt"
    concat.write_text("".join(f"file '{clip.resolve().as_posix()}'\n" for clip in clips), encoding="utf-8")
    visuals = output_dir / "visuals.mp4"
    # Re-encode each output so APIs returning different codecs/FPS still concatenate correctly.
    video_filter = (
        "scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,fps=30,"
        f"tpad=stop_mode=clone:stop_duration={target_duration},"
        f"trim=duration={target_duration},setpts=PTS-STARTPTS,format=yuv420p"
    )
    LOG.info("Concatenating and normalizing the vertical video…")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-an", "-vf", video_filter, "-c:v", "libx264", "-preset", "medium", str(visuals)])

    narration = output_dir / "narration.mp3"
    LOG.info("Generating English narration with Google Chirp 3 HD…")
    tts.speech(plan.narration, narration)
    narration_seconds = media_duration(narration)
    if narration_seconds > target_duration + 0.4:
        # Gentle tempo increase only when the voice runs long; prevents visual/audio drift.
        tempo = min(1.25, narration_seconds / target_duration)
        adjusted = output_dir / "narration_fit.mp3"
        run(["ffmpeg", "-y", "-i", str(narration), "-filter:a", f"atempo={tempo:.4f}", str(adjusted)])
        narration = adjusted

    final_video = output_dir / "short.mp4"
    LOG.info("Muxing narration and final video…")
    run(["ffmpeg", "-y", "-i", str(visuals), "-i", str(narration), "-filter_complex", f"[1:a]apad=pad_dur={target_duration}[a]", "-map", "0:v:0", "-map", "[a]", "-t", str(target_duration), "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(final_video)])
    return final_video


def render_social_video(social: SocialPlan, tts: GoogleChirpTTS, output_dir: Path, settings: Settings, target_duration: int) -> Path:
    visuals = output_dir / "visuals.mp4"
    if not visuals.is_file():
        raise BotError(f"Không tìm thấy visuals.mp4 để tạo bản social: {visuals}")
    narration = output_dir / "narration_vi.mp3"
    LOG.info("Generating Vietnamese narration for Facebook/TikTok…")
    tts.speech(social.narration, narration, voice=settings.social_tts_voice, speaking_rate=settings.social_tts_speaking_rate)
    narration_seconds = media_duration(narration)
    if narration_seconds > target_duration + 0.4:
        tempo = min(1.25, narration_seconds / target_duration)
        adjusted = output_dir / "narration_vi_fit.mp3"
        run(["ffmpeg", "-y", "-i", str(narration), "-filter:a", f"atempo={tempo:.4f}", str(adjusted)])
        narration = adjusted
    social_video = output_dir / "short_vi.mp4"
    LOG.info("Muxing Vietnamese social video…")
    run(["ffmpeg", "-y", "-i", str(visuals), "-i", str(narration), "-filter_complex", f"[1:a]apad=pad_dur={target_duration}[a]", "-map", "0:v:0", "-map", "[a]", "-t", str(target_duration), "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(social_video)])
    return social_video


def upload_to_youtube(video: Path, plan: ShortPlan, settings: Settings, privacy: str) -> str:
    if not settings.youtube_client_secrets.exists():
        raise BotError(f"Thiếu OAuth client secrets: {settings.youtube_client_secrets}")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    scope = ["https://www.googleapis.com/auth/youtube.upload"]
    credentials: Credentials | None = None
    if settings.youtube_token.exists():
        LOG.info("Loading the saved YouTube authorization token…")
        credentials = Credentials.from_authorized_user_file(str(settings.youtube_token), scope)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            LOG.info("Refreshing the YouTube authorization token…")
            credentials.refresh(Request())
        else:
            LOG.info("Opening the browser for YouTube authorization…")
            credentials = InstalledAppFlow.from_client_secrets_file(str(settings.youtube_client_secrets), scope).run_local_server(port=0)
        settings.youtube_token.write_text(credentials.to_json(), encoding="utf-8")
    service = build("youtube", "v3", credentials=credentials)
    body = {"snippet": {"title": plan.title, "description": plan.description, "tags": plan.tags, "categoryId": "27"}, "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    LOG.info("Uploading %s to YouTube as %s…", video.name, privacy)
    request = service.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True),
    )
    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                LOG.info("YouTube upload progress: %.0f%%", status.progress() * 100)
    except HttpError as exc:
        detail = str(exc)
        if exc.resp.status == 403 and "accessNotConfigured" in detail:
            raise BotError(
                "YouTube Data API v3 chưa được bật cho Google Cloud project của OAuth client. "
                "Bật API trong APIs & Services → Library, chờ vài phút, rồi upload lại MP4 đã render."
            ) from exc
        raise BotError(f"YouTube upload thất bại: {detail}") from exc
    LOG.info("YouTube upload completed.")
    return str(response["id"])


def response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise BotError(f"API trả phản hồi không phải JSON: {response.text[:500]}") from exc
    if not isinstance(data, dict):
        raise BotError(f"API trả JSON không đúng định dạng: {data}")
    return data


def upload_to_facebook_page(video: Path, social: SocialPlan, settings: Settings) -> str:
    if not settings.facebook_page_id or not settings.facebook_page_access_token:
        raise BotError("Thiếu FACEBOOK_PAGE_ID hoặc FACEBOOK_PAGE_ACCESS_TOKEN.")
    url = f"https://graph-video.facebook.com/{settings.facebook_graph_version}/{settings.facebook_page_id}/videos"
    LOG.info("Uploading %s to Facebook Page %s…", video.name, settings.facebook_page_id)
    with video.open("rb") as handle:
        response = requests.post(
            url,
            data={
                "access_token": settings.facebook_page_access_token,
                "title": social.title,
                "description": social.description,
                "published": "true",
            },
            files={"source": (video.name, handle, "video/mp4")},
            timeout=(30, 900),
        )
    data = response_json(response)
    if not response.ok or "error" in data:
        raise BotError(f"Facebook upload thất bại: {data}")
    video_id = str(data.get("id") or data.get("video_id") or "")
    if not video_id:
        raise BotError(f"Facebook upload không trả video id: {data}")
    LOG.info("Facebook upload completed: %s", video_id)
    return video_id


def tiktok_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}


def tiktok_check_error(response: requests.Response) -> dict[str, Any]:
    data = response_json(response)
    error = data.get("error")
    if not response.ok or (isinstance(error, dict) and error.get("code") not in (None, "ok")):
        raise BotError(f"TikTok API thất bại: {data}")
    return data


def tiktok_creator_privacy(settings: Settings) -> str:
    response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers=tiktok_headers(settings.tiktok_access_token),
        timeout=(30, 120),
    )
    data = tiktok_check_error(response)
    options = data.get("data", {}).get("privacy_level_options") or []
    if settings.tiktok_privacy_level in options:
        return settings.tiktok_privacy_level
    if "SELF_ONLY" in options:
        LOG.warning("TikTok privacy %s is unavailable; falling back to SELF_ONLY.", settings.tiktok_privacy_level)
        return "SELF_ONLY"
    if options:
        LOG.warning("TikTok privacy %s is unavailable; falling back to %s.", settings.tiktok_privacy_level, options[0])
        return str(options[0])
    return settings.tiktok_privacy_level


def tiktok_upload_chunks(upload_url: str, video: Path, chunk_size: int, total_chunks: int) -> None:
    total_size = video.stat().st_size
    with video.open("rb") as handle:
        for chunk_index in range(total_chunks):
            start = chunk_index * chunk_size
            if chunk_index == total_chunks - 1:
                data = handle.read()
            else:
                data = handle.read(chunk_size)
            end = start + len(data) - 1
            response = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{total_size}",
                },
                data=data,
                timeout=(30, 900),
            )
            if response.status_code not in {200, 201, 206}:
                raise BotError(f"TikTok upload chunk {chunk_index + 1}/{total_chunks} thất bại: {response.status_code} {response.text[:500]}")


def upload_to_tiktok(video: Path, social: SocialPlan, settings: Settings) -> str:
    if not settings.tiktok_access_token:
        raise BotError("Thiếu TIKTOK_ACCESS_TOKEN.")
    total_size = video.stat().st_size
    if total_size <= 0:
        raise BotError(f"Video TikTok rỗng: {video}")
    chunk_size = total_size if total_size < 5 * 1024 * 1024 else 10 * 1024 * 1024
    total_chunks = max(1, total_size // chunk_size)
    privacy_level = tiktok_creator_privacy(settings)
    body = {
        "post_info": {
            "title": social.description[:2200],
            "privacy_level": privacy_level,
            "disable_duet": settings.tiktok_disable_duet,
            "disable_comment": settings.tiktok_disable_comment,
            "disable_stitch": settings.tiktok_disable_stitch,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "is_aigc": True,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": total_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }
    LOG.info("Initializing TikTok direct post upload (%d bytes, %d chunk(s))…", total_size, total_chunks)
    response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers=tiktok_headers(settings.tiktok_access_token),
        json=body,
        timeout=(30, 120),
    )
    data = tiktok_check_error(response)
    upload_url = data.get("data", {}).get("upload_url")
    publish_id = str(data.get("data", {}).get("publish_id") or "")
    if not upload_url:
        raise BotError(f"TikTok không trả upload_url: {data}")
    tiktok_upload_chunks(str(upload_url), video, chunk_size, total_chunks)
    LOG.info("TikTok upload completed: %s", publish_id)
    return publish_id


def publish_social_video(video: Path, social: SocialPlan, settings: Settings) -> dict[str, str]:
    results: dict[str, str] = {}
    if settings.publish_facebook:
        try:
            results["facebook"] = upload_to_facebook_page(video, social, settings)
        except Exception as exc:
            LOG.error("Facebook publish failed: %s", exc)
    if settings.publish_tiktok:
        try:
            results["tiktok"] = upload_to_tiktok(video, social, settings)
        except Exception as exc:
            LOG.error("TikTok publish failed: %s", exc)
    return results


def social_publish_enabled(settings: Settings) -> bool:
    return settings.publish_facebook or settings.publish_tiktok


def prepare_social_video(plan: ShortPlan, client: Pollinations, tts: GoogleChirpTTS, output_dir: Path, settings: Settings) -> tuple[Path, SocialPlan]:
    social_file = output_dir / "social_vi.json"
    if social_file.is_file():
        social = SocialPlan.from_dict(json.loads(social_file.read_text(encoding="utf-8")))
    else:
        social = plan_social_vietnamese(client, plan, settings.duration)
        social_file.write_text(json.dumps(social.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    social_video = output_dir / "short_vi.mp4"
    if not social_video.is_file():
        social_video = render_social_video(social, tts, output_dir, settings, settings.duration)
    return social_video, social


def slug(value: str) -> str:
    simple = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", simple).strip("-")[:42] or "short"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", default="little-known discoveries in history, science, technology, or geography")
    parser.add_argument("--duration", type=int, choices=range(10, 21), metavar="10..20")
    parser.add_argument("--publish", action="store_true", help="Tự upload sau khi render")
    parser.add_argument("--privacy-status", choices=("private", "unlisted", "public"))
    parser.add_argument("--dry-run", action="store_true", help="Chỉ tạo và in kế hoạch")
    parser.add_argument("--upload-file", type=Path, help="Upload lại MP4 đã render, không tạo nội dung/video mới")
    parser.add_argument("--scheduled", action="store_true", help="Bật giới hạn an toàn: tối đa 3 video mới mỗi ngày UTC")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    args = parser.parse_args()
    configure_logging(args.log_level)
    materialize_railway_credentials()
    settings = Settings.from_env(args.duration)
    archive, client, tts = Archive(), Pollinations(settings), GoogleChirpTTS(settings)
    if args.upload_file:
        if not args.publish:
            parser.error("--upload-file requires --publish")
        video = args.upload_file.resolve()
        plan_file = video.parent / "plan.json"
        if not video.is_file() or not plan_file.is_file():
            raise BotError("--upload-file phải trỏ đến short.mp4 có plan.json cùng thư mục.")
        plan = ShortPlan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
        youtube_id = upload_to_youtube(video, plan, settings, args.privacy_status or settings.youtube_privacy)
        print(f"Đã upload: https://youtube.com/watch?v={youtube_id}")
        if social_publish_enabled(settings):
            social_video, social = prepare_social_video(plan, client, tts, video.parent, settings)
            social_results = publish_social_video(social_video, social, settings)
            if social_results:
                print(f"Đã publish social: {social_results}")
        return 0
    if args.scheduled and archive.jobs_created_today() >= 3:
        LOG.warning("Daily limit reached: 3 video jobs have already been created today (UTC). Exiting.")
        return 0
    for attempt in range(1, 5):
        plan = plan_short(client, archive, args.theme, settings.duration)
        duplicate = archive.duplicate_of(plan)
        if not duplicate:
            break
        print(f"Ý tưởng trùng ({duplicate['title']!r}); yêu cầu Grok tạo góc khác…")
    else:
        raise BotError("Không tìm được ý tưởng đủ mới sau 4 lần.")

    if args.dry_run:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_dir = DATA_DIR / "generated" / f"{datetime.now():%Y%m%d-%H%M%S}-{slug(plan.topic)}"
    output_dir.mkdir(parents=True)
    (output_dir / "plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    record_id = archive.reserve(plan, output_dir)
    rendered = False
    try:
        video = render(plan, client, tts, output_dir, settings.duration)
        rendered = True
        archive.mark(record_id, "rendered")
        print(f"Đã render: {video}")
        if args.publish:
            youtube_id = upload_to_youtube(video, plan, settings, args.privacy_status or settings.youtube_privacy)
            archive.mark(record_id, "published", youtube_id)
            print(f"Đã upload: https://youtube.com/watch?v={youtube_id}")
            if social_publish_enabled(settings):
                social_video, social = prepare_social_video(plan, client, tts, output_dir, settings)
                social_results = publish_social_video(social_video, social, settings)
                if social_results:
                    print(f"Đã publish social: {social_results}")
    except Exception:
        archive.mark(record_id, "upload_failed" if rendered else "failed")
        raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BotError as error:
        print(f"LỖI: {error}", file=sys.stderr)
        raise SystemExit(1)
