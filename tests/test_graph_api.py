from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

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
