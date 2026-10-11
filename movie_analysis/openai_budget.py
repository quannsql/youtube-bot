"""Local daily allocation across movie jobs; does not claim account billing/quota access."""
from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from youtube_shorts_bot import BotError
from .core import job_lock, read_json, write_json

GROUPS = {
    "main": ("gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o", "o1", "o3"),
    "mini": ("gpt-5.4-mini", "gpt-5.4-nano", "gpt-5-mini", "gpt-5-nano", "gpt-4.1-mini",
             "gpt-4.1-nano", "gpt-4o-mini", "o3-mini", "o4-mini"),
}


def model_group(model):
    for group, models in GROUPS.items():
        if any(model == base or re.fullmatch(re.escape(base) + r"-\d{4}-\d{2}-\d{2}", model) for base in models):
            return group
    return None


class OpenAIDailyBudget:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "usage.json"
        self.limits = {}
        for group in GROUPS:
            raw = os.getenv("MOVIE_OPENAI_" + group.upper() + "_DAILY_TOKEN_LIMIT", "0")
            try:
                limit = int(raw)
            except ValueError as exc:
                raise BotError("Giới hạn token OpenAI mỗi ngày cần số nguyên không âm.") from exc
            if limit < 0:
                raise BotError("Giới hạn token OpenAI mỗi ngày cần số nguyên không âm.")
            self.limits[group] = limit

    def reserve(self, model, tokens, stage, job):
        group = model_group(model)
        if not any(self.limits.values()):
            return None
        if group is None or not self.limits[group]:
            raise BotError("Model chưa được cấp ngân sách OpenAI trong ngày; không tự gọi model ngoài nhóm đã cấu hình.")
        day = datetime.now(UTC).date().isoformat()
        receipt = {"id": uuid.uuid4().hex, "day": day, "group": group}
        with job_lock(self.directory):
            data = read_json(self.path, {"days": {}, "scope": "local movie calls only; not account quota or invoice"})
            records = data["days"].setdefault(day, [])
            used = sum(r["charged_tokens"] for r in records if r["group"] == group)
            if used + tokens > self.limits[group]:
                raise BotError(f"Ngân sách OpenAI nhóm {group} hôm nay không đủ: đã ghi {used}, "
                               f"cần dự trữ {tokens}, trần {self.limits[group]}. Cache được giữ; reset 07:00 Việt Nam. "
                               "Kiểm tra Usage/Costs; sổ này không biết usage từ ứng dụng khác.")
            records.append({**receipt, "model": model, "stage": stage, "job": str(job),
                            "charged_tokens": tokens, "status": "reserved"})
            write_json(self.path, data)
        return receipt

    def settle(self, receipt, status, actual_tokens=None):
        if receipt is None:
            return
        with job_lock(self.directory):
            data = read_json(self.path)
            record = next(r for r in data["days"][receipt["day"]] if r["id"] == receipt["id"])
            record["status"] = status
            if type(actual_tokens) is int and actual_tokens > 0:
                record["charged_tokens"] = actual_tokens
            # Keep conservative reservations if no actual usage was received.
            write_json(self.path, data)
