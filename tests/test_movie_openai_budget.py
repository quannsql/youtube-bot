import json
from datetime import UTC, datetime

import pytest
import youtube_shorts_bot as legacy
from movie_analysis.core import MovieConfig, read_json
from movie_analysis.openai_budget import OpenAIDailyBudget, model_group
from movie_analysis.openai_writer import OpenAIWriterClient
from movie_analysis.provider import GeminiVideoClient
from test_movie_analysis import Response, script


def configure(monkeypatch, main=1000, mini=2000):
    monkeypatch.setenv("MOVIE_OPENAI_MAIN_DAILY_TOKEN_LIMIT", str(main))
    monkeypatch.setenv("MOVIE_OPENAI_MINI_DAILY_TOKEN_LIMIT", str(mini))


def test_groups_do_not_mix_snapshot_mini_or_unlisted_models():
    assert model_group("gpt-5.4") == model_group("gpt-5.4-2026-03-05") == "main"
    assert model_group("gpt-5.4-mini") == model_group("gpt-5.4-mini-2026-03-17") == "mini"
    assert model_group("gpt-5.4-pro") is None


def test_shared_allocation_reserves_before_request_and_replaces_with_actual(tmp_path, monkeypatch):
    configure(monkeypatch)
    one = OpenAIDailyBudget(tmp_path)
    first = one.reserve("gpt-5.4", 900, "script", "job1")
    two = OpenAIDailyBudget(tmp_path)
    with pytest.raises(legacy.BotError, match="không đủ"):
        two.reserve("gpt-4o", 101, "script", "job2")
    one.settle(first, "received", 300)
    assert two.reserve("gpt-4o", 700, "script", "job2")
    assert two.reserve("gpt-5.4-mini", 2000, "script", "job3")  # separate group
    with pytest.raises(legacy.BotError):
        two.reserve("gpt-5.4-pro", 1, "script", "job4")


def test_network_failure_keeps_reservation_and_utc_reset_preserves_old_receipts(tmp_path, monkeypatch):
    configure(monkeypatch)
    class Clock:
        day = datetime(2026, 10, 11, 23, 59, tzinfo=UTC)
        @classmethod
        def now(cls, _tz):
            return cls.day
    monkeypatch.setattr("movie_analysis.openai_budget.datetime", Clock)
    budget = OpenAIDailyBudget(tmp_path)
    receipt = budget.reserve("gpt-5.4", 1000, "script", "job1")
    budget.settle(receipt, "network_error")
    with pytest.raises(legacy.BotError):
        budget.reserve("gpt-5.4", 1, "script", "job2")
    Clock.day = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
    assert budget.reserve("gpt-5.4", 1000, "script", "job3")
    budget.settle(receipt, "late_received", 900)
    assert read_json(budget.path)["days"]["2026-10-11"][0]["charged_tokens"] == 900


def test_client_checks_daily_budget_before_http_and_other_jobs_share_it(tmp_path, monkeypatch):
    configure(monkeypatch, 30000, 0)
    monkeypatch.setattr(legacy, "DATA_DIR", tmp_path)
    class HTTP:
        calls = 0
        def request(self, *_args, **_kwargs):
            self.calls += 1
            return Response(200, {"status": "completed", "usage": {"total_tokens": 25000},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(script())}]}]})
    http = HTTP()
    cfg = MovieConfig()
    first = GeminiVideoClient("fake-key", cfg, tmp_path / "one")
    second = GeminiVideoClient("fake-key", cfg, tmp_path / "two")
    assert OpenAIWriterClient("fake-key", cfg, first, "gpt-5.4", http).generate("script", "JSON")
    with pytest.raises(legacy.BotError, match="Ngân sách"):
        OpenAIWriterClient("fake-key", cfg, second, "gpt-5.4", http).generate("script", "JSON")
    assert http.calls == 1 and second.usage["calls"] == []


@pytest.mark.parametrize("bad", ["-1", "garbage"])
def test_bad_daily_limits_fail_before_network(tmp_path, monkeypatch, bad):
    configure(monkeypatch)
    monkeypatch.setenv("MOVIE_OPENAI_MAIN_DAILY_TOKEN_LIMIT", bad)
    with pytest.raises(legacy.BotError):
        OpenAIDailyBudget(tmp_path)
