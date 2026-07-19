"""Generate factual, artistic English YouTube videos with OpenAI Images and FFmpeg.

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
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, quote_plus
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
_configured_data_dir = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT)
DATA_DIR = _configured_data_dir if _configured_data_dir.is_absolute() else ROOT / _configured_data_dir
LOG = logging.getLogger("shorts_bot")
MIN_SHORT_DURATION_SECONDS = 45
MAX_SHORT_DURATION_SECONDS = 60
SHORT_FORM_INTERMEDIATE_WIDTH = 1728
SHORT_FORM_SCENE_RENDER_TIMEOUT_SECONDS = 120
SHORT_FORM_FINAL_RENDER_TIMEOUT_SECONDS = 600
LONG_FORM_INTERMEDIATE_WIDTH = 2304
LONG_FORM_SCENE_RENDER_TIMEOUT_SECONDS = 120
LONG_FORM_FINAL_RENDER_TIMEOUT_SECONDS = 900

# Windows PowerShell sessions can still inherit cp1252. Keep CLI output
# deterministic instead of failing on non-ASCII text in paths or user themes.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


class BotError(RuntimeError):
    pass


class ImageGenerationTransientError(BotError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ImageGenerationSafetyError(BotError):
    """A prompt/image was rejected by the provider's safety system; do not retry it."""


class FacebookAPIError(BotError):
    def __init__(
        self,
        message: str,
        code: str | None = None,
        subcode: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode

    @property
    def is_expired_token(self) -> bool:
        return self.code == "190" and self.subcode == "463"


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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


def ensure_dejavu_font() -> None:
    font_dir = DATA_DIR / "fonts"
    font_file = font_dir / "DejaVuSans.ttf"
    config_file = DATA_DIR / "fonts.conf"

    if not font_file.is_file():
        LOG.info("DejaVuSans.ttf not found in %s. Downloading...", font_dir)
        font_dir.mkdir(parents=True, exist_ok=True)
        url = "https://github.com/prawnpdf/prawn/raw/master/data/fonts/DejaVuSans.ttf"
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            font_file.write_bytes(response.content)
            LOG.info("Successfully downloaded DejaVuSans.ttf.")
        except Exception as exc:
            raise BotError(f"Could not download DejaVuSans.ttf: {exc}") from exc

    if not config_file.is_file():
        LOG.info("Creating custom fonts.conf in %s...", config_file)
        config_content = f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>{font_dir.resolve().as_posix()}</dir>
  <cachedir>/tmp/fontcache</cachedir>
  <config></config>
</fontconfig>
"""
        config_file.write_text(config_content, encoding="utf-8")

    os.environ["FONTCONFIG_FILE"] = str(config_file.resolve())


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = ""
    brave_search_api_key: str = ""
    openai_text_endpoint: str = "https://api.openai.com/v1/responses"
    openai_image_endpoint: str = "https://api.openai.com/v1/images/generations"
    openai_tts_endpoint: str = "https://api.openai.com/v1/audio/speech"
    text_model: str = "gpt-5.4-mini"
    text_reasoning_effort: str = "low"
    text_long_form_reasoning_effort: str = "medium"
    text_max_output_tokens: int = 16000
    text_connect_timeout: int = 30
    text_read_timeout: int = 300
    text_attempts: int = 3
    text_retry_backoff_seconds: int = 5
    brave_image_search_endpoint: str = "https://api.search.brave.com/res/v1/images/search"
    brave_search_attempts: int = 3
    brave_search_min_interval_seconds: float = 2.0
    image_connect_timeout: int = 30
    image_read_timeout: int = 180
    image_attempts: int = 3
    image_retry_backoff_seconds: int = 10
    brave_web_images_per_short: int = 2
    brave_web_images_per_long_form: int = 5
    long_form_openai_images: int = 10
    language: str = "en"
    duration: int = 20
    long_form_min_duration_seconds: int = 240
    long_form_max_duration_seconds: int = 300
    long_form_min_scenes: int = 15
    long_form_max_scenes: int = 15
    long_form_timezone: str = "Asia/Bangkok"
    long_form_interval_days: int = 2
    scheduled_daily_limit: int = 2
    image_model: str = "gpt-image-2"
    image_quality: str = "low"
    image_vertical_size: str = "1024x1536"
    image_horizontal_size: str = "1536x1024"
    allow_image_fallback_placeholder: bool = False
    overlay_logo: Path = ROOT / "overlay-logo.png"
    overlay_logo_short_width: int = 220
    overlay_logo_long_form_width: int = 220
    overlay_logo_margin: int = 36
    overlay_logo_short_top_margin: int = 72
    overlay_logo_long_form_top_margin: int = 36
    google_tts_service_account: Path = DATA_DIR / "google_tts_service_account.json"
    google_tts_voice: str = "en-US-Chirp3-HD-Enceladus"
    google_tts_speaking_rate: float = 1.05
    youtube_client_secrets: Path = DATA_DIR / "client_secrets.json"
    youtube_token: Path = DATA_DIR / "youtube_token.json"
    youtube_privacy: str = "private"
    social_openai_tts_voice: str = "ash"
    social_openai_tts_speed: float = 1.0
    publish_facebook: bool = False
    facebook_graph_version: str = "v25.0"
    facebook_page_id: str = ""
    facebook_page_access_token: str = ""
    facebook_user_access_token: str = ""
    publish_tiktok: bool = False
    tiktok_access_token: str = ""
    tiktok_privacy_level: str = "SELF_ONLY"
    tiktok_disable_duet: bool = False
    tiktok_disable_comment: bool = False
    tiktok_disable_stitch: bool = False

    @classmethod
    def from_env(cls, duration_override: int | None = None) -> "Settings":
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        brave_long_form_images = min(10, max(0, int(os.getenv("BRAVE_WEB_IMAGES_PER_LONG_FORM", "5"))))
        overlay_logo = Path(os.getenv("OVERLAY_LOGO_FILE", "overlay-logo.png"))
        if not overlay_logo.is_absolute():
            overlay_logo = ROOT / overlay_logo
        if not openai_api_key:
            raise BotError("Thiếu OPENAI_API_KEY cho OpenAI text và GPT Image 2.")
        duration = duration_override or int(os.getenv("SHORT_DURATION_SECONDS", "60"))
        if not MIN_SHORT_DURATION_SECONDS <= duration <= MAX_SHORT_DURATION_SECONDS:
            raise BotError(
                f"SHORT_DURATION_SECONDS phải nằm trong "
                f"{MIN_SHORT_DURATION_SECONDS}–{MAX_SHORT_DURATION_SECONDS} giây."
            )
        text_reasoning_effort = os.getenv("OPENAI_TEXT_REASONING_EFFORT", "low").strip() or "low"
        text_long_form_reasoning_effort = (
            os.getenv("OPENAI_TEXT_LONG_FORM_REASONING_EFFORT", "medium").strip() or "medium"
        )
        allowed_reasoning_efforts = {"none", "low", "medium", "high", "xhigh"}
        if text_reasoning_effort not in allowed_reasoning_efforts:
            raise BotError("OPENAI_TEXT_REASONING_EFFORT phải là none, low, medium, high hoặc xhigh.")
        if text_long_form_reasoning_effort not in allowed_reasoning_efforts:
            raise BotError("OPENAI_TEXT_LONG_FORM_REASONING_EFFORT phải là none, low, medium, high hoặc xhigh.")
        return cls(
            openai_api_key=openai_api_key,
            brave_search_api_key=os.getenv("BRAVE_SEARCH_API_KEY", "").strip(),
            text_model=os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini",
            text_reasoning_effort=text_reasoning_effort,
            text_long_form_reasoning_effort=text_long_form_reasoning_effort,
            text_max_output_tokens=max(1024, int(os.getenv("OPENAI_TEXT_MAX_OUTPUT_TOKENS", "16000"))),
            text_connect_timeout=max(1, int(os.getenv("OPENAI_TEXT_CONNECT_TIMEOUT_SECONDS", "30"))),
            text_read_timeout=max(1, int(os.getenv("OPENAI_TEXT_READ_TIMEOUT_SECONDS", "300"))),
            text_attempts=max(1, int(os.getenv("OPENAI_TEXT_ATTEMPTS", "3"))),
            text_retry_backoff_seconds=max(0, int(os.getenv("OPENAI_TEXT_RETRY_BACKOFF_SECONDS", "5"))),
            brave_search_attempts=max(1, int(os.getenv("BRAVE_SEARCH_ATTEMPTS", "3"))),
            brave_search_min_interval_seconds=max(
                0.0, float(os.getenv("BRAVE_SEARCH_MIN_INTERVAL_SECONDS", "2.0"))
            ),
            image_connect_timeout=int(os.getenv("IMAGE_CONNECT_TIMEOUT_SECONDS", "30")),
            image_read_timeout=int(os.getenv("IMAGE_READ_TIMEOUT_SECONDS", "180")),
            image_attempts=max(1, int(os.getenv("OPENAI_IMAGE_ATTEMPTS", "3"))),
            image_retry_backoff_seconds=max(0, int(os.getenv("OPENAI_IMAGE_RETRY_BACKOFF_SECONDS", "10"))),
            brave_web_images_per_short=min(6, max(0, int(os.getenv("BRAVE_WEB_IMAGES_PER_SHORT", "2")))),
            brave_web_images_per_long_form=brave_long_form_images,
            language=os.getenv("SHORT_LANGUAGE", "en"),
            duration=duration,
            long_form_min_duration_seconds=max(60, int(os.getenv("LONG_FORM_MIN_DURATION_SECONDS", "240"))),
            long_form_max_duration_seconds=max(60, int(os.getenv("LONG_FORM_MAX_DURATION_SECONDS", "300"))),
            long_form_min_scenes=cls.long_form_openai_images + brave_long_form_images,
            long_form_max_scenes=cls.long_form_openai_images + brave_long_form_images,
            long_form_timezone=os.getenv("LONG_FORM_TIMEZONE", "Asia/Bangkok").strip() or "Asia/Bangkok",
            long_form_interval_days=max(1, int(os.getenv("LONG_FORM_INTERVAL_DAYS", "2"))),
            scheduled_daily_limit=max(0, int(os.getenv("SCHEDULED_DAILY_LIMIT", "2"))),
            allow_image_fallback_placeholder=env_bool("ALLOW_IMAGE_FALLBACK_PLACEHOLDER", False),
            overlay_logo=overlay_logo,
            overlay_logo_short_width=max(64, int(os.getenv("OVERLAY_LOGO_SHORT_WIDTH", "220"))),
            overlay_logo_long_form_width=max(64, int(os.getenv("OVERLAY_LOGO_LONG_FORM_WIDTH", "220"))),
            overlay_logo_margin=max(0, int(os.getenv("OVERLAY_LOGO_MARGIN", "36"))),
            overlay_logo_short_top_margin=max(0, int(os.getenv("OVERLAY_LOGO_SHORT_TOP_MARGIN", "72"))),
            overlay_logo_long_form_top_margin=max(0, int(os.getenv("OVERLAY_LOGO_LONG_FORM_TOP_MARGIN", "36"))),
            google_tts_service_account=DATA_DIR / os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_FILE", "google_tts_service_account.json"),
            google_tts_voice=os.getenv("GOOGLE_TTS_VOICE", "en-US-Chirp3-HD-Enceladus"),
            google_tts_speaking_rate=float(os.getenv("GOOGLE_TTS_SPEAKING_RATE", "1.05")),
            youtube_client_secrets=DATA_DIR / os.getenv("YOUTUBE_CLIENT_SECRETS", "client_secrets.json"),
            youtube_token=DATA_DIR / os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json"),
            youtube_privacy=os.getenv("YOUTUBE_PRIVACY_STATUS", "private"),
            social_openai_tts_voice=os.getenv("SOCIAL_OPENAI_TTS_VOICE", "ash").strip() or "ash",
            social_openai_tts_speed=min(4.0, max(0.25, float(os.getenv("SOCIAL_OPENAI_TTS_SPEED", "1.0")))),
            publish_facebook=env_bool("PUBLISH_FACEBOOK", False),
            facebook_graph_version=os.getenv("FACEBOOK_GRAPH_VERSION", "v25.0"),
            facebook_page_id=os.getenv("FACEBOOK_PAGE_ID", "").strip(),
            facebook_page_access_token=os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip(),
            facebook_user_access_token=os.getenv("FACEBOOK_USER_ACCESS_TOKEN", "").strip(),
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
    # Literal web image search phrase naming the real person/place/object.
    # Only the generation prompt must avoid real people; the search may name them.
    search_query: str = ""


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
    thumbnail_text: str = ""
    subject_stature_score: int = 0
    historical_significance_score: int = 0
    broad_learning_value_score: int = 0
    significance_reason: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ShortPlan":
        required = {"topic", "angle", "title", "description", "tags", "narration", "scenes", "fact_note", "source_hints"}
        missing = required - value.keys()
        if missing:
            raise BotError(f"Grok trả kế hoạch thiếu trường: {', '.join(sorted(missing))}")
        raw_scenes = value["scenes"]
        if not isinstance(raw_scenes, list):
            raise BotError("Kế hoạch có trường scenes không hợp lệ.")
        scenes: list[Scene] = []
        for raw_scene in raw_scenes:
            if not isinstance(raw_scene, dict):
                raise BotError("Kế hoạch có cảnh không hợp lệ.")
            try:
                duration = float(raw_scene["duration"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BotError("Kế hoạch có cảnh thiếu hoặc sai duration.") from exc
            visual_prompt = raw_scene.get("visual_prompt") or raw_scene.get("visual_prompt_alt")
            scenes.append(Scene(
                duration=duration,
                visual_prompt=str(visual_prompt or ""),
                search_query=str(raw_scene.get("search_query") or "").strip(),
            ))
        if not scenes or any(not scene.visual_prompt or scene.duration <= 0 for scene in scenes):
            raise BotError("Kế hoạch có cảnh không hợp lệ.")
        narration = str(value["narration"])
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", narration) if part.strip()]
        return cls(
            topic=str(value["topic"]), angle=str(value["angle"]), title=str(value["title"])[:100],
            description=str(value["description"])[:5000], tags=[str(x).lstrip("#") for x in value["tags"]][:2],
            hook=str(value.get("hook") or sentences[0]), narration=narration,
            closing_line=str(value.get("closing_line") or sentences[-1]), scenes=scenes,
            fact_note=str(value["fact_note"]), source_hints=[str(x) for x in value["source_hints"]][:4],
            thumbnail_text=str(value.get("thumbnail_text") or "")[:48],
            subject_stature_score=max(0, min(10, int(value.get("subject_stature_score") or 0))),
            historical_significance_score=max(0, min(10, int(value.get("historical_significance_score") or 0))),
            broad_learning_value_score=max(0, min(10, int(value.get("broad_learning_value_score") or 0))),
            significance_reason=str(value.get("significance_reason") or "")[:500],
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
            tags=[str(tag).lstrip("#") for tag in value["tags"]][:2],
            narration=str(value["narration"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CaptionCue:
    start: float
    end: float
    text: str


def normalized_words(text: str) -> set[str]:
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return {word for word in re.findall(r"[a-z0-9]+", value) if len(word) > 2}


def similarity(a: str, b: str) -> float:
    left, right = normalized_words(a), normalized_words(b)
    return len(left & right) / len(left | right) if left and right else 0.0


LONG_FORM_SUBJECT_STOPWORDS = {
    "about", "after", "against", "amid", "and", "back", "because", "before",
    "biggest", "but", "center", "clash", "crisis", "current", "economic",
    "economy", "effect", "effects", "event", "explained", "for", "from", "global",
    "how", "impact", "impacts", "into", "latest", "major", "news", "now", "over",
    "put", "risk", "risks", "story", "the", "this", "today", "video", "what",
    "when", "where", "which", "why", "with", "world",
}


def long_form_subject_words(text: str) -> set[str]:
    return normalized_words(text) - LONG_FORM_SUBJECT_STOPWORDS


def same_long_form_subject(candidate: str, previous: str) -> bool:
    """Detect the same central subject even when the consequence/angle changes."""
    left = long_form_subject_words(candidate)
    right = long_form_subject_words(previous)
    if not left or not right:
        return False
    shared = left & right
    smaller = min(len(left), len(right))
    if smaller == 1:
        token = next(iter(left if len(left) == 1 else right))
        return token in shared and len(token) >= 5
    # 0.5 instead of a stricter ratio: rephrased headlines about the same
    # strait/conflict/company share only part of their words, and repeating a
    # subject is worse for the channel than skipping a borderline candidate.
    return len(shared) >= 2 and len(shared) / smaller >= 0.5


def narration_word_bounds(duration: int) -> tuple[int, int]:
    # Broad safety range: audio-led rendering allows the final video to be
    # shorter or longer than the requested target without rejecting a good plan.
    return max(24, round(duration * 1.5)), duration * 4 + 4


def target_narration_word_bounds(duration: int) -> tuple[int, int]:
    """Preferred pacing sent to the writer; not a hard requirement."""
    if duration < MIN_SHORT_DURATION_SECONDS:
        return narration_word_bounds(duration)
    return max(24, round(duration * 3.0)), round(duration * 3.45)


class Archive:
    def __init__(self, path: Path = DATA_DIR / "shorts.db") -> None:
        self.database_url = os.getenv("DATABASE_URL")
        self.is_postgres = bool(self.database_url and self.database_url.startswith(("postgres://", "postgresql://")))

        if self.is_postgres:
            try:
                import psycopg2
                from psycopg2.extras import DictCursor
            except ImportError as exc:
                raise BotError("DATABASE_URL is set but psycopg2-binary is not installed. Please add it to requirements.") from exc
            
            # Fix schema prefix if needed
            db_url = self.database_url.replace("postgres://", "postgresql://", 1)
            self.conn = psycopg2.connect(db_url, cursor_factory=DictCursor)
            with self.conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS shorts (
                    id SERIAL PRIMARY KEY, created_at TEXT NOT NULL, topic TEXT NOT NULL,
                    angle TEXT NOT NULL, title TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, youtube_id TEXT, output_path TEXT
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS idea_queue (
                    id SERIAL PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    mode TEXT NOT NULL, idea TEXT NOT NULL, duration INTEGER,
                    publish BOOLEAN NOT NULL DEFAULT TRUE, privacy TEXT,
                    status TEXT NOT NULL DEFAULT 'pending', youtube_id TEXT,
                    output_title TEXT, error TEXT
                )""")
            self.conn.commit()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("""CREATE TABLE IF NOT EXISTS shorts (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, topic TEXT NOT NULL,
                angle TEXT NOT NULL, title TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, youtube_id TEXT, output_path TEXT
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS idea_queue (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                mode TEXT NOT NULL, idea TEXT NOT NULL, duration INTEGER,
                publish INTEGER NOT NULL DEFAULT 1, privacy TEXT,
                status TEXT NOT NULL DEFAULT 'pending', youtube_id TEXT,
                output_title TEXT, error TEXT
            )""")
            self.conn.commit()

    def get_kv(self, key: str) -> str | None:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                row = cur.fetchone()
                return row["value"] if row else None
        else:
            row = self.conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kv_store (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, value)
                )
            self.conn.commit()
        else:
            self.conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
            self.conn.commit()

    def recent_context(self, limit: int = 40) -> list[dict[str, str]]:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("SELECT topic, angle, title, status FROM shorts ORDER BY id DESC LIMIT %s", (limit,))
                return [dict(row) for row in cur.fetchall()]
        else:
            rows = self.conn.execute(
                "SELECT topic, angle, title, status FROM shorts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_long_form_context(self, limit: int = 20) -> list[dict[str, str]]:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT topic, angle, title, status FROM shorts "
                    "WHERE output_path LIKE %s ORDER BY id DESC LIMIT %s",
                    ("%long-%", limit),
                )
                return [dict(row) for row in cur.fetchall()]
        rows = self.conn.execute(
            "SELECT topic, angle, title, status FROM shorts "
            "WHERE output_path LIKE ? ORDER BY id DESC LIMIT ?",
            ("%long-%", limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _matches_long_form_subject(plan: ShortPlan, previous_topic: str, previous_title: str) -> bool:
        # Two granularities: topic vs topic keeps the word sets small so the
        # overlap ratio stays meaningful, while topic+title catches subjects
        # that only appear in the title ("Hormuz" phrasing varies between them).
        return same_long_form_subject(plan.topic, previous_topic) or same_long_form_subject(
            f"{plan.topic} {plan.title}", f"{previous_topic} {previous_title}"
        )

    def same_long_form_subject_as(self, plan: ShortPlan, limit: int = 40) -> sqlite3.Row | dict | None:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM shorts WHERE output_path LIKE %s ORDER BY id DESC LIMIT %s",
                    ("%long-%", limit),
                )
                rows = cur.fetchall()
                for previous in rows:
                    if self._matches_long_form_subject(plan, str(previous["topic"]), str(previous["title"])):
                        return dict(previous)
            return None
        rows = self.conn.execute(
            "SELECT * FROM shorts WHERE output_path LIKE ? ORDER BY id DESC LIMIT ?",
            ("%long-%", limit),
        ).fetchall()
        for previous in rows:
            if self._matches_long_form_subject(plan, str(previous["topic"]), str(previous["title"])):
                return previous
        return None

    def duplicate_of(self, plan: ShortPlan, threshold: float = 0.52) -> sqlite3.Row | dict | None:
        candidate = f"{plan.topic} {plan.angle} {plan.title}"
        exact = hashlib.sha256(candidate.lower().encode()).hexdigest()
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("SELECT * FROM shorts WHERE fingerprint = %s", (exact,))
                row = cur.fetchone()
                if row:
                    return dict(row)
                cur.execute("SELECT * FROM shorts ORDER BY id DESC LIMIT 150")
                for previous in cur.fetchall():
                    previous_text = f"{previous['topic']} {previous['angle']} {previous['title']}"
                    if similarity(candidate, previous_text) >= threshold:
                        return dict(previous)
        else:
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
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO shorts (created_at, topic, angle, title, fingerprint, status, output_path) VALUES (%s, %s, %s, %s, %s, 'rendering', %s) RETURNING id",
                    (datetime.now(UTC).isoformat(), plan.topic, plan.angle, plan.title, fingerprint, str(output_path)),
                )
                record_id = cur.fetchone()[0]
            self.conn.commit()
            return int(record_id)
        else:
            cursor = self.conn.execute(
                "INSERT INTO shorts (created_at, topic, angle, title, fingerprint, status, output_path) VALUES (?, ?, ?, ?, ?, 'rendering', ?)",
                (datetime.now(UTC).isoformat(), plan.topic, plan.angle, plan.title, fingerprint, str(output_path)),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def mark(self, record_id: int, status: str, youtube_id: str | None = None) -> None:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("UPDATE shorts SET status = %s, youtube_id = COALESCE(%s, youtube_id) WHERE id = %s", (status, youtube_id, record_id))
            self.conn.commit()
        else:
            self.conn.execute("UPDATE shorts SET status = ?, youtube_id = COALESCE(?, youtube_id) WHERE id = ?", (status, youtube_id, record_id))
            self.conn.commit()

    def jobs_created_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM shorts WHERE created_at LIKE %s AND output_path NOT LIKE %s",
                    (today + "%", "%long-%"),
                )
                return int(cur.fetchone()["count"])
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS count FROM shorts "
                "WHERE substr(created_at, 1, 10) = ? AND output_path NOT LIKE ?",
                (today, "%long-%"),
            ).fetchone()
            return int(row["count"])

    def long_form_jobs_created_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM shorts WHERE created_at LIKE %s AND output_path LIKE %s",
                    (today + "%", "%long-%"),
                )
                return int(cur.fetchone()["count"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM shorts WHERE substr(created_at, 1, 10) = ? AND output_path LIKE ?",
            (today, "%long-%"),
        ).fetchone()
        return int(row["count"])

    def latest_long_form_created_at(self) -> datetime | None:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT created_at FROM shorts WHERE output_path LIKE %s "
                    "AND status IN ('rendered', 'published') ORDER BY created_at DESC LIMIT 1",
                    ("%long-%",),
                )
                row = cur.fetchone()
        else:
            row = self.conn.execute(
                "SELECT created_at FROM shorts WHERE output_path LIKE ? "
                "AND status IN ('rendered', 'published') ORDER BY created_at DESC LIMIT 1",
                ("%long-%",),
            ).fetchone()
        if not row:
            return None
        value = str(row["created_at"])
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def resumable_long_form_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return interrupted Long jobs whose assets may still exist on the Volume."""
        statuses = ("rendering", "failed", "upload_failed")
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, output_path FROM shorts WHERE output_path LIKE %s "
                    "AND status IN %s ORDER BY created_at DESC LIMIT %s",
                    ("%long-%", statuses, limit),
                )
                return [dict(row) for row in cur.fetchall()]
        rows = self.conn.execute(
            "SELECT id, status, output_path FROM shorts WHERE output_path LIKE ? "
            "AND status IN (?, ?, ?) ORDER BY created_at DESC LIMIT ?",
            ("%long-%", *statuses, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def resumable_short_form_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return interrupted Short jobs that can continue from files on the Volume."""
        statuses = ("rendering", "failed")
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, output_path FROM shorts WHERE output_path NOT LIKE %s "
                    "AND status IN %s ORDER BY created_at DESC LIMIT %s",
                    ("%long-%", statuses, limit),
                )
                return [dict(row) for row in cur.fetchall()]
        rows = self.conn.execute(
            "SELECT id, status, output_path FROM shorts WHERE output_path NOT LIKE ? "
            "AND status IN (?, ?) ORDER BY created_at DESC LIMIT ?",
            ("%long-%", *statuses, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Manual idea queue (web frontend) ---------------------------------
    def enqueue_idea(
        self, mode: str, idea: str, duration: int | None, publish: bool, privacy: str | None
    ) -> int:
        now = datetime.now(UTC).isoformat()
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO idea_queue (created_at, updated_at, mode, idea, duration, publish, privacy, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending') RETURNING id",
                    (now, now, mode, idea, duration, publish, privacy),
                )
                record_id = cur.fetchone()[0]
            self.conn.commit()
            return int(record_id)
        cursor = self.conn.execute(
            "INSERT INTO idea_queue (created_at, updated_at, mode, idea, duration, publish, privacy, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (now, now, mode, idea, duration, 1 if publish else 0, privacy),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def claim_next_idea(self) -> dict[str, Any] | None:
        """Atomically take the oldest pending idea and mark it processing."""
        now = datetime.now(UTC).isoformat()
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE idea_queue SET status = 'processing', updated_at = %s "
                    "WHERE id = (SELECT id FROM idea_queue WHERE status = 'pending' "
                    "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
                    (now,),
                )
                row = cur.fetchone()
            self.conn.commit()
            return dict(row) if row else None
        row = self.conn.execute(
            "SELECT * FROM idea_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE idea_queue SET status = 'processing', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        self.conn.commit()
        claimed = dict(row)
        claimed["status"] = "processing"
        return claimed

    def get_idea(self, idea_id: int) -> dict[str, Any] | None:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("SELECT * FROM idea_queue WHERE id = %s", (idea_id,))
                row = cur.fetchone()
                return dict(row) if row else None
        row = self.conn.execute("SELECT * FROM idea_queue WHERE id = ?", (idea_id,)).fetchone()
        return dict(row) if row else None

    def update_idea(
        self,
        idea_id: int,
        status: str,
        youtube_id: str | None = None,
        output_title: str | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE idea_queue SET status = %s, updated_at = %s, "
                    "youtube_id = COALESCE(%s, youtube_id), "
                    "output_title = COALESCE(%s, output_title), "
                    "error = COALESCE(%s, error) WHERE id = %s",
                    (status, now, youtube_id, output_title, error, idea_id),
                )
            self.conn.commit()
            return
        self.conn.execute(
            "UPDATE idea_queue SET status = ?, updated_at = ?, "
            "youtube_id = COALESCE(?, youtube_id), "
            "output_title = COALESCE(?, output_title), "
            "error = COALESCE(?, error) WHERE id = ?",
            (status, now, youtube_id, output_title, error, idea_id),
        )
        self.conn.commit()

    def recent_ideas(self, limit: int = 30) -> list[dict[str, Any]]:
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("SELECT * FROM idea_queue ORDER BY id DESC LIMIT %s", (limit,))
                return [dict(row) for row in cur.fetchall()]
        rows = self.conn.execute(
            "SELECT * FROM idea_queue ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_stuck_ideas(self) -> int:
        """Reset ideas left in 'processing' by an interrupted worker back to 'pending'."""
        now = datetime.now(UTC).isoformat()
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE idea_queue SET status = 'pending', updated_at = %s WHERE status = 'processing'",
                    (now,),
                )
                count = cur.rowcount
            self.conn.commit()
            return int(count or 0)
        cursor = self.conn.execute(
            "UPDATE idea_queue SET status = 'pending', updated_at = ? WHERE status = 'processing'",
            (now,),
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)


class OpenAITextClient:
    """Generate JSON planning content through the official OpenAI Responses API."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.long_form_reasoning_effort = settings.text_long_form_reasoning_effort
        self.headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError):
            return response.text[:500] or "unknown error"
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)[:500]
        return str(payload)[:500]

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        parts: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "\n".join(parts).strip()

    def chat(
        self,
        prompt: str,
        temperature: float = 0.55,
        reasoning_effort: str | None = None,
    ) -> str:
        # Keep temperature in the public method for compatibility with the planner.
        # GPT-5.4 mini uses reasoning effort; temperature is intentionally omitted.
        del temperature
        effort = reasoning_effort or self.s.text_reasoning_effort
        payload = {
            "model": self.s.text_model,
            "instructions": "Return only valid JSON. Do not use Markdown fences or add commentary outside the JSON.",
            "input": prompt,
            "reasoning": {"effort": effort},
            "max_output_tokens": self.s.text_max_output_tokens,
        }
        retryable_statuses = {408, 409, 429, 500, 502, 503, 504}
        last_error: Exception | None = None
        for attempt in range(1, self.s.text_attempts + 1):
            LOG.info(
                "Generating content plan with %s (reasoning=%s, attempt %d/%d)...",
                self.s.text_model,
                effort,
                attempt,
                self.s.text_attempts,
            )
            try:
                response = requests.post(
                    self.s.openai_text_endpoint,
                    headers=self.headers,
                    json=payload,
                    timeout=(self.s.text_connect_timeout, self.s.text_read_timeout),
                )
                if not response.ok:
                    detail = self._error_detail(response)
                    error = BotError(f"OpenAI Responses API {response.status_code}: {detail}")
                    if response.status_code not in retryable_statuses:
                        raise error
                    last_error = error
                else:
                    try:
                        response_payload = response.json()
                    except ValueError as exc:
                        last_error = BotError("OpenAI Responses API trả về dữ liệu không phải JSON.")
                        last_error.__cause__ = exc
                    else:
                        if response_payload.get("status") == "incomplete":
                            reason = response_payload.get("incomplete_details", {}).get("reason", "unknown")
                            last_error = BotError(f"OpenAI text response chưa hoàn tất: {reason}")
                        else:
                            text = self._output_text(response_payload)
                            if text:
                                return text
                            last_error = BotError("OpenAI text response không có output_text.")
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
            except BotError:
                raise

            if attempt < self.s.text_attempts:
                delay = self.s.text_retry_backoff_seconds * attempt
                LOG.warning("OpenAI text request failed temporarily: %s; retrying in %ss.", last_error, delay)
                if delay:
                    time.sleep(delay)

        raise BotError(
            f"OpenAI text generation failed after {self.s.text_attempts} attempts: {last_error}"
        ) from last_error


class OpenAIImageClient:
    """Generate low-cost stills through the official OpenAI Image API."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }

    def image(self, prompt: str, destination: Path, width: int = 1080, height: int = 1920) -> None:
        # These two low-quality sizes are the documented ~$0.005 output-price tier for GPT Image 2.
        size = self.s.image_vertical_size if height > width else self.s.image_horizontal_size
        payload = {
            "model": self.s.image_model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": self.s.image_quality,
            "output_format": "jpeg",
            "output_compression": 90,
        }
        partial = destination.with_name(f"{destination.name}.part")
        last_error: Exception | None = None
        for attempt in range(1, self.s.image_attempts + 1):
            if partial.exists():
                partial.unlink()
            LOG.info(
                "Requesting GPT Image 2 scene %s at %s/%s (attempt %d/%d; estimated output ~$0.005)",
                destination.name,
                size,
                self.s.image_quality,
                attempt,
                self.s.image_attempts,
            )
            try:
                response = requests.post(
                    self.s.openai_image_endpoint,
                    headers=self.headers,
                    json=payload,
                    timeout=(self.s.image_connect_timeout, self.s.image_read_timeout),
                )
                if not response.ok:
                    detail = response.text[:800]
                    if response.status_code == 400 and (
                        "moderation_blocked" in detail or "rejected by the safety system" in detail.lower()
                    ):
                        raise ImageGenerationSafetyError(
                            f"OpenAI Image API safety block for {destination.name}: {detail}"
                        )
                    if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
                        raise ImageGenerationTransientError(
                            f"OpenAI Image API {response.status_code}: {detail}", response.status_code
                        )
                    raise BotError(f"OpenAI Image API {response.status_code}: {detail}")
                data = response_json(response)
                images = data.get("data")
                encoded = images[0].get("b64_json") if isinstance(images, list) and images else None
                if not isinstance(encoded, str) or not encoded:
                    raise ImageGenerationTransientError("OpenAI Image API response is missing data[0].b64_json")
                try:
                    image_bytes = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise ImageGenerationTransientError("OpenAI Image API returned invalid base64 image data") from exc
                if len(image_bytes) < 1024:
                    raise ImageGenerationTransientError(
                        f"OpenAI Image API returned only {len(image_bytes)} bytes for {destination.name}"
                    )
                partial.write_bytes(image_bytes)
                partial.replace(destination)
                LOG.info("Saved %s from OpenAI (%.1f MB)", destination.name, len(image_bytes) / (1024 * 1024))
                return
            except requests.RequestException as exc:
                last_error = exc
                LOG.warning("OpenAI image request failed: %s", exc)
            except ImageGenerationTransientError as exc:
                last_error = exc
                LOG.warning("OpenAI image generation failed temporarily: %s", exc)
            finally:
                if partial.exists():
                    partial.unlink()
            if attempt < self.s.image_attempts:
                sleep_for = self.s.image_retry_backoff_seconds * attempt
                LOG.info("Retrying %s in %ss…", destination.name, sleep_for)
                time.sleep(sleep_for)
        raise ImageGenerationTransientError(
            f"OpenAI image generation failed after {self.s.image_attempts} attempts for {destination.name}: {last_error}"
        )


WEB_IMAGE_SOURCE_HOSTS = (
    "unsplash.com",
    "pexels.com",
    "pixabay.com",
    "wikimedia.org",
    "wikipedia.org",
    "nasa.gov",
    "loc.gov",
    "si.edu",
    # Official/government and international-organization sites: their press
    # photos of real newsmakers are public domain or CC BY with attribution
    # (attribution is added to the video description automatically).
    "whitehouse.gov",
    "defense.gov",
    "state.gov",
    "nato.int",
    "europa.eu",
    "un.org",
    "president.gov.ua",
    "gov.uk",
)


class BraveImageSearch:
    """Find optional trusted-source web visuals and retain source pages for attribution."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.used_source_pages: set[str] = set()
        self.sources: list[dict[str, str]] = []
        self._next_search_at = 0.0
        self._disabled_for_run = False

    @staticmethod
    def _first_numeric_header(response: requests.Response, name: str) -> float | None:
        headers = getattr(response, "headers", {})
        raw_value = headers.get(name, "") if hasattr(headers, "get") else ""
        try:
            return max(0.0, float(str(raw_value).split(",", 1)[0].strip()))
        except (TypeError, ValueError):
            return None

    def _wait_for_search_slot(self) -> None:
        wait_seconds = self._next_search_at - time.monotonic()
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _schedule_next_search(self, response: requests.Response, retry_number: int = 0) -> float:
        """Respect Brave's sliding-window headers and a safe one-request/second default."""
        delay = self.s.brave_search_min_interval_seconds
        remaining = self._first_numeric_header(response, "X-RateLimit-Remaining")
        reset = self._first_numeric_header(response, "X-RateLimit-Reset")
        retry_after = self._first_numeric_header(response, "Retry-After")
        if remaining is not None and remaining < 1 and reset is not None:
            delay = max(delay, reset + 0.05)
        if retry_after is not None:
            delay = max(delay, retry_after + 0.05)
        if retry_number:
            delay = max(delay, float(2 ** (retry_number - 1)))
        # Never sleep for a monthly reset. Repeated 429s disable Brave for this
        # job below, allowing OpenAI/the existing-image fallback to continue.
        delay = min(delay, 30.0)
        self._next_search_at = max(self._next_search_at, time.monotonic() + delay)
        return delay

    def _search(self, query: str) -> requests.Response | None:
        if self._disabled_for_run:
            return None
        for attempt in range(1, self.s.brave_search_attempts + 1):
            self._wait_for_search_slot()
            try:
                response = requests.get(
                    self.s.brave_image_search_endpoint,
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self.s.brave_search_api_key,
                        "User-Agent": "youtube-documentary-bot/1.0",
                    },
                    params={
                        "q": query[:400],
                        "count": 20,
                        "country": "ALL",
                        "search_lang": "en",
                        "safesearch": "strict",
                        "spellcheck": "true",
                    },
                    timeout=(self.s.image_connect_timeout, 30),
                )
            except requests.RequestException as exc:
                LOG.warning("Brave image search failed; falling back to OpenAI: %s", exc)
                return None

            status_code = getattr(response, "status_code", 200)
            delay = self._schedule_next_search(response, retry_number=attempt if status_code == 429 else 0)
            if status_code != 429:
                try:
                    response.raise_for_status()
                except requests.RequestException as exc:
                    if status_code in {401, 403}:
                        self._disabled_for_run = True
                    LOG.warning("Brave image search failed; falling back to OpenAI: %s", exc)
                    return None
                return response
            if attempt < self.s.brave_search_attempts:
                LOG.info(
                    "Brave image search rate-limited (HTTP 429); retrying in %.2fs (attempt %d/%d).",
                    delay,
                    attempt + 1,
                    self.s.brave_search_attempts,
                )

        self._disabled_for_run = True
        LOG.warning(
            "Brave image search remained rate-limited after %d attempts; disabling Brave for the rest "
            "of this run and falling back to OpenAI.",
            self.s.brave_search_attempts,
        )
        return None

    @staticmethod
    def _allowed_url(value: str) -> bool:
        try:
            host = (urlparse(value).hostname or "").lower()
        except ValueError:
            return False
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in WEB_IMAGE_SOURCE_HOSTS)

    @staticmethod
    def _matches_orientation(properties: dict[str, Any], width: int, height: int) -> bool:
        result_width = properties.get("width")
        result_height = properties.get("height")
        if not isinstance(result_width, (int, float)) or not isinstance(result_height, (int, float)):
            return True
        return (result_height > result_width) == (height > width)

    def image(self, query: str, destination: Path, width: int, height: int) -> bool:
        if not self.s.brave_search_api_key or self._disabled_for_run:
            return False
        try:
            response = self._search(query)
            if response is None:
                return False
            results = response_json(response).get("results", [])
        except Exception as exc:
            LOG.warning("Brave image search failed; falling back to OpenAI: %s", exc)
            return False

        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict):
                continue
            properties = result.get("properties") if isinstance(result.get("properties"), dict) else {}
            image_url = str(properties.get("url") or "")
            source_page = str(result.get("url") or result.get("source") or "")
            if not image_url or source_page in self.used_source_pages:
                continue
            if not self._allowed_url(image_url) or not self._allowed_url(source_page):
                continue
            if not self._matches_orientation(properties, width, height):
                continue
            try:
                image_response = requests.get(
                    image_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; youtube-documentary-bot/1.0)"},
                    timeout=(self.s.image_connect_timeout, 45),
                )
                image_response.raise_for_status()
                content_type = image_response.headers.get("Content-Type", "").lower()
                image_bytes = image_response.content
                if not content_type.startswith("image/") or not 10_000 <= len(image_bytes) <= 25_000_000:
                    continue
                destination.write_bytes(image_bytes)
            except Exception as exc:
                LOG.debug("Could not download Brave image result %s: %s", image_url, exc)
                continue
            self.used_source_pages.add(source_page)
            self.sources.append({
                "title": str(result.get("title") or "Web image")[:200],
                "source_page": source_page,
                "image_url": image_url,
            })
            LOG.info("Downloaded trusted-source web image for %s via Brave Search.", destination.name)
            return True
        LOG.info("No suitable trusted Brave image result for %s; using OpenAI.", destination.name)
        return False


class VisualAssetProvider:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.openai = OpenAIImageClient(settings)
        self.brave = BraveImageSearch(settings)

    @property
    def web_sources(self) -> list[dict[str, str]]:
        return self.brave.sources

    def image(
        self,
        search_query: str,
        generation_prompt: str,
        destination: Path,
        width: int,
        height: int,
        prefer_web: bool,
        web_only: bool = False,
    ) -> str:
        if prefer_web and self.brave.image(search_query, destination, width, height):
            return "web"
        if prefer_web and web_only:
            return "missing"
        self.openai.image(generation_prompt, destination, width=width, height=height)
        return "openai"


class GoogleCloudTTS:
    """Google Cloud Text-to-Speech narration; independent from the image provider."""

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
            raise BotError(f"Google Cloud TTS không tạo được giọng đọc: {exc}") from exc
        destination.write_bytes(response.audio_content)


class OpenAIShortVietnameseTTS:
    """OpenAI TTS used only for Vietnamese Facebook/TikTok Shorts."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def speech(self, text: str, destination: Path) -> None:
        payload = {
            "model": "gpt-4o-mini-tts",
            "voice": self.s.social_openai_tts_voice,
            "input": text,
            "instructions": (
                "Speak natural Vietnamese from Vietnam for a short documentary social video. "
                "Use the vibe of a patient teacher: calm, warm, clear, and encouraging. "
                "Explain each idea with gentle pacing, smooth pauses, and natural intonation. "
                "Do not sound childish, overly dramatic, or rushed. Do not add, omit, or translate words."
            ),
            "response_format": "mp3",
            "speed": self.s.social_openai_tts_speed,
        }
        try:
            response = requests.post(
                self.s.openai_tts_endpoint,
                headers={
                    "Authorization": f"Bearer {self.s.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(self.s.text_connect_timeout, self.s.text_read_timeout),
            )
        except requests.RequestException as exc:
            raise BotError(f"OpenAI TTS tiếng Việt không thể kết nối: {exc}") from exc
        if not response.ok:
            detail = response.text[:500].strip() or "empty response"
            raise BotError(f"OpenAI TTS tiếng Việt thất bại (HTTP {response.status_code}): {detail}")
        if not response.content:
            raise BotError("OpenAI TTS tiếng Việt trả audio rỗng.")
        destination.write_bytes(response.content)


# Chỉ dẫn dành cho LLM khi VIẾT visual_prompt (không phải để gửi cho model ảnh).
VISUAL_STYLE_RULES = (
    "Write each visual_prompt as one concrete, self-contained sentence describing a single clear subject "
    "and setting for a photorealistic documentary image. "
    "Vary the shot type and composition from scene to scene across the storyboard: mix wide establishing "
    "landscapes, aerial/drone views, macro close-ups of objects or textures, cutaway details, and symbolic "
    "still lifes. Do NOT default to the same framing every time. "
    "CRITICAL: never place a recurring foreground framing device such as an iron fence, metal railing, bars, "
    "cage, or grid in the shot unless the topic is literally about one. "
    "Describe the subject positively and specifically (what IS in frame), not what to avoid. "
    "Avoid readable text, logos, or documents, and avoid close-up human faces or dense crowds. "
    "Prefer environments, objects, forces of nature, space, and animals. Keep every scene vertical 9:16."
)

# Style suffix GỌN gửi kèm cho model ảnh (chỉ token dương, không câu chỉ dẫn, không phủ định).
IMAGE_STYLE_SUFFIX = (
    "cinematic photorealistic documentary photograph, natural lighting, "
    "shallow depth of field, ultra-detailed, sharp focus, 8k, vertical 9:16"
)

LONG_FORM_IMAGE_STYLE_SUFFIX = (
    "cinematic photorealistic documentary photograph, natural lighting, "
    "wide establishing composition, ultra-detailed, sharp focus, 8k, horizontal 16:9"
)

LONG_FORM_VISUAL_STYLE_RULES = (
    "Write each visual_prompt as one concrete, self-contained sentence for a horizontal 16:9 documentary image. "
    "Use broadcast documentary variety: wide establishing shots, maps without readable labels, symbolic still lifes, "
    "infrastructure details, screens without legible text, satellite-like views, and contextual crowd-free scenes. "
    "In visual_prompt avoid readable text, logos, graphic injury, close-up human faces, dense crowds, and names of real people "
    "(the image generator rejects real public figures). "
    "Separately, give every scene a search_query: a short literal news-photo search phrase that DOES name the real person, "
    "place, organization, or object of that beat (e.g. 'Volodymyr Zelenskyy official portrait', 'Ukraine defence ministry building Kyiv'), "
    "so the pipeline can fetch a real licensed photo of the actual subject first. Scene 1's search_query must name the story's "
    "central person or place, because scene 1 becomes the thumbnail. "
    "Do not mention Vietnam, Vietnamese people, Vietnamese officials, or Vietnam-related locations."
)

VIETNAM_BLOCKLIST = (
    "vietnam", "vietnamese", "viet nam", "hanoi", "ha noi", "ho chi minh", "saigon",
    "da nang", "nguyen", "pham", "tran ", "vo ", "to lam",
)

LONG_FORM_TOPIC_DOMAINS = (
    "global politics and elections outside Vietnam",
    "wars, military affairs, defense, and geopolitics",
    "global economy, business, trade, and markets",
    "consumer technology, cybersecurity, AI industry, and major tech companies",
    "major international sports",
    "major world news with clear public impact",
)

# The Shorts --theme default is history-flavoured; long-form is a current-events
# news digest, so it gets its own default theme unless the user overrides --theme.
LONG_FORM_DEFAULT_THEME = (
    "an international current-events news digest rotating across global politics, "
    "conflicts and defense, economy and business, technology, major sports, and "
    "consequential world news"
)

LONG_FORM_EDITORIAL_LANES = {
    "politics_elections": {
        "label": "global politics, elections, governments, diplomacy, or major policy decisions",
        "feed_categories": ("top", "world"),
    },
    "conflicts_defense": {
        "label": "wars, military affairs, defense, and geopolitics",
        "feed_categories": ("world", "top"),
    },
    "economy_business": {
        "label": "global economy, business, trade, markets, companies, or consumer costs",
        "feed_categories": ("business",),
    },
    "technology": {
        "label": "consumer technology, cybersecurity, AI industry, or major technology companies",
        "feed_categories": ("technology",),
    },
    "sports": {
        "label": "major international sports with broad public interest",
        "feed_categories": ("sports",),
    },
    "global_headlines": {
        "label": "major world news with clear public impact outside politics, war, business, tech, and sports",
        "feed_categories": ("top", "world"),
    },
}

LONG_FORM_LANE_KEYWORDS = {
    "politics_elections": (
        "ballot", "campaign", "congress", "diplomat", "election", "government",
        "law", "minister", "parliament", "policy", "politic", "president",
        "senate", "summit", "trump", "vote",
    ),
    "conflicts_defense": (
        "army", "attack", "ceasefire", "defense", "drone", "geopolit", "hormuz",
        "iran", "israel", "military", "missile", "nato", "navy", "nuclear",
        "russia", "strike", "troop", "ukraine", "war",
    ),
    "economy_business": (
        "airline", "bank", "business", "company", "consumer", "cost", "econom",
        "finance", "fuel", "inflation", "market", "oil", "price", "shipping",
        "stock", "trade",
    ),
    "technology": (
        "ai ", "android", "apple", "chip", "codex", "cyber", "google", "microsoft",
        "openai", "semiconductor", "software", "tech", "xbox",
    ),
    "sports": (
        "athlete", "basketball", "championship", "cup", "fifa", "final", "football",
        "game", "league", "match", "nba", "nfl", "olympic", "soccer", "sport",
        "team", "tournament",
    ),
    "global_headlines": (
        "accident", "aviation", "crash", "disaster", "earthquake", "evacuation",
        "flood", "heatwave", "outage", "rescue", "storm", "wildfire",
    ),
}


def classify_long_form_lane(text: str) -> str:
    normalized = f" {unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()} "
    scores = {
        lane: sum(1 for keyword in keywords if keyword in normalized)
        for lane, keywords in LONG_FORM_LANE_KEYWORDS.items()
    }
    best_lane = max(scores, key=scores.get)
    return best_lane if scores[best_lane] else "global_headlines"


def choose_long_form_editorial_lane(
    past: list[dict[str, str]],
    excluded: set[str] | None = None,
) -> str:
    excluded = set(excluded or ())
    recent_lanes = [
        classify_long_form_lane(f"{row.get('topic', '')} {row.get('angle', '')} {row.get('title', '')}")
        for row in past[:3]
    ]
    candidates = [
        lane for lane in LONG_FORM_EDITORIAL_LANES
        if lane not in excluded and lane not in recent_lanes
    ]
    if not candidates:
        candidates = [lane for lane in LONG_FORM_EDITORIAL_LANES if lane not in excluded]
    if not candidates:
        candidates = list(LONG_FORM_EDITORIAL_LANES)
    return random.choice(candidates)


def news_context_for_lane(news_context: list[dict[str, str]], lane: str) -> list[dict[str, str]]:
    categories = set(LONG_FORM_EDITORIAL_LANES[lane]["feed_categories"])
    return [item for item in news_context if item.get("category") in categories]


def recent_long_form_subject_texts(past: list[dict[str, str]], limit: int = 12) -> list[str]:
    """Topic+title of recent long-form videos; used as a subject cooldown list."""
    subjects: list[str] = []
    for row in past[:limit]:
        text = " ".join(
            part for part in (str(row.get("topic", "")), str(row.get("title", ""))) if part
        ).strip()
        if text:
            subjects.append(text)
    return subjects


def news_item_covers_recent_subject(item: dict[str, str], covered_subjects: list[str]) -> bool:
    headline_words = long_form_subject_words(f"{item.get('title', '')} {item.get('summary', '')}")
    return any(
        len(headline_words & long_form_subject_words(subject)) >= 2
        for subject in covered_subjects
    )


def fresh_news_context_for_lane(
    news_context: list[dict[str, str]],
    lane: str,
    covered_subjects: list[str],
) -> list[dict[str, str]]:
    """Lane headlines minus recently covered subjects, in random order.

    Shuffling matters: the model anchors on the first (loudest) headline, which
    is how one dominant story ends up in every video.
    """
    fresh = [
        item for item in news_context_for_lane(news_context, lane)
        if not news_item_covers_recent_subject(item, covered_subjects)
    ]
    random.shuffle(fresh)
    return fresh


CURIOSITY_TOPIC_CATEGORIES = [
    "World-historical figures outside Vietnam with durable international recognition: reveal one documented decision, failure, rivalry, reform, campaign, or contradiction that helps explain the person's larger historical impact. The specific detail must unlock the major figure's significance, not function as celebrity trivia.",
    "Major historical events outside Vietnam that changed a nation, region, empire, or international order: tell one decisive moment, overlooked mistake, unlikely turning point, deception, survival decision, or consequence that changed the recognizable outcome.",
    "World wonders and architecture, including iconic major engineering works, ancient or modern: explain how a named landmark or megaproject was built, its hardest structural problem, symbolism, human cost, or a decisive episode in its history. Choose globally or nationally significant works, not an ordinary building or small local structure.",
    "World-renowned natural wonders and extreme geography: focus on a named, widely recognized place, how its defining feature formed, what makes it globally exceptional, a record it holds, or how it shaped exploration, settlement, borders, or civilization.",
    "Major civilizations, empires, lost cities, and consequential historical mysteries: center the story on a widely significant civilization, ruler, capital, monument, conflict, collapse, or surviving historical record and clearly separate evidence from legend.",
    "Major wars, battles, imperial struggles, revolutions, and political turning points outside Vietnam: focus on one concrete tactic, decision, route, betrayal, logistical failure, or unexpected event that helps explain a large historical outcome rather than a minor skirmish or local dispute.",
    "Historically transformative disasters or collapses only: choose events whose consequences changed national or international law, industry, public policy, warfare, urban planning, or public memory for generations. Reject isolated local accidents, one-building failures, sensational death toll stories, and small incidents remembered mainly as trivia.",
    "Globally recognized monuments, cultural heritage, and symbols: explain a documented creation, transformation, controversy, or historical turning point that shaped identity across a nation, civilization, or multiple countries. Reject food-origin trivia, small customs, and novelty-first anecdotes.",
]

ABSTRACT_SHORT_TOPIC_TERMS = (
    "abstract mechanism",
    "biological carrying capacity",
    "cognitive processing",
    "conceptual model",
    "climate trap",
    "earth's carrying capacity",
    "earth's biological",
    "human processing engine",
    "planetary feedback",
    "planet can lock",
    "processing engine",
    "self-feeding climate",
    "systems theory",
    "theoretical framework",
)

MINOR_SHORT_STORY_TERMS = (
    "city disaster",
    "factory accident",
    "local accident",
    "local disaster",
    "municipal accident",
    "one building failed",
    "one tank failed",
    "small town disaster",
    "underbuilt tank",
)

SCIENCE_NEWS_BLOCKLIST = (
    "archaeologists",
    "archaeology",
    "asteroid",
    "clinical trial",
    "exoplanet",
    "fossil discovery",
    "medical study",
    "nasa mission",
    "new species",
    "researchers discover",
    "scientists discover",
    "space telescope",
    "study finds",
)

# Trục ĐỊNH DẠNG kể chuyện — độc lập với chủ đề, quyết định "kiểu" video để phá thế đơn điệu.
NARRATIVE_FORMATS = [
    "MYSTERY: pose a genuine unsolved question and walk through the leading explanation.",
    "SUPERLATIVE / RECORD: reveal the most extreme example of something (biggest, oldest, deadliest, fastest) and why it holds that record.",
    "HOW-IT-WORKS: reveal the hidden mechanism behind a familiar phenomenon, object, or process, step by step.",
    "RISE-AND-FALL: tell the arc of how something great emerged, peaked, and collapsed.",
    "MYTH-BUSTER: state a widely believed 'fact' and correct it with the real evidence.",
    "HIDDEN-IN-PLAIN-SIGHT: expose a surprising secret or backstory behind something ordinary the viewer already knows.",
    "ORIGIN STORY: trace where a world-changing thing, idea, or creature actually came from.",
    "NUMBER-SHOCK: build the whole video around one staggering, hard-to-believe statistic or scale comparison.",
    "TRANSFORMATION: show a dramatic before-and-after change in a place, species, or technology over time.",
    "WHAT-IF / CONSEQUENCE: explore a real turning point and the outsized consequences that followed (or almost did).",
]

# Trục KIỂU TIÊU ĐỀ — buộc tiêu đề xoay vòng, không phải lúc nào cũng "Why did...".
TITLE_STYLES = [
    "a bold declarative statement (NOT a question), e.g. 'This Lake Turns Animals to Stone'",
    "a curiosity-gap teaser that withholds the payoff, e.g. 'The Metal That Ended an Empire'",
    "a superlative claim, e.g. 'The Loneliest Tree on Earth'",
    "a short question — but only occasionally, and vary the opening word (not always 'Why')",
    "a surprising number or scale hook, e.g. '600 Years, One Unsolved Blueprint'",
    "a vivid noun phrase naming the subject and its twist, e.g. 'The Library That Erased Itself'",
]


def get_random_topic_rule() -> str:
    category = random.choice(CURIOSITY_TOPIC_CATEGORIES)
    narrative_format = random.choice(NARRATIVE_FORMATS)
    title_style = random.choice(TITLE_STYLES)
    return (
        f"CRITICAL INSTRUCTION: For this specific video, you MUST strictly focus ONLY on this sub-category: **{category}**. "
        "Do not write about anything outside of this category. "
        f"NARRATIVE FORMAT for this video (shape the whole story around it, do not force it into a generic mystery): **{narrative_format}** "
        f"TITLE STYLE to aim for: **{title_style}**. "
        "Only about one in three videos should use a question title; prefer confident declarative or teaser titles otherwise, "
        "and never begin the title with 'Why' unless the narrative format is genuinely MYSTERY. "
        "Choose a major, widely significant subject first, then find a concrete surprising detail inside it. Historical stature and learning value come before shock, oddity, or viral trivia. "
        "Reject local accidents, one-building or one-company incidents, obscure anecdotes, and novelty-first stories unless they clearly changed a nation, civilization, major industry, or international history for generations. "
        "Reject academic theories, unnamed hypothetical systems or planets, and metaphorical thesis-style angles. "
        "Do not hardcode the exact examples, but generate similarly captivating concepts."
    )


def recent_title_openers(past: list[dict[str, str]], limit: int = 8) -> str:
    """Trả về các từ mở đầu tiêu đề dùng gần đây (để yêu cầu LLM tránh lặp khuôn)."""
    openers: list[str] = []
    for row in past[:limit]:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        words = re.findall(r"[A-Za-z']+", title)
        if not words:
            continue
        opener = " ".join(words[:2]).lower()
        if opener and opener not in openers:
            openers.append(opener)
    return ", ".join(f"'{o}'" for o in openers)


PLAN_SCHEMA = '''{
  "topic":"short English topic", "angle":"specific surprising angle", "title":"<=100 chars and explicitly names the concrete main subject", "thumbnail_text":"2-5 words, names the same main subject",
  "description":"English description with exactly 2 hashtags", "tags":["exactly 2 tags"],
  "hook":"first spoken sentence, <=12 words", "narration":"English narration that begins with hook and ends with closing_line",
  "closing_line":"last spoken sentence, <=14 words",
  "scenes":[{"duration":5.5,"visual_prompt":"English photorealistic documentary image prompt: one concrete subject + setting, describe only what IS in frame; vary the shot type between scenes"}],
  "fact_note":"what uncertainty was avoided", "source_hints":["institution or primary-source lead"],
  "subject_stature_score":9, "historical_significance_score":9, "broad_learning_value_score":9,
  "significance_reason":"one sentence explaining the subject's durable national, civilizational, or global importance"
}'''

LONG_FORM_PLAN_SCHEMA = '''{
  "topic":"specific current global topic, not related to Vietnam",
  "angle":"specific explanatory angle with broad viewer appeal",
  "title":"<=100 chars, English, clickable but factual, explicitly names the main person/place/event/company/route",
  "thumbnail_text":"2-5 words, names the same main subject for large thumbnail text",
  "description":"English YouTube description with exactly 2 hashtags and a brief AI-assisted disclosure",
  "tags":["exactly 2 tags"],
  "hook":"first spoken sentence, <=18 words",
  "narration":"English narration that begins with hook and ends with closing_line",
  "closing_line":"last spoken sentence, <=18 words",
  "scenes":[{"duration":18.0,"visual_prompt":"horizontal 16:9 documentary image prompt for this chapter beat","search_query":"2-6 word literal news photo search naming the real person, place, organization, or object of this beat (e.g. 'Zelenskyy press conference Kyiv'); empty string only for purely conceptual beats"}],
  "fact_note":"what uncertainty was preserved or avoided",
  "source_hints":["headline/source lead used from the supplied news context"]
}'''

RESEARCH_SCHEMA = '''{
  "curiosity_frame":"match the assigned narrative format: mystery, record/superlative, hidden mechanism, rise-and-fall, myth correction, hidden origin, staggering number, dramatic transformation, or consequence",
  "viewer_question":"the single clickable hook this Short delivers — phrase it as a question ONLY if the format is a mystery, otherwise as a bold promise or reveal statement", "stakes":"why a broad viewer should care",
  "thumbnail_hint":"2-4 words for a vivid thumbnail concept",
  "concrete_anchor":"the named person, place, object, event, rule, mistake, price, or feature at the center of the story",
  "viewer_payoff":"the concrete answer, historical insight, construction detail, discovery, decision, or consequence the viewer learns",
  "share_trigger":"the one-sentence fact a viewer would repeat to a friend",
  "surprise_payoff":"the specific reveal that makes the hook worth watching",
  "central_claim":"one defensible claim", "evidence_points":["fact 1","fact 2","fact 3"],
  "uncertainty":"what must be qualified or omitted", "fresh_angle":"a non-repetitive narrative angle",
  "subject_stature_score":9, "historical_significance_score":9, "broad_learning_value_score":9,
  "significance_reason":"durable national, civilizational, or global importance beyond the surprising detail",
  "source_leads":["credible primary institution, archive, museum, or research body"],
  "avoid":["specific overclaim or cliché to avoid"]
}'''


def extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise BotError(f"OpenAI không trả JSON: {text[:400]}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise BotError(f"OpenAI trả JSON lỗi: {exc}") from exc


def mentions_vietnam(text: str) -> bool:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return any(term in normalized for term in VIETNAM_BLOCKLIST)


def clean_feed_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"&(?:amp|quot|apos|lt|gt);", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_trending_news_context(limit: int = 28) -> list[dict[str, str]]:
    feeds = {
        "top": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
        "world": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
        "business": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
        "technology": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
        "sports": "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-US&gl=US&ceid=US:en",
    }
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    per_feed_limit = max(1, (limit + len(feeds) - 1) // len(feeds))
    for category, url in feeds.items():
        try:
            response = requests.get(url, timeout=(10, 30))
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except Exception as exc:
            LOG.warning("Could not fetch %s news RSS: %s", category, exc)
            continue
        category_items = 0
        for item in root.findall(".//item"):
            title = clean_feed_text(item.findtext("title") or "")
            summary = clean_feed_text(item.findtext("description") or "")
            link = clean_feed_text(item.findtext("link") or "")
            published = clean_feed_text(item.findtext("pubDate") or "")
            normalized_news = f"{title} {summary}".lower()
            if (
                not title
                or mentions_vietnam(normalized_news)
                or any(term in normalized_news for term in SCIENCE_NEWS_BLOCKLIST)
            ):
                continue
            key = re.sub(r"\W+", "", title.lower())[:90]
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "category": category,
                "title": title[:220],
                "summary": summary[:320],
                "published": published[:80],
                "link": link[:300],
            })
            category_items += 1
            if category_items >= per_feed_limit:
                break
    return items[:limit]


def fetch_news_for_idea(idea: str, limit: int = 12) -> list[dict[str, str]]:
    """Google News RSS search for headlines related to a manual idea (used as factual leads)."""
    query = re.sub(r"\s+", " ", idea or "").strip()[:200]
    if not query:
        return []
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        response = requests.get(url, timeout=(10, 30))
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:
        LOG.warning("Could not fetch related news for the idea: %s", exc)
        return []
    for item in root.findall(".//item"):
        title = clean_feed_text(item.findtext("title") or "")
        if not title:
            continue
        key = re.sub(r"\W+", "", title.lower())[:90]
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title[:220],
            "summary": clean_feed_text(item.findtext("description") or "")[:320],
            "published": clean_feed_text(item.findtext("pubDate") or "")[:80],
            "link": clean_feed_text(item.findtext("link") or "")[:300],
        })
        if len(items) >= limit:
            break
    LOG.info("Fetched %d related news leads for the manual idea.", len(items))
    return items


def research_brief(
    llm: OpenAITextClient,
    theme: str,
    topic_rule: str,
    past: list[dict[str, str]] | None = None,
    rejected: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    past = past or []
    rejected = rejected or []
    prompt = f'''Act as a high-retention research editor for an English general-audience YouTube Short.
Theme: {theme}
Use your reasoning internally before responding. Return only JSON using this schema:
{RESEARCH_SCHEMA}
Topic strategy: {topic_rule}
Rules: Choose one specific, evidence-based topic with a named person, place, object, event, rule, mistake, price, or visible feature. It must deliver a concrete surprise or useful real-world understanding that an ordinary viewer can grasp immediately. Do not invent sources, data, dates, quotations, or expert opinions. A source_lead is only a lead for later verification, never a claim that you accessed it.
Historical-scale gate: the central SUBJECT itself must be important enough for a serious world-history documentary before considering its surprising angle. Require all three honest scores — subject_stature_score, historical_significance_score, and broad_learning_value_score — to be at least 8/10. A famous-sounding headline is not evidence of significance. Do not inflate scores to save a weak candidate.
Reject a candidate if it is mainly a quirky local incident, isolated industrial accident, one-building failure, municipal episode, obscure personal anecdote, food-origin fact, or shocking number with little durable consequence. An event centered on one city or company qualifies only when it clearly changed national or international law, institutions, industry, borders, warfare, culture, or public life for generations.
Prefer a specific revealing detail INSIDE a major subject: a consequential decision by a world-historical person, a turning point in a major event or war, a structural challenge in an iconic work, or a defining feature of a renowned place. The detail is the storytelling lens; it must not be the entire reason the subject seems interesting.
Concrete/retellability test: silently reject the candidate unless it passes at least THREE of these tests: (1) centers a named person, event, place, structure, civilization, object, species, invention, mission, or discovery; (2) contains one documented decision, construction detail, obstacle, mistake, turning point, hidden feature, record, or discovery; (3) delivers a surprising answer that can be retold to a friend in one sentence; (4) has a vivid scene or object that can be shown clearly; (5) explains why the subject mattered in history or what changed because of it.
Clickability filter: before selecting the topic, silently reject candidates that sound like a procedural report, a routine measurement update, a narrow technical footnote, a low-stakes institutional detail, a classroom theory, a speculative planetary scenario, or an academic concept with no concrete story. Never frame a person as a metaphorical "processing engine" and never build the story around carrying capacity, systems theory, a conceptual framework, or an unnamed planet. The final viewer_question should feel like a specific, surprising documentary story someone would click, save, or share without already knowing the subject.
Novelty rule: The topic and fresh_angle must be materially different from every item in the existing archive and rejected candidates below. Do not choose the same object, event, artifact, site, person, mechanism, or central claim. If a broad theme keeps pointing to the same subject, switch domains within the theme.
Existing archive: {json.dumps(past, ensure_ascii=False)}
Rejected candidates from this run: {json.dumps(rejected, ensure_ascii=False)}'''
    LOG.info("Research pass: selecting a defensible, novel story angle…")
    return extract_json(llm.chat(prompt, temperature=0.35))


def plan_short(
    llm: OpenAITextClient,
    archive: Archive,
    theme: str,
    duration: int,
    rejected: list[dict[str, str]] | None = None,
) -> ShortPlan:
    past = archive.recent_context()
    rejected = rejected or []
    topic_rule = get_random_topic_rule()
    recent_openers = recent_title_openers(past)
    opener_rule = (
        f"TITLE VARIETY: recent videos already opened their titles with these words: {recent_openers}. "
        "Do NOT start this title with any of them; pick a clearly different opening. "
        if recent_openers
        else "TITLE VARIETY: vary the opening word of the title; do not default to 'Why'. "
    )
    brief = research_brief(llm, theme, topic_rule, past, rejected)
    target_minimum_words, target_maximum_words = target_narration_word_bounds(duration)
    minimum_words, maximum_words = narration_word_bounds(duration)
    prompt = f'''Act as a senior viral documentary writer. Create ONE highly watchable {duration}-second English-language YouTube Short plan from the editorial brief below.
Theme: {theme}
Audience: curious general English-speaking viewers, not academics or specialists. Keep the channel centered on world-historical figures, major events, civilizations, wars, empires, renowned natural wonders, iconic architecture, major landmarks, monuments, and transformative cultural history.
Topic strategy: {topic_rule}
{opener_rule}
Use the editorial brief's viewer_question, stakes, and thumbnail_hint to make the Short feel specific, surprising, and worth remembering or sharing in the assigned narrative format — not a neutral encyclopedia entry, classroom lesson, consumer tip, or abstract theory.
SIGNIFICANCE REQUIREMENT: Preserve honest subject_stature_score, historical_significance_score, and broad_learning_value_score values of at least 8/10. The narration must teach why the larger subject mattered, not merely recount the surprising detail. Reject local incidents and trivia dressed up with dramatic language.
The topic, angle, title, and hook must name or clearly point to the brief's concrete_anchor. Fully deliver the viewer_payoff, share_trigger, and surprise_payoff. A viewer should be able to retell the core story in one plain sentence.
TITLE REQUIREMENT: The title must explicitly name the concrete_anchor — the actual person, landmark, place, structure, event, civilization, object, or discovery — rather than hiding it behind "this", "that", "the secret", or a generic mystery phrase. Make the named subject appear early in the title whenever natural. For example, write "The Three Gorges Dam's Hidden Problem", not "The Dam Nobody Saw Coming". Set thumbnail_text to 2-5 bold words that name the same subject; it is not a vague slogan.
Use a sharp curiosity hook in the first 1.5 seconds, a clear escalation or reversal in the middle, and a concise closing line that makes the viewer think. The narration must start verbatim with hook and end verbatim with closing_line.
CRITICAL NARRATIVE RULE: The story must strictly follow a 3-part structure:
1. BEGINNING (Context): Immediately establish the facts: Who? Where? When? What happened? Never jump straight into a mystery without setting the scene.
2. MIDDLE (Story and reveal): Show the decision, construction challenge, obstacle, mistake, discovery, conflict, hidden feature, cause, or turning point using concrete details and simple cause-and-effect.
3. ENDING (Meaning): Deliver the promised answer and finish with the historical significance, lasting consequence, striking comparison, or memorable fact worth retelling.
Ensure the narration flows logically and is highly accessible to a general audience.
Use plain spoken language, define any necessary technical term immediately, keep one main idea per sentence, and replace abstract filler with concrete cause-and-effect or scale comparisons. Fully pay off the opening hook before the closing line.
Do not use thesis-like angles such as "X as a human engine," "a planet locking itself into a climate trap," "hacking Earth's carrying capacity," or similar metaphorical academic framing. Do not select standalone topics about inventions, engineering achievements, scientific discoveries, animals, archaeology, astronomy, or space exploration. Construction details are allowed only when they directly explain a named landmark, monument, building, bridge, dam, palace, temple, or other architectural work.
Facts: only use the supplied evidence points. Preserve the uncertainty exactly when relevant. Never turn a source lead into a citation or claim it was consulted.
Visuals: {VISUAL_STYLE_RULES}
Storyboard rhythm: make each scene visually distinct, such as hook image, map/diagram, evidence close-up, mechanism/process reveal, and closing visual metaphor. Intentional slight movement discontinuity is acceptable; vertical 9:16.
Split the story into exactly 6 scenes whose total duration is exactly {duration}. Reusing a visual concept is acceptable when it helps continuity, but every scene still needs a concrete visual_prompt. For a {duration}-second Short, aim for roughly {target_minimum_words}–{target_maximum_words} spoken English words. This is a preferred pace, not a reason to pad a clear story with filler.
Every string in the returned JSON must be English, including topic, title, description, tags, narration, fact_note, and source_hints.
The existing archive and rejected candidates below must not be repeated or merely reframed. Return raw JSON only using exactly this schema:
{PLAN_SCHEMA}
Editorial brief: {json.dumps(brief, ensure_ascii=False)}
Existing archive: {json.dumps(past, ensure_ascii=False)}
Rejected candidates from this run: {json.dumps(rejected, ensure_ascii=False)}'''
    LOG.info("Writing pass: turning the research brief into a short-form story…")
    draft = ShortPlan.from_dict(extract_json(llm.chat(prompt, temperature=0.65)))
    review_prompt = f'''Act as the final fact, documentary-story, and retention editor. Think deeply but return only JSON.
Improve the draft below into a stronger {duration}-second English YouTube Short. Return exactly:
{{"quality_check":{{"hook_score":1,"clarity_score":1,"concreteness_score":1,"retellability_score":1,"shareability_score":1,"surprise_score":1,"subject_stature_score":1,"historical_significance_score":1,"broad_learning_value_score":1,"factual_risk":"short note","changes":["short note"]}},"plan":{PLAN_SCHEMA}}}
The plan must retain only claims supported by the editorial brief. Reject hype, vague filler, fake certainty, generic endings, repetition, consumer-tip drift, academic framing, speculative planetary scenarios, minor local incidents, novelty-first trivia, and dry topics that lack a strong concrete story. Score historical stature honestly: a candidate below 8/10 on subject stature, historical significance, or broad learning value must be rejected rather than rescued with dramatic wording. Rewrite any abstract angle into a named person/place/object/event/structure/discovery story; if that is impossible, replace it with a better candidate from the assigned documentary category. Make the hook immediately intriguing, the middle concrete, and the closing line memorable enough to retell or share. Use plain spoken English and ensure the narration clearly pays off the hook while explaining the larger subject's lasting importance. Keep the title in the assigned style: it may be a bold declarative statement, a curiosity-gap teaser, a superlative, a number hook, or a question — but do NOT reflexively rewrite it into a "Why..." question, and only keep a question title if the story is genuinely a mystery. The title must explicitly name the concrete_anchor, never a pronoun-only or generic subject; thumbnail_text must be 2-5 words naming that same main subject. {opener_rule}The narration must begin with hook and end with closing_line. Keep exactly 6 scenes with durations totaling exactly {duration}. Preserve this visual direction in every scene: {VISUAL_STYLE_RULES}
Editorial brief: {json.dumps(brief, ensure_ascii=False)}
Draft: {json.dumps(draft.to_dict(), ensure_ascii=False)}'''
    LOG.info("Quality pass: checking factual precision, hook, pacing, and ending…")
    reviewed = extract_json(llm.chat(review_prompt, temperature=0.4))
    if not isinstance(reviewed.get("plan"), dict):
        raise BotError("OpenAI quality pass thiếu trường plan.")
    plan = ShortPlan.from_dict(reviewed["plan"])
    review_quality = reviewed.get("quality_check", {})
    if not plan.subject_stature_score:
        plan.subject_stature_score = int(review_quality.get("subject_stature_score") or 0)
    if not plan.historical_significance_score:
        plan.historical_significance_score = int(review_quality.get("historical_significance_score") or 0)
    if not plan.broad_learning_value_score:
        plan.broad_learning_value_score = int(review_quality.get("broad_learning_value_score") or 0)
    ensure_title_names_main_subject(plan)
    normalize_scene_count(plan, 6)
    quality = reviewed.get("quality_check", {})
    LOG.info("Quality pass complete — hook %s/10, clarity %s/10.", quality.get("hook_score", "?"), quality.get("clarity_score", "?"))
    scene_total = sum(scene.duration for scene in plan.scenes)
    words = len(re.findall(r"\b\w+\b", plan.narration, flags=re.UNICODE))
    if abs(scene_total - duration) > 0.1:
        raise BotError(f"OpenAI chia cảnh {scene_total:g}s, không đúng mục tiêu {duration}s.")
    if not minimum_words <= words <= maximum_words:
        raise BotError(f"Kịch bản có {words} từ, ngoài khoảng phù hợp cho Short {duration}s.")
    clean_narration = re.sub(r"[\W_]+", "", plan.narration).lower()
    clean_hook = re.sub(r"[\W_]+", "", plan.hook).lower()
    clean_closing = re.sub(r"[\W_]+", "", plan.closing_line).lower()
    if not clean_narration.startswith(clean_hook) or not clean_narration.endswith(clean_closing):
        LOG.warning("Narration does not match hook/closing_line. Auto-correcting...")
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", plan.narration) if part.strip()]
        if sentences:
            if not re.sub(r"[\W_]+", "", sentences[0]).lower().startswith(clean_hook):
                sentences[0] = plan.hook
            if not re.sub(r"[\W_]+", "", sentences[-1]).lower().endswith(clean_closing):
                sentences[-1] = plan.closing_line
            plan.narration = " ".join(sentences)

            clean_narration = re.sub(r"[\W_]+", "", plan.narration).lower()
            if not clean_narration.startswith(clean_hook):
                plan.narration = plan.hook + " " + plan.narration
            if not re.sub(r"[\W_]+", "", plan.narration).lower().endswith(clean_closing):
                plan.narration = plan.narration + " " + plan.closing_line

    words = len(re.findall(r"\b\w+\b", plan.narration, flags=re.UNICODE))
    if not minimum_words <= words <= maximum_words:
        raise BotError(f"Kịch bản có {words} từ, ngoài khoảng phù hợp cho Short {duration}s.")
    LOG.info("Plan ready: %r (%d scenes, %d words)", plan.title, len(plan.scenes), words)
    return plan


def long_form_word_bounds(duration: int) -> tuple[int, int]:
    # Broad content safety range. The actual audio duration now controls the
    # rendered timeline, so this must not act as a hard target-duration gate.
    return max(60, round(duration * 0.5)), round(duration * 5.0)


def target_long_form_word_bounds(duration: int) -> tuple[int, int]:
    """Word budget matched to the measured Google TTS pace (~2.55 words/sec).

    The rendered timeline follows the narration audio, so words-per-second is
    what actually controls the final video length. The old 3.0-3.45 factor
    produced ~9-minute videos from a 6.6-minute plan.
    """
    return round(duration * 2.4), round(duration * 2.6)


def image_scene_prompt_horizontal(visual_prompt: str) -> str:
    return f"{visual_prompt.strip().rstrip('.')}. {LONG_FORM_IMAGE_STYLE_SUFFIX}"


TITLE_SUBJECT_STOP_WORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with",
    "this", "that", "these", "those", "why", "how", "what", "when", "where", "who", "is", "are", "was", "were",
}


def title_mentions_main_subject(plan: ShortPlan) -> bool:
    anchor = plan.thumbnail_text or plan.topic
    anchor_terms = {
        term.lower()
        for term in re.findall(r"[A-Za-z0-9]+", anchor)
        if len(term) >= 3 and term.lower() not in TITLE_SUBJECT_STOP_WORDS
    }
    title_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9]+", plan.title)}
    return not anchor_terms or bool(anchor_terms & title_terms)


def ensure_title_names_main_subject(plan: ShortPlan) -> None:
    """Prevent a vague headline when the planner omitted its named main subject."""
    if title_mentions_main_subject(plan):
        return
    anchor = re.sub(r"\s+", " ", (plan.thumbnail_text or plan.topic).strip())
    if not anchor:
        return
    prefix = f"{anchor}: "
    remaining = max(0, 100 - len(prefix))
    plan.title = (prefix + plan.title[:remaining].rstrip()).rstrip(": ")[:100]
    LOG.info("Prefixed the title with its named main subject: %r", anchor)


def normalize_scene_count(plan: ShortPlan, target_count: int) -> None:
    """Keep the same timeline while enforcing the visual budget, reusing concepts when needed."""
    while len(plan.scenes) < target_count:
        index = max(range(len(plan.scenes)), key=lambda item: plan.scenes[item].duration)
        scene = plan.scenes[index]
        first_duration = round(scene.duration / 2, 2)
        second_duration = round(scene.duration - first_duration, 2)
        scene.duration = first_duration
        plan.scenes.insert(
            index + 1,
            Scene(duration=second_duration, visual_prompt=scene.visual_prompt, search_query=scene.search_query),
        )
    while len(plan.scenes) > target_count:
        extra = plan.scenes.pop()
        plan.scenes[-1].duration = round(plan.scenes[-1].duration + extra.duration, 2)


def validate_long_form_plan(
    plan: ShortPlan,
    duration: int,
    min_words: int,
    max_words: int,
    expected_scene_count: int,
) -> None:
    combined_text = " ".join([plan.topic, plan.angle, plan.title, plan.description, plan.narration, *plan.source_hints])
    if mentions_vietnam(combined_text):
        raise BotError("Long-form plan bi loai vi co noi dung lien quan den Viet Nam.")
    scene_total = sum(scene.duration for scene in plan.scenes)
    if abs(scene_total - duration) > 0.1:
        if abs(scene_total - duration) <= 2 and plan.scenes:
            plan.scenes[-1].duration = round(plan.scenes[-1].duration + (duration - scene_total), 2)
        else:
            raise BotError(f"OpenAI chia canh long-form {scene_total:g}s, khong dung muc tieu {duration}s.")
    if len(plan.scenes) != expected_scene_count:
        raise BotError(
            f"OpenAI tao {len(plan.scenes)} canh long-form; bot yeu cau dung {expected_scene_count} canh."
        )
    words = spoken_word_count(plan.narration)
    if not min_words <= words <= max_words:
        raise BotError(f"Kich ban long-form co {words} tu, ngoai khoang {min_words}-{max_words}.")


def ensure_long_form_hook_and_closing(plan: ShortPlan) -> None:
    clean_narration = re.sub(r"[\W_]+", "", plan.narration).lower()
    clean_hook = re.sub(r"[\W_]+", "", plan.hook).lower()
    clean_closing = re.sub(r"[\W_]+", "", plan.closing_line).lower()
    if not clean_narration.startswith(clean_hook):
        plan.narration = f"{plan.hook} {plan.narration}"
    if not re.sub(r"[\W_]+", "", plan.narration).lower().endswith(clean_closing):
        plan.narration = f"{plan.narration} {plan.closing_line}"


def expand_long_form_plan(
    llm: OpenAITextClient,
    plan: ShortPlan,
    duration: int,
    min_words: int,
    max_words: int,
    news_context: list[dict[str, str]],
) -> ShortPlan:
    current_words = spoken_word_count(plan.narration)
    expand_prompt = f'''Act as a senior long-form documentary script doctor. Return only JSON.
The current plan is too short for a {duration}-second horizontal YouTube video: it has {current_words} words, but it must have {min_words}-{max_words} spoken English words.

Expand ONLY the narration, hook if needed, closing_line if needed, and scene visual prompts if they need to match the expanded chapters. Keep the same topic, title, tags, description, scene count, and scene durations.

Expansion requirements:
- Write natural spoken documentary prose, not bullet points.
- Add context, timeline, explanation, stakes, uncertainty, and likely next consequences.
- Do not invent precise numbers, quotes, dates, casualty figures, scores, or market data not present in the supplied context.
- Do not mention Vietnam or any Vietnam-related person, place, or event.
- Keep the story strictly within politics, military affairs, economics, technology industry, sports, or major world news. Do not add science, climate research, space, medicine, or academic-study material.
- Keep every chapter tied to a concrete current event and an ordinary-viewer consequence; avoid abstract geopolitical or economic theory.
- The narration must begin with hook and end with closing_line.
- Final narration word count must be {min_words}-{max_words}.

News context: {json.dumps(news_context, ensure_ascii=False)}
Plan to expand: {json.dumps(plan.to_dict(), ensure_ascii=False)}

Return exactly:
{{"plan":{LONG_FORM_PLAN_SCHEMA}}}'''
    LOG.info("Long-form script was too short (%d words). Requesting expansion pass...", current_words)
    expanded = extract_json(
        llm.chat(
            expand_prompt,
            temperature=0.45,
            reasoning_effort=getattr(llm, "long_form_reasoning_effort", "medium"),
        )
    )
    if not isinstance(expanded.get("plan"), dict):
        raise BotError("OpenAI long-form expansion pass thieu truong plan.")
    return ShortPlan.from_dict(expanded["plan"])


def plan_long_form(
    llm: OpenAITextClient,
    archive: Archive,
    theme: str,
    duration: int,
    min_scenes: int,
    max_scenes: int,
    news_context: list[dict[str, str]] | None = None,
    rejected: list[dict[str, str]] | None = None,
    editorial_lane: str = "global_headlines",
    covered_subjects: list[str] | None = None,
) -> ShortPlan:
    past = archive.recent_long_form_context()
    rejected = rejected or []
    news_context = news_context or []
    covered_subjects = covered_subjects if covered_subjects is not None else recent_long_form_subject_texts(past)
    lane_label = LONG_FORM_EDITORIAL_LANES[editorial_lane]["label"]
    target_min_words, target_max_words = target_long_form_word_bounds(duration)
    min_words, max_words = long_form_word_bounds(duration)
    scene_count = random.randint(min_scenes, max_scenes)
    target_scene_duration = round(duration / scene_count, 2)
    prompt = f'''Act as a senior YouTube news-digest producer and factual script editor.
Create ONE English long-form news-digest video plan for a horizontal 16:9 video.
Target duration: exactly {duration} seconds, about {duration // 60} to {round(duration / 60, 1)} minutes.
Theme: {theme}
Allowed domains: {", ".join(LONG_FORM_TOPIC_DOMAINS)}.
Assigned editorial lane for this run: {lane_label}.
Fresh news context from public RSS headlines: {json.dumps(news_context, ensure_ascii=False)}
Existing archive: {json.dumps(past, ensure_ascii=False)}
Recently covered subjects (temporary cooldown list): {json.dumps(covered_subjects, ensure_ascii=False)}
Rejected candidates from this run: {json.dumps(rejected, ensure_ascii=False)}

Hard rules:
- Do NOT choose any topic, event, person, location, company, political figure, sports figure, or public controversy related to Vietnam.
- COOLDOWN RULE: Do not choose any story whose central subject, person, country pair, strait, route, company, conflict, policy, or event overlaps the recently covered subjects list above, even with a completely new angle or consequence. The channel must not publish the same subject twice in a row; pick a genuinely different subject.
- Stay inside the assigned editorial lane. Choose from its supplied headlines; do not switch back to a recently covered lane merely because it has a louder headline.
- Shortlist at least three viable headlines with DIFFERENT central subjects from the news context, then pick one of them at random rather than automatically taking the biggest or most dramatic story. Variety across videos matters more than always covering the single loudest headline.
- Use the supplied news context as leads. If the context is thin, choose a globally relevant current topic in the same assigned lane that does not overlap the cooldown list, and explicitly keep claims broad.
- Cover ONLY politics, elections, wars, military affairs, geopolitics, economics, business, trade, markets, consumer technology, cybersecurity, the AI industry, major sports, or major world news. Reject science, climate research, space, medicine, health studies, archaeology, and academic discoveries even if they appear in a top-news feed.
- Do not invent quotes, casualty numbers, market numbers, scores, dates, or source names not present in the context.
- The result must feel timely, clickable, practical, surprising, and broad-interest, but not sensationalized.
- Select a story with a concrete change: who acted, what changed, who pays or benefits, what viewers should watch next, and why it matters now. Reject routine speeches, procedural updates, and abstract policy theory with no visible consequence.
- NOVELTY REQUIREMENT: Do not reuse the same central person, country pair, place, route, company, conflict, policy, event, or sports team from the existing archive or rejected candidates, even with a different consequence or angle. If a candidate shares the same central subject, choose a genuinely different headline.
- TITLE REQUIREMENT: The title must explicitly name the central person, country, place, route, company, conflict, policy, event, or sports team — not only its consequence. Put that concrete subject early when possible. Set thumbnail_text to 2-5 bold words that name the same central subject; never use a vague slogan.
- Use a save/share test: the viewer should finish with at least one clear consequence, comparison, warning sign, or next development they can explain to someone else.
- FORMAT: a {duration // 60}-minute NEWS DIGEST, not a deep-dive documentary. Structure: immediate headline payoff, what just happened, essential background in one short chapter, who is affected, one or two brief analysis beats, what to watch next, quick close. Keep analysis light and concrete; skip extended history lessons, competing-interpretation essays, and theory.
- Narration must be coherent spoken English, not bullet points, and must begin with hook and end with closing_line.
- WORD BUDGET: write roughly {target_min_words}-{target_max_words} spoken English words and treat {target_max_words} as a hard ceiling — the timeline follows the narration audio, so every extra 100 words adds about 40 seconds to the video. Never pad with filler.
- Make {scene_count} scenes totaling exactly {duration} seconds. Most scenes should be about {target_scene_duration} seconds.
- Visuals: {LONG_FORM_VISUAL_STYLE_RULES}
- It is acceptable for later scenes to reuse a visual concept if the narration has moved to a new argument, but still provide a visual_prompt for every scene.

Return raw JSON only using exactly this schema:
{LONG_FORM_PLAN_SCHEMA}'''
    LOG.info("Writing long-form current-events documentary plan...")
    draft = ShortPlan.from_dict(
        extract_json(
            llm.chat(
                prompt,
                temperature=0.55,
                reasoning_effort=getattr(llm, "long_form_reasoning_effort", "medium"),
            )
        )
    )
    review_prompt = f'''Act as the final long-form news, fact, usefulness, and retention editor. Return only JSON.
Improve the draft below for a {duration}-second horizontal YouTube news digest.
Return exactly:
{{"quality_check":{{"timeliness_score":1,"clarity_score":1,"public_relevance_score":1,"shareability_score":1,"surprise_score":1,"factual_risk":"short note","changes":["short note"]}},"plan":{LONG_FORM_PLAN_SCHEMA}}}

Rules:
- Reject or rewrite any Vietnam-related topic, person, event, or location.
- Keep the assigned editorial lane: {lane_label}. Do not replace the draft with a story from another lane.
- Reject a draft that repeats the same central person, country pair, place, route, company, conflict, policy, event, or sports team from the archive, the rejected candidates, or this cooldown list of recently covered subjects: {json.dumps(covered_subjects, ensure_ascii=False)}. A new angle or consequence does not make a repeated subject acceptable.
- Reject science, climate research, space, medicine, health studies, archaeology, and academic discoveries. Keep only politics, military affairs, economics, business, technology industry, sports, or consequential world news.
- Keep only claims supportable by the supplied RSS context or clearly phrased as general background.
- Reject routine announcements or abstract theory unless the script can name the concrete change, affected people, real-world consequence, and what happens next.
- Keep a strong first 20 seconds, then clear chapters with escalation, practical explanation, a surprising but supported payoff, and a reason viewers would save or share the video. The title must explicitly name the central person/place/event/company/route, and thumbnail_text must be 2-5 words that name that same subject rather than a vague teaser.
- The narration must begin with hook and end with closing_line.
- The scenes must total exactly {duration} seconds and be horizontal 16:9 visual prompts.
- Keep the news-digest format: light, concrete analysis only — no extended history or theory chapters.
- Aim for roughly {target_min_words}-{target_max_words} words. If the draft narration exceeds {target_max_words} words, cut secondary detail until it fits; the final video length follows the narration audio directly. Never pad with filler.

News context: {json.dumps(news_context, ensure_ascii=False)}
Existing archive: {json.dumps(past, ensure_ascii=False)}
Rejected candidates from this run: {json.dumps(rejected, ensure_ascii=False)}
Draft: {json.dumps(draft.to_dict(), ensure_ascii=False)}'''
    LOG.info("Quality pass: checking long-form timeliness, structure, and Vietnam exclusion...")
    reviewed = extract_json(
        llm.chat(
            review_prompt,
            temperature=0.35,
            reasoning_effort=getattr(llm, "long_form_reasoning_effort", "medium"),
        )
    )
    if not isinstance(reviewed.get("plan"), dict):
        raise BotError("OpenAI long-form quality pass thieu truong plan.")
    plan = ShortPlan.from_dict(reviewed["plan"])
    ensure_title_names_main_subject(plan)
    ensure_long_form_hook_and_closing(plan)
    normalize_scene_count(plan, scene_count)
    validate_long_form_plan(plan, duration, min_words, max_words, scene_count)
    quality = reviewed.get("quality_check", {})
    LOG.info(
        "Long-form plan ready: %r (%d scenes, %d words, timeliness %s/10).",
        plan.title,
        len(plan.scenes),
        spoken_word_count(plan.narration),
        quality.get("timeliness_score", "?"),
    )
    return plan


def choose_novel_long_form_plan(
    llm: OpenAITextClient,
    archive: Archive,
    theme: str,
    duration: int,
    min_scenes: int,
    max_scenes: int,
    max_attempts: int = 4,
) -> ShortPlan | None:
    rejected: list[dict[str, str]] = []
    all_news_context = fetch_trending_news_context()
    past = archive.recent_long_form_context()
    covered_subjects = recent_long_form_subject_texts(past)
    attempted_lanes: set[str] = set()
    attempts = 0
    while attempts < max_attempts:
        editorial_lane = choose_long_form_editorial_lane(past, attempted_lanes)
        attempted_lanes.add(editorial_lane)
        news_context = fresh_news_context_for_lane(all_news_context, editorial_lane, covered_subjects)
        if (
            not news_context
            and all_news_context
            and len(attempted_lanes) < len(LONG_FORM_EDITORIAL_LANES)
        ):
            LOG.info(
                "No fresh %s headlines left after the recent-subject cooldown; switching lane.",
                editorial_lane,
            )
            continue
        attempts += 1
        plan = plan_long_form(
            llm,
            archive,
            theme,
            duration,
            min_scenes,
            max_scenes,
            news_context,
            rejected,
            editorial_lane,
            covered_subjects,
        )
        duplicate = archive.same_long_form_subject_as(plan)
        if not duplicate:
            duplicate = archive.duplicate_of(plan, threshold=0.45)
        if not duplicate:
            return plan
        rejected.append(rejection_context(plan, duplicate))
        print(
            f"Long-form subject duplicated ({duplicate['title']!r}); "
            "switching editorial lane and requesting a different story..."
        )
    return None


def plan_short_from_idea(llm: OpenAITextClient, duration: int, user_idea: str) -> ShortPlan:
    """Build a Short plan around a user-supplied idea, bypassing the auto novelty/significance gates."""
    target_minimum_words, target_maximum_words = target_narration_word_bounds(duration)
    prompt = f'''Act as a senior viral documentary writer. Create ONE highly watchable {duration}-second English-language YouTube Short plan.
The user has explicitly requested THIS exact idea. Build the entire Short around it and do NOT substitute a different topic:
"""{user_idea}"""

Rules:
- Treat the user's idea as the mandatory subject, angle, and story. Interpret it faithfully even if it is written in another language; the finished narration is in English.
- Structure the story in 3 parts: BEGINNING (establish who / where / when / what), MIDDLE (the decision, reveal, conflict, mechanism, or turning point with concrete detail), ENDING (the meaning, consequence, or memorable payoff).
- Open with a sharp curiosity hook in the first 1.5 seconds. The narration must start verbatim with hook and end verbatim with closing_line.
- Do NOT invent statistics, dates, quotations, casualty numbers, prices, scores, or source names. Build from general knowledge and stay qualitative when a precise figure is unknown; never fabricate precise facts or citations.
- TITLE: explicitly name the concrete main subject of the idea (the person, place, event, object, or work), not a vague pronoun. Set thumbnail_text to 2-5 bold words naming that same subject.
- Split the story into exactly 6 scenes whose durations total exactly {duration}. Aim for roughly {target_minimum_words}-{target_maximum_words} spoken English words; never pad with filler.
- Every string in the returned JSON must be English (topic, title, description, tags, narration, fact_note, source_hints).
- Visuals: {VISUAL_STYLE_RULES}

Return raw JSON only using exactly this schema:
{PLAN_SCHEMA}'''
    LOG.info("Writing manual-idea Short plan from the user's idea...")
    draft = ShortPlan.from_dict(extract_json(llm.chat(prompt, temperature=0.6)))
    review_prompt = f'''Act as the final documentary-story and retention editor for a {duration}-second English YouTube Short. Return only JSON.
Keep the video centered on the user's requested idea and improve hook, clarity, concreteness, pacing, and the closing line. Return exactly:
{{"plan":{PLAN_SCHEMA}}}
Rules:
- The subject MUST stay the user's idea: """{user_idea}""". Do not swap in a different topic.
- Do NOT invent statistics, dates, quotations, numbers, prices, scores, or source names; keep it qualitative when a precise figure is unknown.
- Keep exactly 6 scenes; narration must begin with hook and end with closing_line; aim for {target_minimum_words}-{target_maximum_words} spoken English words.
- Every string must be English. Make the title explicitly name the main subject and thumbnail_text 2-5 words naming it. Preserve this visual direction in every scene: {VISUAL_STYLE_RULES}
Draft: {json.dumps(draft.to_dict(), ensure_ascii=False)}'''
    LOG.info("Quality pass: refining the manual Short hook, pacing, and ending...")
    try:
        reviewed = extract_json(llm.chat(review_prompt, temperature=0.4))
        plan_dict = reviewed.get("plan") if isinstance(reviewed.get("plan"), dict) else reviewed
        plan = ShortPlan.from_dict(plan_dict)
    except Exception as exc:
        LOG.warning("Manual Short review pass failed (%s); using the draft.", exc)
        plan = draft
    ensure_title_names_main_subject(plan)
    normalize_scene_count(plan, 6)
    ensure_long_form_hook_and_closing(plan)
    rescale_scene_durations(plan, float(duration), "Manual Short plan")
    words = spoken_word_count(plan.narration)
    minimum_words, maximum_words = narration_word_bounds(duration)
    if not minimum_words <= words <= maximum_words:
        LOG.warning(
            "Manual Short narration has %d words (comfortable range %d-%d for %ds); rendering it anyway.",
            words, minimum_words, maximum_words, duration,
        )
    LOG.info("Manual Short plan ready: %r (%d scenes, %d words).", plan.title, len(plan.scenes), words)
    return plan


def plan_long_form_from_idea(
    llm: OpenAITextClient,
    duration: int,
    min_scenes: int,
    max_scenes: int,
    user_idea: str,
) -> ShortPlan:
    """Build a long-form plan around a user-supplied idea, bypassing lane/novelty/Vietnam gates."""
    target_min_words, target_max_words = target_long_form_word_bounds(duration)
    min_words, max_words = long_form_word_bounds(duration)
    scene_count = random.randint(min_scenes, max_scenes)
    target_scene_duration = round(duration / scene_count, 2)
    news_context = fetch_news_for_idea(user_idea)
    manual_visual_rules = (
        "Write each visual_prompt as one concrete, self-contained sentence for a horizontal 16:9 documentary image. "
        "Use broadcast documentary variety: wide establishing shots, maps without readable labels, symbolic still lifes, "
        "infrastructure details, screens without legible text, satellite-like views, and contextual crowd-free scenes. "
        "In visual_prompt avoid readable text, logos, graphic injury, close-up human faces, dense crowds, and names of real "
        "people (the image generator rejects real public figures). "
        "Separately, give every scene a search_query: a short literal photo-search phrase that DOES name the real person, "
        "place, organization, or object of that beat, so the pipeline can fetch a real licensed photo first. Scene 1's "
        "search_query must name the story's central person or place, because scene 1 becomes the thumbnail."
    )
    prompt = f'''Act as a senior YouTube long-form documentary writer and factual script editor.
Create ONE English long-form video plan for a horizontal 16:9 video.
Target duration: exactly {duration} seconds, about {duration // 60} to {round(duration / 60, 1)} minutes.
The user has explicitly requested THIS exact idea. Build the entire video around it and do NOT substitute a different topic:
"""{user_idea}"""
Fresh related news headlines from public RSS (use as factual leads when they fit the idea; ignore unrelated ones): {json.dumps(news_context, ensure_ascii=False)}

Rules:
- Treat the user's idea as the mandatory subject and thesis. Interpret it faithfully even if it is written in another language; the finished narration is in English.
- Prefer concrete, current facts drawn from the related news above when they fit the idea, but do not copy unrelated stories and do not fabricate details beyond what the leads support.
- Structure: a strong hook, clear chapters that escalate, concrete explanation, and a memorable close. The narration must begin verbatim with hook and end verbatim with closing_line.
- Do NOT invent statistics, dates, quotations, casualty numbers, market data, scores, or source names. Build from general knowledge and stay qualitative when a precise figure is unknown; never fabricate precise facts or citations.
- TITLE: explicitly name the central person, place, event, company, route, or work of the idea. Set thumbnail_text to 2-5 bold words naming that same subject.
- WORD BUDGET: write roughly {target_min_words}-{target_max_words} spoken English words. The timeline follows the narration audio, so do not pad with filler.
- Make {scene_count} scenes totaling exactly {duration} seconds. Most scenes should be about {target_scene_duration} seconds.
- Visuals: {manual_visual_rules}
- It is acceptable for later scenes to reuse a visual concept, but still provide a visual_prompt for every scene.

Return raw JSON only using exactly this schema:
{LONG_FORM_PLAN_SCHEMA}'''
    LOG.info("Writing manual-idea long-form plan from the user's idea...")
    draft = ShortPlan.from_dict(
        extract_json(
            llm.chat(
                prompt,
                temperature=0.5,
                reasoning_effort=getattr(llm, "long_form_reasoning_effort", "medium"),
            )
        )
    )
    review_prompt = f'''Act as the final long-form documentary and retention editor for a {duration}-second horizontal YouTube video. Return only JSON.
Keep the video centered on the user's requested idea and improve the hook, chapter flow, concreteness, and closing line. Return exactly:
{{"plan":{LONG_FORM_PLAN_SCHEMA}}}
Rules:
- The subject MUST stay the user's idea: """{user_idea}""". Do not swap in a different topic.
- Keep only claims supportable by the related news below or clearly phrased as general background; do NOT invent numbers, quotes, dates, or source names.
- Keep {scene_count} scenes totaling {duration} seconds; narration must begin with hook and end with closing_line; aim for {target_min_words}-{target_max_words} spoken English words.
- Make the title explicitly name the main subject and thumbnail_text 2-5 words naming it. Preserve this visual direction: {manual_visual_rules}
Related news: {json.dumps(news_context, ensure_ascii=False)}
Draft: {json.dumps(draft.to_dict(), ensure_ascii=False)}'''
    LOG.info("Quality pass: refining the manual long-form structure and pacing...")
    try:
        reviewed = extract_json(
            llm.chat(
                review_prompt,
                temperature=0.35,
                reasoning_effort=getattr(llm, "long_form_reasoning_effort", "medium"),
            )
        )
        plan_dict = reviewed.get("plan") if isinstance(reviewed.get("plan"), dict) else reviewed
        plan = ShortPlan.from_dict(plan_dict)
    except Exception as exc:
        LOG.warning("Manual long-form review pass failed (%s); using the draft.", exc)
        plan = draft
    ensure_title_names_main_subject(plan)
    ensure_long_form_hook_and_closing(plan)
    normalize_scene_count(plan, scene_count)
    rescale_scene_durations(plan, float(duration), "Manual long-form plan")
    words = spoken_word_count(plan.narration)
    if not min_words <= words <= max_words:
        LOG.warning(
            "Manual long-form narration has %d words (safety range %d-%d for %ds); rendering it anyway.",
            words, min_words, max_words, duration,
        )
    LOG.info("Manual long-form plan ready: %r (%d scenes, %d words).", plan.title, len(plan.scenes), words)
    return plan


def plan_social_vietnamese(llm: OpenAITextClient, plan: ShortPlan, duration: int) -> SocialPlan:
    target_minimum_words, target_maximum_words = target_narration_word_bounds(duration)
    minimum_words, maximum_words = narration_word_bounds(duration)
    schema = (
        '{"title":"Vietnamese title, <=100 characters","description":"Vietnamese caption with exactly 2 relevant hashtags",'
        '"tags":["exactly 2 short hashtags"],"narration":"Vietnamese voice-over"}'
    )
    prompt = f'''Translate and adapt this English YouTube Short plan into Vietnamese for Facebook and TikTok.
    Return raw JSON only with this schema:
{schema}
Rules:
- Keep every factual claim equivalent to the English plan; do not add dates, names, statistics, sources, or certainty.
- Make the Vietnamese narration natural, concise, and suitable for a {duration}-second short video.
- Aim for roughly {target_minimum_words}-{target_maximum_words} Vietnamese words, but prioritize natural Vietnamese over padding.
- The caption should disclose that the video is AI-assisted when appropriate.
English plan: {json.dumps(plan.to_dict(), ensure_ascii=False)}'''
    LOG.info("Creating Vietnamese social caption and narration…")
    social = SocialPlan.from_dict(extract_json(llm.chat(prompt, temperature=0.35)))
    words = len(re.findall(r"\b\w+\b", social.narration, flags=re.UNICODE))
    if words > target_maximum_words or not minimum_words <= words <= maximum_words:
        LOG.info(
            "Vietnamese social narration has %d words; requesting a concise repair for %d-%d words.",
            words,
            minimum_words,
            target_maximum_words,
        )
        repair_prompt = f'''Rewrite this Vietnamese Facebook/TikTok social plan into a natural voice-over for a {duration}-second video.
Return raw JSON only with this schema:
{schema}
Rules:
- Preserve every factual claim, named subject, and the original meaning. Do not add facts, dates, numbers, sources, or certainty.
- Keep the title and caption natural Vietnamese; the caption must have exactly 2 relevant hashtags.
- The narration must contain {minimum_words}-{target_maximum_words} Vietnamese words. Remove repetition and filler first; keep the hook, core explanation, and ending.

Plan to rewrite: {json.dumps(social.to_dict(), ensure_ascii=False)}'''
        social = SocialPlan.from_dict(extract_json(llm.chat(repair_prompt, temperature=0.2)))
        words = len(re.findall(r"\b\w+\b", social.narration, flags=re.UNICODE))
    if not minimum_words <= words <= maximum_words or words > target_maximum_words:
        raise BotError(f"Kịch bản tiếng Việt có {words} từ, ngoài khoảng phù hợp cho Short {duration}s.")
    LOG.info("Vietnamese social plan ready: %r (%d words)", social.title, words)
    return social


def rejection_context(plan: ShortPlan, duplicate: sqlite3.Row) -> dict[str, str]:
    return {
        "topic": plan.topic,
        "angle": plan.angle,
        "title": plan.title,
        "matched_existing_topic": str(duplicate["topic"]),
        "matched_existing_angle": str(duplicate["angle"]),
        "matched_existing_title": str(duplicate["title"]),
    }


def short_editorial_rejection_reason(plan: ShortPlan) -> str | None:
    """Reject abstract or low-significance Shorts before image credits are spent."""
    text = " ".join((plan.topic, plan.angle, plan.title, plan.hook, plan.narration)).lower()
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    matched = [term for term in ABSTRACT_SHORT_TOPIC_TERMS if term in normalized]
    if matched:
        return f"abstract or theory-led framing ({', '.join(matched[:3])})"
    minor_matches = [term for term in MINOR_SHORT_STORY_TERMS if term in normalized]
    if minor_matches:
        return f"minor or local incident framing ({', '.join(minor_matches[:3])})"
    significance_scores = {
        "subject stature": plan.subject_stature_score,
        "historical significance": plan.historical_significance_score,
        "broad learning value": plan.broad_learning_value_score,
    }
    supplied_scores = [score for score in significance_scores.values() if score > 0]
    if supplied_scores:
        weak = [name for name, score in significance_scores.items() if score < 8]
        if weak:
            return f"insufficient documentary significance ({', '.join(weak)})"
    return None


def choose_novel_plan(
    llm: OpenAITextClient,
    archive: Archive,
    theme: str,
    duration: int,
    max_attempts: int = 4,
) -> ShortPlan | None:
    rejected: list[dict[str, str]] = []
    for _attempt in range(1, max_attempts + 1):
        plan = plan_short(llm, archive, theme, duration, rejected)
        editorial_reason = short_editorial_rejection_reason(plan)
        if editorial_reason:
            rejected.append({
                "topic": plan.topic,
                "angle": plan.angle,
                "title": plan.title,
                "editorial_rejection": editorial_reason,
            })
            print(
                f"Short idea rejected by editorial gate ({plan.title!r}: {editorial_reason}); "
                "requesting a larger, more consequential historical subject..."
            )
            continue
        duplicate = archive.duplicate_of(plan)
        if not duplicate:
            return plan
        rejected.append(rejection_context(plan, duplicate))
        print(f"Ý tưởng trùng ({duplicate['title']!r}); yêu cầu OpenAI tạo góc khác…")
    return None


def image_scene_prompt(visual_prompt: str) -> str:
    # Keep the generation prompt concise; scene exclusions are enforced by the storyboard instructions.
    return f"{visual_prompt.strip().rstrip('.')}. {IMAGE_STYLE_SUFFIX}"


def spoken_word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text, flags=re.UNICODE))


def caption_chunks(text: str, min_words: int = 3, max_words: int = 6) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return []
    chunks: list[str] = []
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+", cleaned) if part.strip()]
    for sentence in sentences:
        current: list[str] = []
        for token in sentence.split():
            current.append(token)
            words = spoken_word_count(" ".join(current))
            punctuation_pause = token.rstrip().endswith((",", ";", ":", "—", "–"))
            if words >= max_words or (words >= min_words and punctuation_pause):
                chunks.append(" ".join(current).strip())
                current = []
        if current:
            chunks.append(" ".join(current).strip())

    merged: list[str] = []
    for chunk in chunks:
        if (
            merged
            and spoken_word_count(chunk) < min_words
            and spoken_word_count(merged[-1]) + spoken_word_count(chunk) <= max_words + 1
        ):
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)
    return merged


def caption_cues_from_text(text: str, timeline_duration: float) -> list[CaptionCue]:
    chunks = caption_chunks(text)
    if not chunks or timeline_duration <= 0:
        return []
    weights = [max(1, spoken_word_count(chunk)) for chunk in chunks]
    total_weight = sum(weights)
    cursor = 0.0
    cues: list[CaptionCue] = []
    for index, (chunk, weight) in enumerate(zip(chunks, weights, strict=True)):
        end = timeline_duration if index == len(chunks) - 1 else cursor + timeline_duration * (weight / total_weight)
        cue_end = end if index == len(chunks) - 1 else max(cursor + 0.25, end - 0.04)
        cues.append(CaptionCue(start=round(cursor, 2), end=round(min(cue_end, timeline_duration), 2), text=chunk))
        cursor = end
    return cues


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    total_seconds, cs = divmod(centiseconds, 100)
    minutes, sec = divmod(total_seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02d}:{sec:02d}.{cs:02d}"


def ass_escape_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def caption_ass_text(text: str, max_line_chars: int = 24) -> str:
    words = text.split()
    if len(text) <= max_line_chars or len(words) < 4:
        return ass_escape_text(text)
    best_split = 1
    best_score = float("inf")
    for split in range(1, len(words)):
        left, right = " ".join(words[:split]), " ".join(words[split:])
        score = max(len(left), len(right)) + abs(len(left) - len(right)) * 0.25
        if score < best_score:
            best_split, best_score = split, score
    return "\\N".join(
        ass_escape_text(part)
        for part in (" ".join(words[:best_split]), " ".join(words[best_split:]))
    )


def write_ass_captions(
    cues: list[CaptionCue],
    destination: Path,
    play_res_x: int = 1080,
    play_res_y: int = 1920,
    font_size: int = 74,
    margin_v: int = 275,
) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H66000000,-1,0,0,0,100,100,0,0,1,6,1,2,120,120,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for cue in cues:
        lines.append(
            "Dialogue: 0,"
            f"{ass_time(cue.start)},{ass_time(cue.end)},Caption,,0,0,0,,"
            f"{{\\fad(80,80)}}{caption_ass_text(cue.text)}\n"
        )
    destination.write_text("".join(lines), encoding="utf-8")


def ffmpeg_filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


YOUTUBE_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024


def thumbnail_headline(plan: ShortPlan) -> str:
    """Return a concise, concrete subject label for a YouTube thumbnail."""
    raw = plan.thumbnail_text or plan.topic or plan.title
    clean = re.sub(r"\s+", " ", raw).strip()
    words = clean.split()
    # The writer normally returns 2-5 words. The fallback keeps old plans and
    # malformed model output readable without hiding the named subject.
    return " ".join(words[:6]).upper() or "DOCUMENTARY"


def thumbnail_headline_lines(text: str, max_line_chars: int = 22) -> str:
    """Wrap thumbnail copy into at most two large, readable lines."""
    words = text.split()
    if not words:
        return "DOCUMENTARY"
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_line_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) <= 2:
        return "\n".join(lines)
    return f"{lines[0]}\n{lines[1]}…"


def create_long_form_thumbnail(plan: ShortPlan, video: Path, output_dir: Path, settings: Settings) -> Path:
    """Build a 16:9 custom thumbnail from an existing hook visual, without a new image API call."""
    destination = output_dir / "thumbnail.jpg"
    if destination.is_file() and 1024 <= destination.stat().st_size <= YOUTUBE_THUMBNAIL_MAX_BYTES:
        LOG.info("Reusing prepared long-form YouTube thumbnail: %s", destination.name)
        return destination

    source_scene = long_form_image_path(output_dir, 1)
    source = source_scene if source_scene.is_file() and source_scene.stat().st_size >= 1024 else video
    if not source.is_file():
        raise BotError(f"Không tìm thấy visual để tạo thumbnail long-form: {source}")
    font_file = DATA_DIR / "fonts" / "DejaVuSans.ttf"
    if not font_file.is_file():
        raise BotError(f"Không tìm thấy font để tạo thumbnail: {font_file}")

    headline_file = output_dir / "thumbnail_headline.txt"
    headline_file.write_text(thumbnail_headline_lines(thumbnail_headline(plan)), encoding="utf-8")
    visual_filter = (
        "scale=1280:720:force_original_aspect_ratio=increase,"
        "crop=1280:720,eq=contrast=1.08:saturation=1.12,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.14:t=fill,"
        "drawbox=x=0:y=390:w=iw:h=330:color=black@0.70:t=fill,"
        f"drawtext=fontfile='{ffmpeg_filter_path(font_file)}':"
        f"textfile='{ffmpeg_filter_path(headline_file)}':"
        "fontcolor=white:fontsize=74:line_spacing=12:borderw=5:bordercolor=black@0.9:"
        "shadowcolor=black@0.9:shadowx=4:shadowy=4:x=64:y=h-text_h-66:fix_bounds=1"
    )
    command = ["ffmpeg", "-y", "-i", str(source)]
    logo = settings.overlay_logo
    if logo.is_file():
        command.extend([
            "-loop", "1", "-i", str(logo),
            "-filter_complex",
            f"[0:v]{visual_filter}[base];"
            "[1:v]scale=130:-1:flags=lanczos,format=rgba[logo];"
            "[base][logo]overlay=x=W-w-42:y=42:format=auto[v]",
            "-map", "[v]",
        ])
    else:
        command.extend(["-vf", visual_filter])
    command.extend(["-frames:v", "1", "-q:v", "4", str(destination)])
    require_tools()
    LOG.info("Creating 16:9 long-form YouTube thumbnail from %s (no new image credit)…", source.name)
    run(command)
    if not destination.is_file() or destination.stat().st_size < 1024:
        raise BotError("Thumbnail long-form không tạo được ảnh JPEG hợp lệ.")
    if destination.stat().st_size > YOUTUBE_THUMBNAIL_MAX_BYTES:
        raise BotError("Thumbnail long-form vượt giới hạn 2 MB của YouTube API.")
    LOG.info("Created long-form YouTube thumbnail: %s (%.1f MB)", destination.name, destination.stat().st_size / (1024 * 1024))
    return destination


def ass_video_filter(captions: Path) -> str:
    return f"ass='{ffmpeg_filter_path(captions)}',format=yuv420p"


def mux_video_audio_with_captions(
    visuals: Path,
    narration: Path,
    captions: Path,
    output: Path,
    target_duration: float,
    settings: Settings,
    long_form: bool = False,
    timeout_seconds: int | None = None,
) -> None:
    logo = settings.overlay_logo
    if not logo.is_file():
        raise BotError(f"Không tìm thấy logo overlay: {logo}")
    logo_width = (
        settings.overlay_logo_long_form_width
        if long_form
        else settings.overlay_logo_short_width
    )
    margin = settings.overlay_logo_margin
    top_margin = (
        settings.overlay_logo_long_form_top_margin
        if long_form
        else settings.overlay_logo_short_top_margin
    )
    video_filter = (
        f"[0:v]{ass_video_filter(captions)}[base];"
        f"[2:v]scale={logo_width}:-1:flags=lanczos,format=rgba[logo];"
        f"[base][logo]overlay=x=W-w-{margin}:y={top_margin}:format=auto,format=yuv420p[v];"
        f"[1:a]apad=pad_dur={target_duration}[a]"
    )
    command = [
        "ffmpeg", "-y",
        "-i", str(visuals),
        "-i", str(narration),
        "-loop", "1", "-i", str(logo),
        "-filter_complex", video_filter,
        "-map", "[v]",
        "-map", "[a]",
        "-t", str(target_duration),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(output),
    ]
    if timeout_seconds is None:
        run(command)
    else:
        run(command, timeout_seconds=timeout_seconds)


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise BotError("Không tìm thấy trong PATH: " + ", ".join(missing))


def run(command: list[str], timeout_seconds: int | None = None) -> None:
    LOG.debug("Running command: %s", " ".join(command))
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_label = f" after {timeout_seconds}s" if timeout_seconds else ""
        LOG.error("FFmpeg timed out%s: %s", timeout_label, " ".join(command))
        raise BotError(f"FFmpeg quá thời gian cho phép{timeout_label}: {' '.join(command)}") from exc
    if result.returncode:
        tail = result.stderr[-3000:] or result.stdout[-3000:] or "(no ffmpeg output captured)"
        LOG.error("FFmpeg exited with code %s: %s", result.returncode, " ".join(command))
        LOG.error("FFmpeg output tail: %s", tail)
        raise BotError(f"FFmpeg lỗi: {' '.join(command)}\n{result.stderr[-1500:]}")


def create_fallback_scene_image(
    destination: Path,
    previous_image: Path | None,
    width: int = 1080,
    height: int = 1920,
) -> None:
    if previous_image and previous_image.is_file() and previous_image.stat().st_size >= 1024:
        shutil.copyfile(previous_image, destination)
        LOG.warning("Reused previous scene image for %s after image generation failed.", destination.name)
        return

    LOG.warning("Creating a neutral fallback image for %s after image generation failed.", destination.name)
    run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x101820:s={width}x{height}",
        "-frames:v", "1",
        "-q:v", "2",
        str(destination),
    ])


def media_duration(path: Path) -> float:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], text=True, capture_output=True)
    if result.returncode:
        raise BotError(f"Không đọc được thời lượng {path.name}")
    return float(result.stdout.strip())


def split_text_for_tts(text: str, max_chars: int = 3800) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text.strip()]


def synthesize_narration(
    tts: GoogleCloudTTS,
    text: str,
    destination: Path,
    output_dir: Path,
    prefix: str = "narration_part",
) -> None:
    chunks = split_text_for_tts(text)
    if len(chunks) == 1:
        tts.speech(text, destination)
        return
    parts: list[Path] = []
    for index, chunk in enumerate(chunks, start=1):
        part = output_dir / f"{prefix}_{index:02d}.mp3"
        LOG.info("Generating narration chunk %d/%d...", index, len(chunks))
        tts.speech(chunk, part)
        parts.append(part)
    concat = output_dir / f"{prefix}_concat.txt"
    concat.write_text("".join(f"file '{part.resolve().as_posix()}'\n" for part in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(destination)])


def measured_narration_duration(narration: Path, label: str) -> float:
    """Use the generated audio as the source of truth for the visual timeline."""
    narration_seconds = media_duration(narration)
    if narration_seconds <= 0:
        raise BotError(f"{label} narration has no audible duration.")
    return round(narration_seconds, 3)


def rescale_scene_durations(plan: ShortPlan, target_duration: float, label: str) -> float:
    """Preserve scene proportions while making the video exactly match its audio."""
    original_duration = sum(scene.duration for scene in plan.scenes)
    if original_duration <= 0 or not plan.scenes:
        raise BotError(f"{label} has no valid scene duration to rescale.")
    scale = target_duration / original_duration
    running_total = 0.0
    for scene in plan.scenes[:-1]:
        scene.duration = round(scene.duration * scale, 3)
        running_total += scene.duration
    plan.scenes[-1].duration = round(target_duration - running_total, 3)
    LOG.info(
        "%s timeline follows %.3fs of narration (was %.3fs; scale %.3f).",
        label,
        target_duration,
        original_duration,
        scale,
    )
    return target_duration


def prepare_short_english_narration(
    plan: ShortPlan,
    tts: GoogleCloudTTS,
    output_dir: Path,
) -> tuple[Path, float]:
    narration = output_dir / "narration.mp3"
    LOG.info("Preflighting English narration before generating any Short images…")
    tts.speech(plan.narration, narration)
    return narration, measured_narration_duration(narration, "Short English")


def prepare_long_form_narration(
    plan: ShortPlan,
    tts: GoogleCloudTTS,
    output_dir: Path,
) -> tuple[Path, float]:
    narration = output_dir / "long_narration.mp3"
    if narration.is_file() and narration.stat().st_size >= 1024:
        narration_seconds = measured_narration_duration(narration, "Long-form")
        LOG.info("Reusing existing long-form English narration at %.3fs; no new TTS request.", narration_seconds)
        return narration, narration_seconds
    LOG.info("Preflighting long-form English narration before generating any images…")
    synthesize_narration(tts, plan.narration, narration, output_dir, prefix="long_narration_part")
    return narration, measured_narration_duration(narration, "Long-form")


def distributed_web_image_indexes(scene_count: int, requested: int) -> set[int]:
    count = min(scene_count, max(0, requested))
    if count == 0:
        return set()
    if count == scene_count:
        return set(range(1, scene_count + 1))
    return {max(1, min(scene_count, round((index + 1) * (scene_count + 1) / (count + 1)))) for index in range(count)}


def append_web_source_credits(plan: ShortPlan, sources: list[dict[str, str]]) -> None:
    source_pages: list[str] = []
    for source in sources:
        page = source.get("source_page", "").strip()
        if page.startswith(("https://", "http://")) and page not in source_pages:
            source_pages.append(page)
    if not source_pages:
        return
    credit = "\n\nVisual sources discovered via Brave Search:\n" + "\n".join(f"- {url}" for url in source_pages)
    plan.description = (plan.description + credit)[:5000]


def render(
    plan: ShortPlan,
    client: VisualAssetProvider,
    output_dir: Path,
    target_duration: float,
    narration: Path,
    narration_seconds: float,
) -> Path:
    require_tools()
    LOG.info("Rendering %d scenes into %s", len(plan.scenes), output_dir)
    clips: list[Path] = []
    previous_image: Path | None = None
    web_indexes = distributed_web_image_indexes(len(plan.scenes), client.s.brave_web_images_per_short)
    for index, scene in enumerate(plan.scenes, start=1):
        image_file = output_dir / f"scene_{index}.jpg"
        clip = output_dir / f"scene_{index}.mp4"
        if image_file.is_file() and image_file.stat().st_size >= 1024:
            LOG.info("Reusing existing Short image %d; no new image credit spent.", index)
        else:
            try:
                client.image(
                    search_query=scene.visual_prompt,
                    generation_prompt=image_scene_prompt(scene.visual_prompt),
                    destination=image_file,
                    width=1080,
                    height=1920,
                    prefer_web=index in web_indexes,
                )
            except (ImageGenerationTransientError, ImageGenerationSafetyError) as exc:
                if not client.s.allow_image_fallback_placeholder and not isinstance(exc, ImageGenerationSafetyError):
                    raise BotError(
                        f"Image generation for scene {index} failed and placeholder fallback is disabled: {exc}"
                    ) from exc
                LOG.warning(
                    "Image generation for scene %d %s; using fallback image: %s",
                    index,
                    "was safety-blocked" if isinstance(exc, ImageGenerationSafetyError) else "failed",
                    exc,
                )
                create_fallback_scene_image(image_file, previous_image)
        if image_file.stat().st_size < 1024:
            raise BotError(f"Cảnh {index} không phải hình ảnh hợp lệ.")

        if short_form_clip_ready(clip, scene.duration):
            LOG.info(
                "Reusing rendered Short scene %d/%d (%.1fs); no FFmpeg re-render.",
                index,
                len(plan.scenes),
                scene.duration,
            )
            previous_image = image_file
            clips.append(clip)
            continue

        frames = int(scene.duration * 30)
        zoom_filter = (
            f"scale={SHORT_FORM_INTERMEDIATE_WIDTH}:-1,"
            f"zoompan=z='min(zoom+0.001,1.5)':d={frames}:"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920,fps=30"
        )
        command = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(image_file),
            "-t", str(scene.duration),
            "-vf", zoom_filter,
            "-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-pix_fmt", "yuv420p",
            str(clip)
        ]
        started = time.monotonic()
        LOG.info(
            "Rendering Short scene %d/%d (%.1fs, timeout %ds)...",
            index,
            len(plan.scenes),
            scene.duration,
            SHORT_FORM_SCENE_RENDER_TIMEOUT_SECONDS,
        )
        try:
            run(command, timeout_seconds=SHORT_FORM_SCENE_RENDER_TIMEOUT_SECONDS)
        except BotError as exc:
            LOG.warning(
                "Short scene %d could not render from %s (%s). Reusing the preceding visual; no image API call.",
                index,
                image_file.name,
                exc,
            )
            # For the first scene this creates a neutral vertical image. For all
            # later scenes it copies the preceding image, including a Brave file
            # that has already rendered successfully.
            create_fallback_scene_image(image_file, previous_image)
            started = time.monotonic()
            run(command, timeout_seconds=SHORT_FORM_SCENE_RENDER_TIMEOUT_SECONDS)

        clip_seconds = media_duration(clip)
        LOG.info(
            "Short scene %d/%d rendered in %.1fs: %.2fs, %.1f MB",
            index,
            len(plan.scenes),
            time.monotonic() - started,
            clip_seconds,
            clip.stat().st_size / (1024 * 1024),
        )
        previous_image = image_file
        clips.append(clip)

    concat = output_dir / "clips.txt"
    concat.write_text("".join(f"file '{clip.resolve().as_posix()}'\n" for clip in clips), encoding="utf-8")
    visuals = output_dir / "visuals.mp4"
    # Re-encode each output so APIs returning different codecs/FPS still concatenate correctly.
    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,fps=30,"
        f"tpad=stop_mode=clone:stop_duration={target_duration},"
        f"trim=duration={target_duration},setpts=PTS-STARTPTS,format=yuv420p,"
        "unsharp=5:5:1.0:5:5:0.0"
    )
    LOG.info("Concatenating and normalizing the vertical video…")
    run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-an", "-vf", video_filter, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(visuals)],
        timeout_seconds=SHORT_FORM_FINAL_RENDER_TIMEOUT_SECONDS,
    )

    captions = output_dir / "captions_en.ass"
    caption_seconds = min(narration_seconds, target_duration)
    cues = caption_cues_from_text(plan.narration, caption_seconds)
    write_ass_captions(cues, captions)
    LOG.info("Generated %d English caption cues synced to %.2fs narration.", len(cues), caption_seconds)
    final_video = output_dir / "short.mp4"
    LOG.info("Muxing narration, captions, and final video…")
    mux_video_audio_with_captions(
        visuals,
        narration,
        captions,
        final_video,
        target_duration,
        client.s,
        timeout_seconds=SHORT_FORM_FINAL_RENDER_TIMEOUT_SECONDS,
    )
    return final_video


def long_form_image_path(output_dir: Path, index: int) -> Path:
    return output_dir / f"long_scene_{index:02d}.jpg"


def long_form_assets_ready(plan: ShortPlan, output_dir: Path) -> bool:
    return all(long_form_image_path(output_dir, index).is_file() for index in range(1, len(plan.scenes) + 1))


def short_form_clip_ready(clip: Path, expected_duration: float) -> bool:
    if not clip.is_file() or clip.stat().st_size < 1024:
        return False
    try:
        return abs(media_duration(clip) - expected_duration) <= 0.75
    except BotError:
        return False


def long_form_clip_ready(clip: Path, expected_duration: float) -> bool:
    if not clip.is_file() or clip.stat().st_size < 1024:
        return False
    try:
        return abs(media_duration(clip) - expected_duration) <= 0.75
    except BotError:
        return False


def prepare_long_form_images(plan: ShortPlan, client: VisualAssetProvider, output_dir: Path) -> int:
    """Fill every scene image: real web photos first, then OpenAI within budget.

    Pass 1 tries Brave for every scene that has a real-subject search_query
    (plus the evenly spaced legacy web slots), so real newsmakers appear as
    themselves. Pass 2 generates the remaining scenes with OpenAI, spreading
    the image budget across the video so any reused frames never cluster.
    """
    prepared = 0
    web_indexes = distributed_web_image_indexes(len(plan.scenes), client.s.brave_web_images_per_long_form)

    def image_ready(path: Path) -> bool:
        return path.is_file() and path.stat().st_size >= 1024

    for index, scene in enumerate(plan.scenes, start=1):
        image_file = long_form_image_path(output_dir, index)
        if image_ready(image_file):
            continue
        if not scene.search_query and index not in web_indexes:
            continue
        source = client.image(
            search_query=scene.search_query or scene.visual_prompt,
            generation_prompt=image_scene_prompt_horizontal(scene.visual_prompt),
            destination=image_file,
            width=1920,
            height=1080,
            prefer_web=True,
            web_only=True,
        )
        if source == "web":
            prepared += 1

    missing = [
        index for index in range(1, len(plan.scenes) + 1)
        if not image_ready(long_form_image_path(output_dir, index))
    ]
    openai_budget = max(0, client.s.long_form_openai_images)
    if len(missing) <= openai_budget:
        openai_indexes = set(missing)
    else:
        chosen_positions = distributed_web_image_indexes(len(missing), openai_budget)
        openai_indexes = {missing[position - 1] for position in chosen_positions}

    previous_image: Path | None = None
    for index, scene in enumerate(plan.scenes, start=1):
        image_file = long_form_image_path(output_dir, index)
        if image_ready(image_file):
            previous_image = image_file
            continue
        if index not in openai_indexes:
            LOG.warning(
                "Long-form image %d has no web result and no OpenAI budget left; reusing the previous visual.",
                index,
            )
            create_fallback_scene_image(image_file, previous_image, width=1920, height=1080)
            if image_file.stat().st_size < 1024:
                raise BotError(f"Long-form scene {index} did not produce a valid image.")
            previous_image = image_file
            prepared += 1
            continue
        try:
            client.image(
                search_query=scene.search_query or scene.visual_prompt,
                generation_prompt=image_scene_prompt_horizontal(scene.visual_prompt),
                destination=image_file,
                width=1920,
                height=1080,
                prefer_web=False,
            )
        except (ImageGenerationTransientError, ImageGenerationSafetyError) as exc:
            if not client.s.allow_image_fallback_placeholder and not isinstance(exc, ImageGenerationSafetyError):
                raise BotError(
                    f"Long-form image {index} failed and placeholder fallback is disabled: {exc}"
                ) from exc
            LOG.warning(
                "Long-form image %d %s; using fallback image: %s",
                index,
                "was safety-blocked" if isinstance(exc, ImageGenerationSafetyError) else "failed",
                exc,
            )
            create_fallback_scene_image(image_file, previous_image, width=1920, height=1080)
        if image_file.stat().st_size < 1024:
            raise BotError(f"Long-form scene {index} did not produce a valid image.")
        previous_image = image_file
        prepared += 1
    LOG.info("Prepared all %d long-form image(s) in this run.", prepared)
    return prepared


def render_long_form_from_assets(
    plan: ShortPlan,
    output_dir: Path,
    target_duration: float,
    settings: Settings,
    narration: Path,
    narration_seconds: float,
) -> Path:
    require_tools()
    if not long_form_assets_ready(plan, output_dir):
        raise BotError("Long-form images are incomplete; the one-shot run cannot continue.")
    LOG.info("Rendering horizontal long-form video with %d scenes...", len(plan.scenes))
    clips: list[Path] = []
    previous_image: Path | None = None
    for index, scene in enumerate(plan.scenes, start=1):
        image_file = long_form_image_path(output_dir, index)
        clip = output_dir / f"long_scene_{index:02d}.mp4"
        if long_form_clip_ready(clip, scene.duration):
            LOG.info(
                "Reusing rendered long-form scene %d/%d (%.1fs); no FFmpeg re-render.",
                index,
                len(plan.scenes),
                scene.duration,
            )
            previous_image = image_file
            clips.append(clip)
            continue
        frames = int(scene.duration * 30)
        zoom_filter = (
            f"scale={LONG_FORM_INTERMEDIATE_WIDTH}:-1,"
            f"zoompan=z='min(zoom+0.00045,1.18)':d={frames}:"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080,fps=30"
        )
        started = time.monotonic()
        LOG.info(
            "Rendering long-form scene %d/%d (%.1fs, timeout %ds)...",
            index,
            len(plan.scenes),
            scene.duration,
            LONG_FORM_SCENE_RENDER_TIMEOUT_SECONDS,
        )
        command = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(image_file),
            "-t", str(scene.duration),
            "-vf", zoom_filter,
            "-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-pix_fmt", "yuv420p",
            str(clip),
        ]
        try:
            run(command, timeout_seconds=LONG_FORM_SCENE_RENDER_TIMEOUT_SECONDS)
        except BotError as exc:
            if previous_image is None or not previous_image.is_file():
                raise
            LOG.warning(
                "Long-form scene %d could not render from %s (%s). Reusing the preceding visual; no image API call.",
                index,
                image_file.name,
                exc,
            )
            create_fallback_scene_image(image_file, previous_image, width=1920, height=1080)
            started = time.monotonic()
            run(command, timeout_seconds=LONG_FORM_SCENE_RENDER_TIMEOUT_SECONDS)
        LOG.info(
            "Long-form scene %d/%d rendered in %.1fs (%.1f MB).",
            index,
            len(plan.scenes),
            time.monotonic() - started,
            clip.stat().st_size / (1024 * 1024),
        )
        previous_image = image_file
        clips.append(clip)

    concat = output_dir / "long_clips.txt"
    concat.write_text("".join(f"file '{clip.resolve().as_posix()}'\n" for clip in clips), encoding="utf-8")
    visuals = output_dir / "long_visuals.mp4"
    video_filter = (
        "scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,fps=30,"
        f"tpad=stop_mode=clone:stop_duration={target_duration},"
        f"trim=duration={target_duration},setpts=PTS-STARTPTS,format=yuv420p,"
        "unsharp=5:5:1.0:5:5:0.0"
    )
    LOG.info("Concatenating and normalizing %d long-form scene clips...", len(clips))
    run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-an", "-vf", video_filter, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(visuals)],
        timeout_seconds=LONG_FORM_FINAL_RENDER_TIMEOUT_SECONDS,
    )

    captions = output_dir / "long_captions_en.ass"
    caption_seconds = min(narration_seconds, target_duration)
    cues = caption_cues_from_text(plan.narration, caption_seconds)
    write_ass_captions(cues, captions, play_res_x=1920, play_res_y=1080, font_size=58, margin_v=92)
    final_video = output_dir / "long.mp4"
    LOG.info("Muxing horizontal long-form video...")
    mux_video_audio_with_captions(
        visuals,
        narration,
        captions,
        final_video,
        target_duration,
        settings,
        long_form=True,
        timeout_seconds=LONG_FORM_FINAL_RENDER_TIMEOUT_SECONDS,
    )
    return final_video


def retime_vertical_visuals(visuals: Path, destination: Path, target_duration: float) -> Path:
    if destination.is_file() and abs(media_duration(destination) - target_duration) <= 0.15:
        LOG.info("Reusing Vietnamese social visual timeline at %.3fs.", target_duration)
        return destination
    video_filter = (
        "tpad=stop_mode=clone:stop_duration="
        f"{target_duration},trim=duration={target_duration},setpts=PTS-STARTPTS,format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-i", str(visuals), "-an", "-vf", video_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(destination),
    ])
    return destination


def render_social_video(social: SocialPlan, tts: OpenAIShortVietnameseTTS, output_dir: Path, settings: Settings) -> Path:
    visuals = output_dir / "visuals.mp4"
    if not visuals.is_file():
        raise BotError(f"Không tìm thấy visuals.mp4 để tạo bản social: {visuals}")
    narration = output_dir / "narration_vi.mp3"
    LOG.info("Generating Vietnamese narration for Facebook/TikTok with OpenAI gpt-4o-mini-tts…")
    tts.speech(social.narration, narration)
    narration_seconds = measured_narration_duration(narration, "Short Vietnamese")
    social_visuals = retime_vertical_visuals(
        visuals,
        output_dir / "visuals_vi.mp4",
        narration_seconds,
    )
    captions = output_dir / "captions_vi.ass"
    caption_seconds = narration_seconds
    cues = caption_cues_from_text(social.narration, caption_seconds)
    write_ass_captions(cues, captions)
    LOG.info("Generated %d Vietnamese caption cues synced to %.2fs narration.", len(cues), caption_seconds)
    social_video = output_dir / "short_vi.mp4"
    LOG.info("Muxing Vietnamese social video with captions…")
    mux_video_audio_with_captions(social_visuals, narration, captions, social_video, narration_seconds, settings)
    return social_video


def authorize_youtube(settings: Settings) -> None:
    if not settings.youtube_client_secrets.exists():
        raise BotError(f"Thieu OAuth client secrets: {settings.youtube_client_secrets}")
    from google_auth_oauthlib.flow import InstalledAppFlow

    scope = ["https://www.googleapis.com/auth/youtube.upload"]
    LOG.info("Opening the browser for YouTube authorization...")
    credentials = InstalledAppFlow.from_client_secrets_file(
        str(settings.youtube_client_secrets),
        scope,
    ).run_local_server(port=0)
    settings.youtube_token.write_text(credentials.to_json(), encoding="utf-8")
    LOG.info("Saved YouTube authorization token to %s", settings.youtube_token)


def upload_to_youtube(
    video: Path,
    plan: ShortPlan,
    settings: Settings,
    privacy: str,
    thumbnail: Path | None = None,
) -> str:
    if not settings.youtube_client_secrets.exists():
        raise BotError(f"Thiếu OAuth client secrets: {settings.youtube_client_secrets}")
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
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
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise BotError(
                    "YouTube OAuth token da het han hoac bi revoke. "
                    "Tao lai youtube_token.json o local bang: "
                    "python youtube_shorts_bot.py --authorize-youtube "
                    "roi cap nhat file/bien YOUTUBE_TOKEN_JSON_B64 tren Railway."
                ) from exc
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
    video_id = str(response["id"])
    LOG.info("YouTube upload completed.")
    if thumbnail:
        if not thumbnail.is_file() or thumbnail.stat().st_size > YOUTUBE_THUMBNAIL_MAX_BYTES:
            raise BotError(f"Thumbnail YouTube không hợp lệ: {thumbnail}")
        try:
            service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg", resumable=False),
            ).execute()
            LOG.info("Custom YouTube thumbnail set for video %s.", video_id)
        except HttpError as exc:
            # The video is already live at this point. Preserve its published
            # state, but leave an actionable error in Railway's logs.
            LOG.error("Video %s uploaded, but its custom thumbnail could not be set: %s", video_id, exc)
    return video_id


def response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise BotError(f"API trả phản hồi không phải JSON: {response.text[:500]}") from exc
    if not isinstance(data, dict):
        raise BotError(f"API trả JSON không đúng định dạng: {data}")
    return data


def facebook_error_detail(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
    error = data.get("error")
    if not isinstance(error, dict):
        return str(data), None, None
    code = str(error.get("code")) if error.get("code") is not None else None
    subcode = str(error.get("error_subcode")) if error.get("error_subcode") is not None else None
    message = str(error.get("message") or data)
    code_note = f" (code {code}, subcode {subcode})" if subcode else f" (code {code})" if code else ""
    if code == "190" and subcode == "463":
        return (
            "Meta bao access token da het han"
            f"{code_note}. Hay tao lai Page access token cho FACEBOOK_PAGE_ACCESS_TOKEN, "
            "hoac dat FACEBOOK_USER_ACCESS_TOKEN la long-lived user token de bot tu lay Page token qua /me/accounts.",
            code,
            subcode,
        )
    if code == "190":
        return (
            "Meta bao access token khong hop le"
            f"{code_note}: {message}. Kiem tra token, quyen Page va viec user/app van con duoc cap quyen.",
            code,
            subcode,
        )
    return f"{message}{code_note}", code, subcode


def facebook_response_json(response: requests.Response, context: str) -> dict[str, Any]:
    data = response_json(response)
    if response.ok and "error" not in data:
        return data
    detail, code, subcode = facebook_error_detail(data)
    raise FacebookAPIError(f"{context}: {detail}", code=code, subcode=subcode)


def facebook_page_token_from_user_token(settings: Settings) -> str:
    if not settings.facebook_page_id:
        raise BotError("Thiếu FACEBOOK_PAGE_ID.")
    if not settings.facebook_user_access_token:
        raise BotError("Thiếu FACEBOOK_USER_ACCESS_TOKEN để tự lấy Page access token.")
    url: str | None = f"https://graph.facebook.com/{settings.facebook_graph_version}/me/accounts"
    params: dict[str, str] | None = {
        "fields": "id,name,access_token",
        "access_token": settings.facebook_user_access_token,
    }
    while url:
        response = requests.get(url, params=params, timeout=(30, 120))
        data = facebook_response_json(response, "Facebook không lấy được danh sách Page từ user token")
        pages = data.get("data", [])
        if not isinstance(pages, list):
            raise BotError(f"Facebook trả danh sách Page không đúng định dạng: {data}")
        for page in pages:
            if not isinstance(page, dict):
                continue
            if str(page.get("id")) == settings.facebook_page_id:
                token = str(page.get("access_token") or "")
                if token:
                    LOG.info("Resolved Facebook Page access token from FACEBOOK_USER_ACCESS_TOKEN.")
                    return token
                raise BotError(
                    "Facebook tìm thấy Page nhưng không trả access_token. "
                    "Hãy cấp quyền pages_show_list và pages_manage_posts cho token."
                )
        paging = data.get("paging", {})
        url = str(paging.get("next") or "") if isinstance(paging, dict) else ""
        params = None
    raise BotError(
        "FACEBOOK_USER_ACCESS_TOKEN hợp lệ nhưng không thấy FACEBOOK_PAGE_ID trong /me/accounts. "
        "Hãy chắc chắn user quản lý Page này và đã chọn Page khi cấp quyền."
    )


def resolve_facebook_page_access_token(settings: Settings) -> str:
    if not settings.facebook_page_id:
        raise BotError("Thiếu FACEBOOK_PAGE_ID.")
    if settings.facebook_page_access_token:
        return settings.facebook_page_access_token
    return facebook_page_token_from_user_token(settings)


def upload_to_facebook_page_with_token(
    video: Path,
    social: SocialPlan,
    settings: Settings,
    access_token: str,
) -> str:
    url = f"https://graph-video.facebook.com/{settings.facebook_graph_version}/{settings.facebook_page_id}/videos"
    LOG.info("Uploading %s to Facebook Page %s…", video.name, settings.facebook_page_id)
    with video.open("rb") as handle:
        response = requests.post(
            url,
            data={
                "access_token": access_token,
                "title": social.title,
                "description": social.description,
                "published": "true",
            },
            files={"source": (video.name, handle, "video/mp4")},
            timeout=(30, 900),
        )
    data = facebook_response_json(response, "Facebook upload thất bại")
    video_id = str(data.get("id") or data.get("video_id") or "")
    if not video_id:
        raise BotError(f"Facebook upload không trả video id: {data}")
    LOG.info("Facebook upload completed: %s", video_id)
    return video_id


def upload_to_facebook_page(video: Path, social: SocialPlan, settings: Settings) -> str:
    if not settings.facebook_page_id:
        raise BotError("Thiếu FACEBOOK_PAGE_ID.")
    if not settings.facebook_page_access_token and not settings.facebook_user_access_token:
        raise BotError("Thiếu FACEBOOK_PAGE_ACCESS_TOKEN hoặc FACEBOOK_USER_ACCESS_TOKEN.")
    try:
        return upload_to_facebook_page_with_token(
            video,
            social,
            settings,
            resolve_facebook_page_access_token(settings),
        )
    except FacebookAPIError as exc:
        if not (
            exc.is_expired_token
            and settings.facebook_page_access_token
            and settings.facebook_user_access_token
        ):
            raise
        LOG.warning(
            "Configured FACEBOOK_PAGE_ACCESS_TOKEN is expired; retrying with a Page token from FACEBOOK_USER_ACCESS_TOKEN."
        )
        return upload_to_facebook_page_with_token(
            video,
            social,
            settings,
            facebook_page_token_from_user_token(settings),
        )


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


def social_planning_duration(output_dir: Path, fallback_duration: int) -> int:
    """Use the actual English narration length when it is available on disk."""
    narration = output_dir / "narration.mp3"
    if not narration.is_file():
        return fallback_duration
    try:
        return max(1, round(measured_narration_duration(narration, "Short English social planning")))
    except BotError as exc:
        LOG.warning("Could not measure English narration for social planning: %s", exc)
        return fallback_duration


def prepare_social_video(plan: ShortPlan, llm: OpenAITextClient, tts: OpenAIShortVietnameseTTS, output_dir: Path, settings: Settings) -> tuple[Path, SocialPlan]:
    social_file = output_dir / "social_vi.json"
    if social_file.is_file():
        social = SocialPlan.from_dict(json.loads(social_file.read_text(encoding="utf-8")))
    else:
        social = plan_social_vietnamese(
            llm,
            plan,
            social_planning_duration(output_dir, settings.duration),
        )
        social_file.write_text(json.dumps(social.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    social_video = output_dir / "short_vi.mp4"
    if not social_video.is_file():
        social_video = render_social_video(social, tts, output_dir, settings)
    return social_video, social


def create_long_form_job(
    llm: OpenAITextClient,
    archive: Archive,
    theme: str,
    settings: Settings,
) -> tuple[ShortPlan, Path, int, int]:
    min_duration = min(settings.long_form_min_duration_seconds, settings.long_form_max_duration_seconds)
    max_duration = max(settings.long_form_min_duration_seconds, settings.long_form_max_duration_seconds)
    min_scenes = min(settings.long_form_min_scenes, settings.long_form_max_scenes)
    max_scenes = max(settings.long_form_min_scenes, settings.long_form_max_scenes)
    duration = random.randint(min_duration, max_duration)
    plan = choose_novel_long_form_plan(llm, archive, theme, duration, min_scenes, max_scenes)
    if plan is None:
        raise BotError("Could not create a novel long-form plan after retries.")
    output_dir = DATA_DIR / "generated" / f"long-{datetime.now():%Y%m%d-%H%M%S}-{slug(plan.topic)}"
    output_dir.mkdir(parents=True)
    (output_dir / "plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    record_id = archive.reserve(plan, output_dir)
    LOG.info("Created long-form job: %s (%ds)", output_dir, duration)
    return plan, output_dir, duration, record_id


def load_resumable_short_form_job(archive: Archive) -> tuple[ShortPlan, Path, float, int] | None:
    """Load an interrupted Short job without re-planning or regenerating its narration."""
    for row in archive.resumable_short_form_jobs():
        output_dir = Path(str(row.get("output_path") or ""))
        plan_file = output_dir / "plan.json"
        narration = output_dir / "narration.mp3"
        if not plan_file.is_file() or not narration.is_file() or narration.stat().st_size < 1024:
            continue
        try:
            plan = ShortPlan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
            narration_seconds = measured_narration_duration(narration, "Short resume")
        except (BotError, OSError, json.JSONDecodeError) as exc:
            LOG.warning("Skipping unusable unfinished Short job %s: %s", output_dir, exc)
            continue
        scene_total = sum(scene.duration for scene in plan.scenes)
        if abs(scene_total - narration_seconds) > 0.1:
            rescale_scene_durations(plan, narration_seconds, "Short resume")
            plan_file.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        record_id = int(row["id"])
        existing_images = sum(
            1 for index in range(1, len(plan.scenes) + 1)
            if (output_dir / f"scene_{index}.jpg").is_file()
        )
        existing_clips = sum(
            1 for index, scene in enumerate(plan.scenes, start=1)
            if short_form_clip_ready(output_dir / f"scene_{index}.mp4", scene.duration)
        )
        LOG.info(
            "Resuming interrupted Short job from %s (%d/%d images, %d/%d scene clips, %.3fs narration; no new TTS or duplicate image requests).",
            output_dir,
            existing_images,
            len(plan.scenes),
            existing_clips,
            len(plan.scenes),
            narration_seconds,
        )
        return plan, output_dir, narration_seconds, record_id
    return None


def load_resumable_long_form_job(archive: Archive) -> tuple[ShortPlan, Path, float, int] | None:
    """Load a fully prepared but unfinished Long job without calling TTS or image APIs."""
    for row in archive.resumable_long_form_jobs():
        output_dir = Path(str(row.get("output_path") or ""))
        plan_file = output_dir / "plan.json"
        narration = output_dir / "long_narration.mp3"
        if not plan_file.is_file() or not narration.is_file() or narration.stat().st_size < 1024:
            continue
        try:
            plan = ShortPlan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
            narration_seconds = measured_narration_duration(narration, "Long-form resume")
        except (BotError, OSError, json.JSONDecodeError) as exc:
            LOG.warning("Skipping unusable unfinished long-form job %s: %s", output_dir, exc)
            continue
        if not long_form_assets_ready(plan, output_dir):
            # Do not silently spend more image credits after an interruption.
            continue
        scene_total = sum(scene.duration for scene in plan.scenes)
        if abs(scene_total - narration_seconds) > 0.1:
            rescale_scene_durations(plan, narration_seconds, "Long-form resume")
            plan_file.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        record_id = int(row["id"])
        LOG.info(
            "Resuming interrupted long-form job from %s (%d ready images, %.3fs narration; no new image/TTS requests).",
            output_dir,
            len(plan.scenes),
            narration_seconds,
        )
        return plan, output_dir, narration_seconds, record_id
    return None


def long_form_is_due(
    archive: Archive,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[bool, int | None]:
    try:
        timezone = ZoneInfo(settings.long_form_timezone)
    except Exception:
        LOG.warning("Invalid LONG_FORM_TIMEZONE=%s; falling back to UTC.", settings.long_form_timezone)
        timezone = UTC
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    current_date = current.astimezone(timezone).date()
    latest = archive.latest_long_form_created_at()
    if latest is None:
        return True, None
    days_since = (current_date - latest.astimezone(timezone).date()).days
    return days_since >= settings.long_form_interval_days, days_since


def long_form_video_ready(video: Path, expected_duration: float) -> bool:
    if not video.is_file() or video.stat().st_size < 1024:
        return False
    try:
        return abs(media_duration(video) - expected_duration) <= 1.0
    except BotError:
        return False


def short_form_video_ready(video: Path, expected_duration: float) -> bool:
    if not video.is_file() or video.stat().st_size < 1024:
        return False
    try:
        return abs(media_duration(video) - expected_duration) <= 1.0
    except BotError:
        return False


def publish_long_form_video(video: Path, plan: ShortPlan, settings: Settings, privacy: str) -> dict[str, str]:
    thumbnail = create_long_form_thumbnail(plan, video, video.parent, settings)
    youtube_id = upload_to_youtube(video, plan, settings, privacy, thumbnail=thumbnail)
    # Long-form is YouTube-only. Facebook/TikTok Vietnamese publishing applies
    # to the separate Short workflow only.
    return {"youtube": youtube_id}


def run_long_form_flow(
    publish: bool,
    privacy: str,
    theme: str,
    settings: Settings,
    archive: Archive,
    llm: OpenAITextClient,
    images: VisualAssetProvider,
    tts: GoogleCloudTTS,
    force_new: bool = False,
) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    resumable = load_resumable_long_form_job(archive)
    resumed = resumable is not None
    if resumable:
        plan, output_dir, duration, record_id = resumable
        narration = output_dir / "long_narration.mp3"
        narration_seconds = duration
        archive.mark(record_id, "rendering")
    else:
        due, days_since = long_form_is_due(archive, settings)
        if not force_new and not due:
            LOG.info(
                "Long-form is not due yet: last job was %s local day(s) ago; interval is %d days.",
                days_since,
                settings.long_form_interval_days,
            )
            return 0
        plan, output_dir, duration, record_id = create_long_form_job(llm, archive, theme, settings)
        narration = output_dir / "long_narration.mp3"
        narration_seconds = 0.0
    rendered = False
    try:
        if not resumed:
            narration, narration_seconds = prepare_long_form_narration(
                plan,
                tts,
                output_dir,
            )
            duration = rescale_scene_durations(plan, narration_seconds, "Long-form")
            (output_dir / "plan.json").write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            prepare_long_form_images(plan, images, output_dir)

        video = output_dir / "long.mp4"
        if long_form_video_ready(video, duration):
            LOG.info("Reusing completed long-form video %s; skipping render.", video.name)
        else:
            video = render_long_form_from_assets(
                plan,
                output_dir,
                duration,
                settings,
                narration,
                narration_seconds,
            )
        append_web_source_credits(plan, images.web_sources)
        (output_dir / "plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if images.web_sources:
            (output_dir / "web_sources.json").write_text(
                json.dumps(images.web_sources, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        rendered = True
        archive.mark(record_id, "rendered")
        print(f"Rendered long-form video: {video}")
        if publish:
            results = publish_long_form_video(video, plan, settings, privacy)
            archive.mark(record_id, "published", results.get("youtube"))
            print(f"Published long-form video: {results}")
            if settings.youtube_token.exists():
                archive.set_kv("youtube_token", settings.youtube_token.read_text(encoding="utf-8"))
    except Exception:
        archive.mark(record_id, "upload_failed" if rendered else "failed")
        raise
    return 0


def slug(value: str) -> str:
    simple = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", simple).strip("-")[:42] or "short"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def run_manual_short_flow(
    idea: str,
    duration: int,
    publish: bool,
    privacy: str,
    settings: Settings,
    archive: Archive,
    llm: OpenAITextClient,
    images: VisualAssetProvider,
    tts: GoogleCloudTTS,
    social_tts: OpenAIShortVietnameseTTS,
) -> tuple[str | None, str]:
    """Render one Short from a user idea. No daily-limit, novelty, or resume gate. Returns (youtube_id, title)."""
    plan = plan_short_from_idea(llm, duration, idea)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_dir = DATA_DIR / "generated" / f"manual-{datetime.now():%Y%m%d-%H%M%S}-{slug(plan.topic)}"
    output_dir.mkdir(parents=True)
    (output_dir / "plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    record_id = archive.reserve(plan, output_dir)
    youtube_id: str | None = None
    rendered = False
    try:
        narration, narration_seconds = prepare_short_english_narration(plan, tts, output_dir)
        rescale_scene_durations(plan, narration_seconds, "Manual Short English")
        (output_dir / "plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        video = render(plan, images, output_dir, narration_seconds, narration, narration_seconds)
        append_web_source_credits(plan, images.web_sources)
        (output_dir / "plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if images.web_sources:
            (output_dir / "web_sources.json").write_text(
                json.dumps(images.web_sources, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        rendered = True
        archive.mark(record_id, "rendered")
        print(f"Đã render (manual Short): {video}")
        if publish:
            youtube_id = upload_to_youtube(video, plan, settings, privacy)
            archive.mark(record_id, "published", youtube_id)
            print(f"Đã upload: https://youtube.com/watch?v={youtube_id}")
            if settings.youtube_token.exists():
                archive.set_kv("youtube_token", settings.youtube_token.read_text(encoding="utf-8"))
            if social_publish_enabled(settings):
                social_video, social = prepare_social_video(plan, llm, social_tts, output_dir, settings)
                social_results = publish_social_video(social_video, social, settings)
                if social_results:
                    print(f"Đã publish social: {social_results}")
            if archive.is_postgres:
                try:
                    shutil.rmtree(output_dir)
                    LOG.info("Cleaned up output directory: %s", output_dir)
                except Exception as exc:
                    LOG.warning("Could not clean up %s: %s", output_dir, exc)
    except Exception:
        archive.mark(record_id, "upload_failed" if rendered else "failed")
        raise
    return youtube_id, plan.title


def run_manual_long_form_flow(
    idea: str,
    publish: bool,
    privacy: str,
    settings: Settings,
    archive: Archive,
    llm: OpenAITextClient,
    images: VisualAssetProvider,
    tts: GoogleCloudTTS,
) -> tuple[str | None, str]:
    """Render one long-form video from a user idea. No due-gate, novelty, or resume gate. Returns (youtube_id, title)."""
    min_duration = min(settings.long_form_min_duration_seconds, settings.long_form_max_duration_seconds)
    max_duration = max(settings.long_form_min_duration_seconds, settings.long_form_max_duration_seconds)
    min_scenes = min(settings.long_form_min_scenes, settings.long_form_max_scenes)
    max_scenes = max(settings.long_form_min_scenes, settings.long_form_max_scenes)
    duration = random.randint(min_duration, max_duration)
    plan = plan_long_form_from_idea(llm, duration, min_scenes, max_scenes, idea)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_dir = DATA_DIR / "generated" / f"long-manual-{datetime.now():%Y%m%d-%H%M%S}-{slug(plan.topic)}"
    output_dir.mkdir(parents=True)
    (output_dir / "plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    record_id = archive.reserve(plan, output_dir)
    youtube_id: str | None = None
    rendered = False
    try:
        narration, narration_seconds = prepare_long_form_narration(plan, tts, output_dir)
        duration = rescale_scene_durations(plan, narration_seconds, "Manual long-form")
        (output_dir / "plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        prepare_long_form_images(plan, images, output_dir)
        video = render_long_form_from_assets(plan, output_dir, duration, settings, narration, narration_seconds)
        append_web_source_credits(plan, images.web_sources)
        (output_dir / "plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if images.web_sources:
            (output_dir / "web_sources.json").write_text(
                json.dumps(images.web_sources, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        rendered = True
        archive.mark(record_id, "rendered")
        print(f"Rendered manual long-form video: {video}")
        if publish:
            results = publish_long_form_video(video, plan, settings, privacy)
            youtube_id = results.get("youtube")
            archive.mark(record_id, "published", youtube_id)
            print(f"Published manual long-form video: {results}")
            if settings.youtube_token.exists():
                archive.set_kv("youtube_token", settings.youtube_token.read_text(encoding="utf-8"))
            if archive.is_postgres:
                try:
                    shutil.rmtree(output_dir)
                    LOG.info("Cleaned up output directory: %s", output_dir)
                except Exception as exc:
                    LOG.warning("Could not clean up %s: %s", output_dir, exc)
    except Exception:
        archive.mark(record_id, "upload_failed" if rendered else "failed")
        raise
    return youtube_id, plan.title


def run_manual_idea(
    args: argparse.Namespace,
    settings: Settings,
    archive: Archive,
    llm: OpenAITextClient,
    images: VisualAssetProvider,
    tts: GoogleCloudTTS,
    social_tts: OpenAIShortVietnameseTTS,
) -> int:
    """Dispatch a manually submitted idea (from --idea text or a --idea-id queue row)."""
    idea_id: int | None = args.idea_id
    if idea_id is not None:
        row = archive.get_idea(idea_id)
        if not row:
            raise BotError(f"Khong tim thay idea_queue id={idea_id}.")
        idea_text = str(row.get("idea") or "").strip()
        mode = str(row.get("mode") or "short").strip().lower()
        raw_duration = row.get("duration")
        duration = int(raw_duration) if raw_duration else settings.duration
        publish = bool(row.get("publish"))
        privacy = str(row.get("privacy") or settings.youtube_privacy)
    else:
        idea_text = str(args.idea or "").strip()
        mode = "long" if args.long_form else "short"
        duration = args.duration or settings.duration
        publish = args.publish
        privacy = args.privacy_status or settings.youtube_privacy
    if not idea_text:
        raise BotError("Y tuong rong; khong the tao video.")
    if privacy not in ("private", "unlisted", "public"):
        privacy = settings.youtube_privacy
    LOG.info("Manual idea (id=%s, mode=%s, publish=%s): %.80s", idea_id, mode, publish, idea_text)

    if args.dry_run:
        if mode == "long":
            min_scenes = min(settings.long_form_min_scenes, settings.long_form_max_scenes)
            max_scenes = max(settings.long_form_min_scenes, settings.long_form_max_scenes)
            preview_duration = min(settings.long_form_min_duration_seconds, settings.long_form_max_duration_seconds)
            plan = plan_long_form_from_idea(llm, preview_duration, min_scenes, max_scenes, idea_text)
        else:
            plan = plan_short_from_idea(llm, duration, idea_text)
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0

    try:
        if mode == "long":
            youtube_id, title = run_manual_long_form_flow(
                idea_text, publish, privacy, settings, archive, llm, images, tts
            )
        else:
            youtube_id, title = run_manual_short_flow(
                idea_text, duration, publish, privacy, settings, archive, llm, images, tts, social_tts
            )
    except Exception as exc:
        if idea_id is not None:
            archive.update_idea(idea_id, "failed", error=str(exc)[:2000])
        raise
    if idea_id is not None:
        archive.update_idea(idea_id, "done", youtube_id=youtube_id, output_title=title)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--theme",
        default="world-historical figures, major historical events, civilizations, wars, empires, renowned natural wonders, iconic world architecture, major landmarks, monuments, and transformative cultural history with surprising concrete details",
    )
    parser.add_argument(
        "--duration",
        type=int,
        choices=range(MIN_SHORT_DURATION_SECONDS, MAX_SHORT_DURATION_SECONDS + 1),
        metavar=f"{MIN_SHORT_DURATION_SECONDS}..{MAX_SHORT_DURATION_SECONDS}",
    )
    parser.add_argument("--publish", action="store_true", help="Tự upload sau khi render")
    parser.add_argument("--privacy-status", choices=("private", "unlisted", "public"))
    parser.add_argument("--authorize-youtube", action="store_true", help="Create youtube_token.json using a local browser")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ tạo và in kế hoạch")
    parser.add_argument("--upload-file", type=Path, help="Upload lại MP4 đã render, không tạo nội dung/video mới")
    parser.add_argument("--scheduled", action="store_true", help="Bật giới hạn an toàn theo SCHEDULED_DAILY_LIMIT mỗi ngày UTC")
    parser.add_argument("--long-form", action="store_true", help="Create, render, and optionally publish one horizontal video")
    parser.add_argument("--idea", type=str, help="Tạo video từ một ý tưởng cụ thể (thủ công) thay vì tự sinh ý tưởng")
    parser.add_argument("--idea-id", type=int, help="Đọc ý tưởng thủ công từ hàng idea_queue theo id (dùng bởi web frontend)")
    # Accepted temporarily so existing Railway commands do not fail; the pipeline is now always one-shot.
    parser.add_argument("--long-form-mode", choices=("prepare", "finalize", "auto"), default="auto", help=argparse.SUPPRESS)
    parser.add_argument("--long-form-image-budget", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--long-form-force-new", action="store_true", help="Bypass the two-day long-form schedule gate")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    args = parser.parse_args()
    configure_logging(args.log_level)
    run_mode = os.getenv("BOT_RUN_MODE", "").strip().lower().replace("_", "-")
    env_forces_long_form = run_mode in {"long", "long-form", "longform"}
    LOG.info("Startup args: %s | BOT_RUN_MODE=%s", " ".join(sys.argv[1:]) or "(none)", run_mode or "(unset)")
    materialize_railway_credentials()
    ensure_dejavu_font()
    settings = Settings.from_env(args.duration)
    if args.authorize_youtube:
        authorize_youtube(settings)
        print(f"Da tao YouTube token: {settings.youtube_token}")
        return 0
    archive = Archive()
    
    token_db = archive.get_kv("youtube_token")
    if token_db:
        settings.youtube_token.write_text(token_db, encoding="utf-8")

    images = VisualAssetProvider(settings)
    llm = OpenAITextClient(settings)
    tts = GoogleCloudTTS(settings)
    social_tts = OpenAIShortVietnameseTTS(settings)
    if args.idea is not None or args.idea_id is not None:
        return run_manual_idea(args, settings, archive, llm, images, tts, social_tts)
    if args.long_form or env_forces_long_form:
        long_form_theme = args.theme if args.theme != parser.get_default("theme") else LONG_FORM_DEFAULT_THEME
        return run_long_form_flow(
            publish=args.publish,
            privacy=args.privacy_status or settings.youtube_privacy,
            theme=long_form_theme,
            settings=settings,
            archive=archive,
            llm=llm,
            images=images,
            tts=tts,
            force_new=args.long_form_force_new,
        )
    if args.upload_file:
        if not args.publish:
            parser.error("--upload-file requires --publish")
        video = args.upload_file.resolve()
        plan_file = video.parent / "plan.json"
        if not video.is_file() or not plan_file.is_file():
            raise BotError("--upload-file phải trỏ đến short.mp4 có plan.json cùng thư mục.")
        plan = ShortPlan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
        thumbnail = create_long_form_thumbnail(plan, video, video.parent, settings) if video.name == "long.mp4" else None
        youtube_id = upload_to_youtube(
            video,
            plan,
            settings,
            args.privacy_status or settings.youtube_privacy,
            thumbnail=thumbnail,
        )
        print(f"Đã upload: https://youtube.com/watch?v={youtube_id}")
        if settings.youtube_token.exists():
            archive.set_kv("youtube_token", settings.youtube_token.read_text(encoding="utf-8"))

        if social_publish_enabled(settings):
            social_video, social = prepare_social_video(plan, llm, social_tts, video.parent, settings)
            social_results = publish_social_video(social_video, social, settings)
            if social_results:
                print(f"Đã publish social: {social_results}")
        
        if archive.is_postgres:
            try:
                shutil.rmtree(video.parent)
                LOG.info("Cleaned up output directory: %s", video.parent)
            except Exception as exc:
                LOG.warning("Could not clean up %s: %s", video.parent, exc)
                
        return 0
    resumable = None if args.dry_run else load_resumable_short_form_job(archive)
    resumed = resumable is not None
    if resumable:
        plan, output_dir, narration_seconds, record_id = resumable
        narration = output_dir / "narration.mp3"
        archive.mark(record_id, "rendering")
    else:
        if args.scheduled and settings.scheduled_daily_limit > 0:
            jobs_today = archive.jobs_created_today()
            if jobs_today >= settings.scheduled_daily_limit:
                LOG.warning(
                    "Daily limit reached: %d/%d video jobs have already been created today (UTC). Exiting.",
                    jobs_today,
                    settings.scheduled_daily_limit,
                )
                return 0
        plan = choose_novel_plan(llm, archive, args.theme, settings.duration)
        if plan is None:
            message = "Không tìm được ý tưởng đủ mới sau 4 lần."
            if args.scheduled:
                LOG.warning("%s Bỏ qua lượt scheduled này.", message)
                return 0
            raise BotError(message)

        if args.dry_run:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            return 0
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        output_dir = DATA_DIR / "generated" / f"{datetime.now():%Y%m%d-%H%M%S}-{slug(plan.topic)}"
        output_dir.mkdir(parents=True)
        (output_dir / "plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        record_id = archive.reserve(plan, output_dir)
        narration = output_dir / "narration.mp3"
        narration_seconds = 0.0
    rendered = False
    try:
        if not resumed:
            narration, narration_seconds = prepare_short_english_narration(
                plan,
                tts,
                output_dir,
            )
            rescale_scene_durations(plan, narration_seconds, "Short English")
            (output_dir / "plan.json").write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        video = output_dir / "short.mp4"
        if short_form_video_ready(video, narration_seconds):
            LOG.info("Reusing completed Short video %s; skipping render.", video.name)
        else:
            video = render(
                plan,
                images,
                output_dir,
                narration_seconds,
                narration,
                narration_seconds,
            )
        append_web_source_credits(plan, images.web_sources)
        (output_dir / "plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if images.web_sources:
            (output_dir / "web_sources.json").write_text(
                json.dumps(images.web_sources, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        rendered = True
        archive.mark(record_id, "rendered")
        print(f"Đã render: {video}")
        if args.publish:
            youtube_id = upload_to_youtube(video, plan, settings, args.privacy_status or settings.youtube_privacy)
            archive.mark(record_id, "published", youtube_id)
            print(f"Đã upload: https://youtube.com/watch?v={youtube_id}")
            if settings.youtube_token.exists():
                archive.set_kv("youtube_token", settings.youtube_token.read_text(encoding="utf-8"))

            if social_publish_enabled(settings):
                social_video, social = prepare_social_video(plan, llm, social_tts, output_dir, settings)
                social_results = publish_social_video(social_video, social, settings)
                if social_results:
                    print(f"Đã publish social: {social_results}")
                    
            if archive.is_postgres:
                try:
                    shutil.rmtree(output_dir)
                    LOG.info("Cleaned up output directory: %s", output_dir)
                except Exception as exc:
                    LOG.warning("Could not clean up %s: %s", output_dir, exc)
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
