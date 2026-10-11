import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from google.genai import types

import youtube_shorts_bot as legacy
from movie_analysis.core import MovieConfig, read_json
from movie_analysis.live_provider import GeminiLiveVideoClient, LiveRateBudget, tool_result


@pytest.mark.parametrize("call", [NS(name="unknown", args={}), NS(name="submit_analysis", args={"json_text": "[]"}),
                                  NS(name="submit_analysis", args={"json_text": "bad"})])
def test_live_tool_rejects_unknown_or_invalid_result(call):
    with pytest.raises(legacy.BotError):
        tool_result(call)


def test_live_rate_guard_waits_and_rejects_oversize(tmp_path):
    clock = [1000.]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    budget = LiveRateBudget(tmp_path, 65000, now=lambda: clock[0], sleep=sleep)
    budget.reserve(30000)
    budget.reserve(30000)
    assert sum(waits) >= 65 and all(s <= 30 for s in waits)
    assert len(read_json(tmp_path / "window.json")) == 1
    with pytest.raises(legacy.BotError):
        budget.reserve(53000)


def test_live_configuration_caps_fps_without_changing_rest(monkeypatch):
    monkeypatch.setenv("MOVIE_VIDEO_PROVIDER", "gemini_live")
    monkeypatch.setenv("MOVIE_MODEL", "gemini-3.8-live")
    monkeypatch.setenv("MOVIE_VERIFIER_MODEL", "gemini-3.8-live")
    assert MovieConfig.from_env().recheck_fps <= 1
    monkeypatch.setenv("MOVIE_VIDEO_PROVIDER", "gemini")
    monkeypatch.setenv("MOVIE_RECHECK_FPS", "8")
    assert MovieConfig.from_env().recheck_fps == 8


class LiveSession:
    def __init__(self, no_tool=False):
        self.finished = asyncio.Event()
        self.responded = asyncio.Event()
        self.messages = []
        self.no_tool = no_tool
    async def send_client_content(self, **kwargs):
        self.messages.append(kwargs)
    async def send_realtime_input(self, **kwargs):
        self.messages.append(kwargs)
        if kwargs.get("activity_end") is not None:
            self.finished.set()
    async def send_tool_response(self, **kwargs):
        self.messages.append(kwargs)
        self.responded.set()
    async def receive(self):
        await self.finished.wait()
        if self.no_tool:
            yield types.LiveServerMessage(usage_metadata=types.UsageMetadata(total_token_count=300))
            yield types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))
        else:
            yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
                types.FunctionCall(name="submit_analysis", id="f1", args={"json_text": '{"summary":"ok"}'})]))
            await self.responded.wait()
            yield types.LiveServerMessage(usage_metadata=types.UsageMetadata(total_token_count=300))


class LiveSDK:
    def __init__(self, no_tool=False):
        self.session = LiveSession(no_tool)
        self.aio = NS(live=self)
    @asynccontextmanager
    async def connect(self, **kwargs):
        self.connection = kwargs
        yield self.session


class Budget:
    def reserve(self, tokens):
        self.tokens = tokens


@pytest.mark.parametrize("no_tool", [False, True])
def test_live_stream_sends_media_then_requires_tool_and_records_usage(tmp_path, monkeypatch, no_tool):
    cfg = replace(MovieConfig(), video_provider="gemini_live", model="gemini-3.8-live")
    sdk, budget = LiveSDK(no_tool), Budget()
    client = GeminiLiveVideoClient("test-secret", cfg, tmp_path, sdk_client=sdk, rate_budget=budget)
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"fake-image")
    monkeypatch.setattr(client, "_prepare", lambda *a: {"frames": [frame], "audio": bytes(1280), "start": 5, "seconds": .04, "fps": 1})
    if no_tool:
        with pytest.raises(legacy.BotError, match="submit_analysis"):
            client.generate("global", "JSON required")
    else:
        assert client.generate("global", "JSON required") == {"summary": "ok"}
        row = read_json(tmp_path / "usage.json")["calls"][0]
        assert row["provider"] == "gemini_live" and row["charged_tokens"] == 300
        assert row["status"] == "received" and row["frames"] == 1
    messages = sdk.session.messages
    assert messages[0]["turn_complete"] is False
    assert any("video" in m for m in messages) and any("audio" in m for m in messages)
    assert any("SOURCE_TIMESTAMP 5.000s" in m.get("text", "") for m in messages)
    end_label = next(i for i, m in enumerate(messages) if m.get("text", "").startswith("END_OF_VIDEO"))
    end_activity = next(i for i, m in enumerate(messages) if "activity_end" in m)
    assert end_label < end_activity
    config = sdk.connection["config"]
    assert config.response_modalities == [types.Modality.AUDIO]
    assert config.realtime_input_config.automatic_activity_detection.disabled
