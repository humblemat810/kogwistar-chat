from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
import httpx

# Keep the repo root importable when pytest executes from the tests directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph_api as graph_api_mod
from graph_api import GraphAPI

pytestmark = pytest.mark.ci


class _FakeEventSource:
    def __init__(self, events):
        self._events = list(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def aiter_sse(self):
        async def iterator():
            for event in self._events:
                yield event

        return iterator()


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake-response"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self):
        return self._payload


def test_stream_events_prefers_httpx_sse(monkeypatch):
    api = GraphAPI("http://example.com")

    fake_events = [
        SimpleNamespace(id="7", event="run.stage", data='{"stage":"build_context","event_type":"run.stage"}'),
    ]

    def fake_aconnect_sse(*_args, **_kwargs):
        return _FakeEventSource(fake_events)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("custom parser should not be used when httpx-sse succeeds")

    monkeypatch.setattr(graph_api_mod, "aconnect_sse", fake_aconnect_sse)
    monkeypatch.setattr(api, "_stream_events_custom", fail_if_called)

    async def collect():
        try:
            return [event async for event in api.stream_events("token", "run-1")]
        finally:
            await api.close()

    events = asyncio.run(collect())

    assert len(events) == 1
    assert events[0]["seq"] == "7"
    assert events[0]["event_type"] == "run.stage"
    assert events[0]["stage"] == "build_context"


def test_stream_events_falls_back_to_custom_parser(monkeypatch):
    api = GraphAPI("http://example.com")

    def failing_aconnect_sse(*_args, **_kwargs):
        raise RuntimeError("httpx-sse failed")

    async def custom_parser(*_args, **_kwargs):
        yield {"seq": "9", "event_type": "run.completed", "status": "succeeded"}

    monkeypatch.setattr(graph_api_mod, "aconnect_sse", failing_aconnect_sse)
    monkeypatch.setattr(api, "_stream_events_custom", custom_parser)

    async def collect():
        try:
            return [event async for event in api.stream_events("token", "run-2")]
        finally:
            await api.close()

    events = asyncio.run(collect())

    assert len(events) == 1
    assert events[0]["seq"] == "9"
    assert events[0]["event_type"] == "run.completed"
    assert events[0]["status"] == "succeeded"


def test_stream_events_does_not_fallback_on_http_error(monkeypatch):
    api = GraphAPI("http://example.com")

    async def failing_httpx(*_args, **_kwargs):
        request = httpx.Request("GET", "http://example.com/api/runs/run-3/events")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)
        yield  # pragma: no cover

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("HTTP errors must not trigger SSE parser fallback")
        yield  # pragma: no cover

    monkeypatch.setattr(api, "_stream_events_httpx_sse", failing_httpx)
    monkeypatch.setattr(api, "_stream_events_custom", should_not_run)

    async def collect():
        try:
            return [event async for event in api.stream_events("token", "run-3")]
        finally:
            await api.close()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(collect())


def test_list_conversations_rejects_bad_payload(monkeypatch):
    api = GraphAPI("http://example.com")

    async def fake_get(*_args, **_kwargs):
        return _FakeResponse({"conversations": "not-a-list"})

    monkeypatch.setattr(api.client, "get", fake_get)

    async def collect():
        try:
            return await api.list_conversations("token")
        finally:
            await api.close()

    with pytest.raises(TypeError, match="response.conversations must be a JSON array"):
        asyncio.run(collect())


def test_get_run_events_rejects_bad_event_items(monkeypatch):
    api = GraphAPI("http://example.com")

    async def fake_get(*_args, **_kwargs):
        return _FakeResponse({"events": ["not-a-dict"]})

    monkeypatch.setattr(api.client, "get", fake_get)

    async def collect():
        try:
            return await api.get_run_events("token", "run-1")
        finally:
            await api.close()

    with pytest.raises(TypeError, match="response.events\\[0\\] must be a JSON object"):
        asyncio.run(collect())


def test_submit_turn_uses_default_chat_workflow(monkeypatch):
    api = GraphAPI("http://example.com")
    captured: dict[str, object] = {}

    async def fake_post(path, **kwargs):
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return _FakeResponse({"run_id": "run-default-workflow"}, status_code=202)

    monkeypatch.setattr(api.client, "post", fake_post)
    monkeypatch.setattr(graph_api_mod, "DEFAULT_CHAT_WORKFLOW_ID", "debug.rag.v1")

    async def submit():
        try:
            return await api.submit_turn("token", "conv-1", "hello", user_id="user-1")
        finally:
            await api.close()

    assert asyncio.run(submit()) == "run-default-workflow"
    assert captured["path"] == "/api/conversations/conv-1/turns:answer"
    assert captured["json"] == {
        "text": "hello",
        "user_id": "user-1",
        "workflow_id": "debug.rag.v1",
    }
