from __future__ import annotations

import hashlib
import json
import math
import os
import re
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from youtube_shorts_bot import BotError

VERSION = "movie-v1"
RIGHTS_STATES = ("unknown", "restricted", "owned", "licensed", "legal_basis")
SPOKEN_CLEAN_VERSION = "spoken-metadata-v2"
REFERENCE_TOKEN = r"(?:[sc]\d{3}_[\w-]+|ctx_[\w-]+|char_[\w-]+|b\d{2}:\d+)"
REFERENCE_LIST = REFERENCE_TOKEN + r"(?:(?:\s*[,;|]\s*|\s+)" + REFERENCE_TOKEN + r")*"
INLINE_CITATION = re.compile(r"[\[(]\s*" + REFERENCE_LIST + r"\s*[\])]", re.IGNORECASE)
QUOTED_CITATION = re.compile(r"(?P<quote>[\"'`]|\*{1,2})\s*" + REFERENCE_LIST + r"\s*(?P=quote)", re.IGNORECASE)
BARE_CITATION = re.compile(r"(?<![\w-])" + REFERENCE_TOKEN + r"(?![\w-])", re.IGNORECASE)


def spoken_text(text: str) -> str:
    # Reserved tokens may leak without brackets or in mixed case. Preserve
    # ordinary dialogue, character names (C-3PO/R2-D2) and film numbers.
    clean = INLINE_CITATION.sub("", text)
    clean = QUOTED_CITATION.sub("", clean)
    clean = BARE_CITATION.sub("", clean)
    if clean != text:
        clean = re.sub(r"[\[(]\s*[,;|]*\s*[\])]", "", clean)
        clean = re.sub(r"[,;:]\s*([.!?])", r"\1", clean)
        clean = re.sub(r"(?:[,;]\s*){2,}", ", ", clean)
        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
    return re.sub(r"\s+", " ", clean).strip()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BotError(f"Cache hỏng: {path.name}; hãy kiểm tra file trước khi tiếp tục.") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def job_lock(directory: Path):
    """OS releases the lock on crash; no stale PID lock prevents resumption."""
    directory.mkdir(parents=True, exist_ok=True)
    stream = (directory / ".lock").open("a+b")
    stream.seek(0)
    stream.write(b"0")
    stream.flush()
    stream.seek(0)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise BotError("Job này đang chạy trong tiến trình khác.") from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


@dataclass(frozen=True)
class MovieConfig:
    video_provider: str = "gemini"
    model: str = "gemini-3.8-flash"
    verifier_model: str = "gemini-3.8-flash"
    writer_provider: str = "gemini"
    writer_model: str = ""
    identity_provider: str = "gemini"
    identity_model: str = ""
    global_fps: float = 1
    scene_fps: float = 4
    recheck_fps: float = 8
    max_scene_seconds: float = 20
    max_scenes: int = 30
    max_input_seconds: float = 300
    max_input_mb: int = 500
    max_calls: int = 100
    max_tokens: int = 2_000_000
    max_output_tokens: int = 8192
    attempts: int = 3
    max_rechecks: int = 6
    min_confidence: float = 0.85
    target_seconds: int = 60
    visual_mode: str = "continuous"
    source_audio_volume: float = 0.30
    logo_width: int = 70
    logo_gap: int = 20
    live_tpm: int = 65000
    live_max_seconds: float = 300
    live_chunk_seconds: float = 45

    @classmethod
    def from_env(cls, target_seconds: int = 60):
        values = {}
        for name, default in asdict(cls()).items():
            raw = os.getenv("MOVIE_" + name.upper())
            values[name] = type(default)(raw) if raw else default
        values["target_seconds"] = target_seconds
        cfg = cls(**values)
        if cfg.visual_mode not in {"continuous", "evidence"}:
            raise BotError("MOVIE_VISUAL_MODE phải là continuous hoặc evidence.")
        if not 0 < cfg.source_audio_volume <= 1:
            raise BotError("MOVIE_SOURCE_AUDIO_VOLUME phải lớn hơn 0 và không quá 1.")
        if cfg.video_provider not in {"gemini", "gemini_live"}:
            raise BotError("MOVIE_VIDEO_PROVIDER phải là gemini hoặc gemini_live.")
        if cfg.video_provider == "gemini_live":
            from dataclasses import replace
            cfg = replace(cfg, global_fps=min(cfg.global_fps, 1), scene_fps=min(cfg.scene_fps, 1),
                          recheck_fps=min(cfg.recheck_fps, 1))
            if "live" not in cfg.model or "live" not in cfg.verifier_model:
                raise BotError("Gemini Live cần MOVIE_MODEL và MOVIE_VERIFIER_MODEL là model Live.")
        if cfg.writer_provider not in {"gemini", "openai"}:
            raise BotError("MOVIE_WRITER_PROVIDER phải là gemini hoặc openai.")
        if cfg.identity_provider not in {"gemini", "openai"}:
            raise BotError("MOVIE_IDENTITY_PROVIDER phải là gemini hoặc openai.")
        for name, value in asdict(cfg).items():
            if isinstance(value, (int, float)) and (not math.isfinite(value) or value <= 0):
                raise BotError(f"MOVIE_{name.upper()} phải là số dương hữu hạn.")
        if not 20 <= cfg.target_seconds <= 180 or not 0.5 <= cfg.min_confidence <= 1:
            raise BotError("Thời lượng đích phải 20–180s; confidence phải 0.5–1.")
        if not cfg.global_fps <= cfg.scene_fps <= cfg.recheck_fps <= 24:
            raise BotError("FPS phải tăng từ global → scene → recheck, tối đa 24.")
        return cfg


def rights_manifest(status: str, basis: str, source_hash: str) -> dict:
    if status not in RIGHTS_STATES:
        raise BotError("Trạng thái quyền sử dụng không hợp lệ.")
    if status in {"owned", "licensed", "legal_basis"} and not basis.strip():
        raise BotError("Cần ghi căn cứ quyền sử dụng trước khi dùng cảnh phim.")
    return {"status": status, "basis": basis.strip(), "source_sha256": source_hash,
            "allow_source_visuals": status in {"owned", "licensed", "legal_basis"}}


class Cache:
    def __init__(self, directory: Path):
        self.directory = directory

    def stage(self, name: str, dependencies: Any, produce, validate=lambda x: x, *, repair=None, attempts=1):
        key = digest({"version": VERSION, "dependencies": dependencies})
        path = self.directory / (name + ".json")
        cached = read_json(path)
        if cached and cached.get("key") == key:
            return validate(deepcopy(cached["data"]))
        response_path = self.directory / (name + ".response.json")
        response = read_json(response_path)
        if not response or response.get("key") != key:
            response = {"key": key, "data": produce()}
            # Preserve received AI content before validation. This is diagnostic
            # input, not accepted evidence, and allows fixes/resume without rebilling.
            write_json(response_path, response)
        for attempt in range(max(1, attempts)):
            try:
                # Validators normalize timestamps in place. Preserve the raw
                # response, so retries/resume use stable dependencies and evidence.
                data = validate(deepcopy(response["data"]))
                break
            except BotError as exc:
                error = str(exc)
                write_json(self.directory / (name + ".validation_error.json"),
                           {"key": key, "error": error, "response_file": response_path.name})
                if repair is None or attempt + 1 == max(1, attempts):
                    raise
                previous = response["data"]
                repair_name = f"{name}_repair_{attempt + 1}"
                corrected = self.stage(repair_name, [key, previous, error],
                                       lambda: repair(deepcopy(previous), error))
                response = {"data": corrected}
                response_path = self.directory / (repair_name + ".response.json")
        write_json(path, {"key": key, "data": data})
        (self.directory / (name + ".validation_error.json")).unlink(missing_ok=True)
        return data


def number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise BotError(f"{label} không phải số.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BotError(f"{label} không phải số.") from exc
    if not math.isfinite(result):
        raise BotError(f"{label} phải hữu hạn.")
    return result


def timestamp(value: Any, label: str) -> float:
    """Normalize numeric seconds and explicit MM:SS / HH:MM:SS, without guessing units."""
    if isinstance(value, str):
        value = value.strip()
        if ":" in value:
            parts = value.split(":")
            if len(parts) not in {2, 3} or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
                raise BotError(f"{label} cần số giây hoặc MM:SS/HH:MM:SS.")
            values = [number(p, label) for p in parts]
            if any(v >= 60 for v in values[1:]) or any(not v.is_integer() for v in values[:-1]):
                raise BotError(f"{label} có định dạng thời gian không hợp lệ.")
            return sum(v * 60 ** i for i, v in enumerate(reversed(values)))
        if value.endswith("s"):
            value = value[:-1].strip()
    return number(value, label)


def validate_span(start_value, end_value, lower: float, upper: float, tolerance: float, label: str):
    start, end = timestamp(start_value, label + " start"), timestamp(end_value, label + " end")
    # Only correct sampling/rounding at a boundary. Never shift, scale or admit an
    # entirely off-screen event; a substantive mismatch must be re-analyzed.
    if not (start < end and start < upper and end > lower
            and start >= lower - tolerance and end <= upper + tolerance):
        raise BotError(f"{label}: timestamp {start:g}–{end:g}s ngoài phạm vi {lower:g}–{upper:.3f}s "
                       f"(sai số làm tròn tối đa {tolerance:g}s).")
    return max(lower, start), min(upper, end)


def validate_global(data: dict, duration: float, timestamp_tolerance: float = 0.5, start: float = 0) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise BotError("Global analysis thiếu summary.")
    events = data.get("events")
    if not isinstance(events, list) or not events:
        raise BotError("Global analysis thiếu timeline sự kiện.")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise BotError(f"Global event {index + 1} phải là object.")
        event["start"], event["end"] = validate_span(event.get("start"), event.get("end"),
                                                    start, duration, timestamp_tolerance,
                                                    f"Timeline global event {index + 1}")
    # Dialogue timestamps are also used as verifier context; don't leave out-of-
    # range or string-valued timestamps hidden in an otherwise valid global pass.
    dialogue = data.get("dialogue", [])
    if not isinstance(dialogue, list):
        raise BotError("Global dialogue phải là danh sách.")
    for index, line in enumerate(dialogue):
        if not isinstance(line, dict):
            raise BotError(f"Global dialogue {index + 1} phải là object.")
        line["start"], line["end"] = validate_span(line.get("start"), line.get("end"),
                                                  start, duration, timestamp_tolerance,
                                                  f"Global dialogue {index + 1}")
    if not isinstance(data.get("characters", []), list):
        raise BotError("Characters phải là danh sách.")
    return data


def validate_scene(data: dict, scene: dict, timestamp_tolerance: float = 0.05) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise BotError("Scene analysis thiếu claims.")
    seen = set()
    for claim in data["claims"]:
        cid = claim.get("id")
        if not isinstance(cid, str) or not cid.startswith(scene["id"] + "_") or cid in seen:
            raise BotError("Claim ID thiếu, trùng hoặc không thuộc scene.")
        seen.add(cid)
        if claim.get("kind") not in {"observed", "inference", "external"}:
            raise BotError("Claim cần phân loại observed/inference/external.")
        if claim.get("modality") not in {"visual", "audio", "both"}:
            raise BotError("Claim thiếu modality.")
        start, end = validate_span(claim.get("start"), claim.get("end"), scene["start"], scene["end"],
                                   timestamp_tolerance, f'Claim {cid}')
        confidence = number(claim.get("confidence"), "confidence")
        if not 0 <= confidence <= 1:
            raise BotError("Claim có timestamp/confidence không hợp lệ.")
        claim.update(start=start, end=end, confidence=confidence)
        if not all(isinstance(claim.get(k), str) and claim[k].strip() for k in ("text", "evidence")):
            raise BotError("Claim thiếu nội dung hoặc bằng chứng.")
    return data


def validate_verdicts(data: dict, claims: list[dict]) -> dict:
    ids = {claim["id"] for claim in claims}
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list) or len(verdicts) != len(ids):
        raise BotError("Verifier phải trả verdict cho mọi claim.")
    if {v.get("id") for v in verdicts} != ids:
        raise BotError("Verifier trả claim ID thiếu/trùng/không tồn tại.")
    for v in verdicts:
        if v.get("status") not in {"supported", "uncertain", "contradicted"}:
            raise BotError("Verifier status không hợp lệ.")
        if not 0 <= number(v.get("confidence"), "verifier confidence") <= 1 or not v.get("reason"):
            raise BotError("Verifier thiếu confidence hoặc lý do.")
    return data


def accepted_evidence(claims: list[dict], verdicts: list[dict], threshold: float) -> dict:
    by_id = {v["id"]: v for v in verdicts}
    accepted, rejected = [], []
    for claim in claims:
        verdict = by_id.get(claim["id"], {})
        valid = (claim["kind"] == "observed" and verdict.get("status") == "supported"
                 and min(claim["confidence"], verdict.get("confidence", 0)) >= threshold)
        (accepted if valid else rejected).append({**claim, "verification": verdict})
    return {"accepted": accepted, "rejected": rejected}


def validate_script(data: dict, evidence: dict, context: dict | None = None) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("title"), str) or not data["title"].strip():
        raise BotError("Kịch bản thiếu tiêu đề.")
    beats = data.get("beats")
    if not isinstance(beats, list) or not 2 <= len(beats) <= 12:
        raise BotError("Kịch bản cần 2–12 beat.")
    allowed = {c["id"]: c for c in evidence["accepted"]}
    context_ids = {c["id"] for c in (context or {}).get("facts", [])}
    for i, beat in enumerate(beats):
        if beat.get("id") != f"b{i + 1:02d}":
            raise BotError("Beat ID cần b01, b02… theo thứ tự.")
        sentences = beat.get("sentences")
        if not isinstance(sentences, list) or not sentences:
            raise BotError("Beat thiếu câu thuyết minh.")
        for sentence in sentences:
            text = sentence.get("text")
            ids = sentence.get("claim_ids")
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise BotError("Câu thuyết minh rỗng hoặc quá dài.")
            sentence["text"] = spoken_text(text)
            if not re.search(r"\w", sentence["text"]):
                raise BotError("Câu thuyết minh chỉ chứa mã bằng chứng.")
            if sentence.get("kind") not in {"fact", "context", "inference", "joke"}:
                raise BotError("Câu thuyết minh cần phân loại fact/context/inference/joke.")
            if not isinstance(ids, list) or not ids or any(cid not in allowed and cid not in context_ids for cid in ids):
                raise BotError("Câu thuyết minh tham chiếu claim chưa được kiểm chứng.")
            if sentence["kind"] == "context" and not any(cid in context_ids for cid in ids):
                raise BotError("Câu bối cảnh cần tham chiếu nguồn bối cảnh.")
            if sentence["kind"] != "context" and not any(cid in allowed for cid in ids):
                raise BotError("Câu phân tích clip cần bằng chứng từ clip.")
        ids = list(dict.fromkeys(cid for s in sentences for cid in s["claim_ids"]))
        visual_id = beat.get("visual_claim_id")
        if visual_id not in ids or visual_id not in allowed:
            raise BotError("Visual phải tham chiếu bằng chứng của chính beat.")
        if not isinstance(beat.get("headline"), str) or not 1 <= len(beat["headline"]) <= 90:
            raise BotError("Beat cần headline ≤90 ký tự.")
        beat["voiceover"] = " ".join(s["text"].strip() for s in sentences)
        if len(beat["voiceover"]) > 1800:
            raise BotError("Beat quá dài cho lời bình ngắn.")
    return data


class AuditCoverageError(BotError):
    """Incomplete reviewer output, distinct from a rejected script."""


def script_sentence_ids(script: dict) -> list[str]:
    return [f'{b["id"]}:{i}' for b in script["beats"] for i, _ in enumerate(b["sentences"])]


def validate_audit_coverage(data: dict, script: dict) -> dict:
    expected = set(script_sentence_ids(script))
    reviews = data.get("reviews") if isinstance(data, dict) else None
    if not isinstance(reviews, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in reviews):
        raise AuditCoverageError("Script audit phải kiểm tra mọi câu. reviews cần danh sách object có id dạng chuỗi.")
    ids = [r["id"] for r in reviews]
    actual = set(ids)
    if len(ids) != len(expected) or actual != expected:
        missing, unknown = sorted(expected - actual), sorted(actual - expected)
        duplicates = sorted({rid for rid in ids if ids.count(rid) > 1})
        details = "; ".join(label + ", ".join(values) for label, values in
                            (("Thiếu: ", missing), ("ID lạ: ", unknown), ("Trùng: ", duplicates)) if values)
        raise AuditCoverageError("Script audit phải kiểm tra mọi câu. " + details)
    return data


def validate_audit(data: dict, script: dict) -> dict:
    validate_audit_coverage(data, script)
    reviews = data["reviews"]
    if data.get("title_supported") is not True or data.get("style_ok") is not True:
        raise BotError("Tiêu đề hoặc phong cách kịch bản chưa đạt audit.")
    if any(r.get("supported") is not True or not r.get("reason") for r in reviews):
        raise BotError("Kịch bản có câu không có bằng chứng; dừng trước TTS/render.")
    return data
