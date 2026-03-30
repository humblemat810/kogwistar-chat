from __future__ import annotations

"""HTTP client wrapper for the GraphRAG backend.

The rest of the app should not care about raw HTTP details, status codes, or
endpoint paths. This class centralizes those concerns so the route handlers in
`main.py` stay readable.
"""

import json
import logging
import os
import time
from typing import Any, AsyncIterator, cast

import httpx
from dotenv import load_dotenv

from app_contracts import (
    AuthTokenResponse,
    ConversationListResponse,
    ConversationSummary,
    ConversationTurn,
    CreateConversationResponse,
    RunEventsResponse,
    RunResponse,
    StreamEventPayload,
    SubmitTurnResponse,
    TranscriptResponse,
    UserProfileResponse,
    normalize_conversation_summary,
    normalize_conversation_turn,
    normalize_run_event,
    require_list,
    require_mapping,
    require_str,
)

try:
    from httpx_sse import aconnect_sse
except Exception:  # pragma: no cover - optional dependency
    aconnect_sse = None

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:28110")
LOG = logging.getLogger("htmx.graph_api")


def _trace_sse_graph(stage: str, *, run_id: str, **fields: object) -> None:
    """Emit a noisy but grep-friendly SSE trace line for backend stream timing."""

    stamp_ms = int(time.time() * 1000)
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    line = f"[SSE GRAPH] t_ms={stamp_ms} stage={stage} run_id={run_id}"
    if extras:
        line = f"{line} {extras}"
    LOG.info(line)
    print(line, flush=True)


class GraphAPI:
    """Thin async client for the GraphRAG API.

    This is intentionally not a full SDK. Each method maps 1:1 to the endpoint
    used by the frontend. That makes the code easy to audit when the backend
    contract changes.
    """

    def __init__(self, base_url: str = SERVER_URL):
        """Create a reusable HTTP client.

        We keep a single `httpx.AsyncClient` so connection pooling works across
        requests.
        """

        self.base_url = base_url.rstrip("/")
        LOG.info("Initializing GraphAPI with base_url: %s", self.base_url)
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        """Build the auth header expected by the backend."""

        return {"Authorization": f"Bearer {token}"}

    async def get_dev_token(self, role: str = "ro", ns: str | list[str] = "docs") -> AuthTokenResponse:
        """Mint a developer token for testing (dev-only endpoint)."""

        payload = {"role": role, "ns": ns, "username": "dev_user"}
        headers = {"Content-Type": "application/json"}
        LOG.info("Requesting dev token from %s/auth/dev-token with payload: %s", self.base_url, payload)
        
        try:
            response = await self.client.post("/auth/dev-token", json=payload, headers=headers)
            if response.status_code != 200:
                LOG.error("get_dev_token failed: status=%s body=%s", response.status_code, response.text)
            response.raise_for_status()
            return cast(AuthTokenResponse, require_mapping(response.json(), context="get_dev_token response"))
        except Exception as exc:
            LOG.exception("Exception during get_dev_token call")
            raise

    async def get_me(self, token: str) -> UserProfileResponse:
        """Fetch the currently authenticated user profile."""

        response = await self.client.get("/api/auth/me", headers=self._headers(token))
        response.raise_for_status()
        return cast(UserProfileResponse, require_mapping(response.json(), context="get_me response"))

    async def list_conversations(self, token: str, user_id: str | None = None) -> list[ConversationSummary]:
        """Return the conversation list for the current user.

        `user_id` is accepted because the frontend already tracks it in the
        session, but the backend authorizes the request using the bearer token.
        """

        response = await self.client.get("/api/conversations", headers=self._headers(token))
        if response.status_code != 200:
            LOG.error("list_conversations failed: %s - %s", response.status_code, response.text)
        response.raise_for_status()
        payload = cast(ConversationListResponse, require_mapping(response.json(), context="list_conversations response"))
        conversations = require_list(payload.get("conversations"), context="list_conversations response.conversations")
        return [normalize_conversation_summary(item, context=f"list_conversations response.conversations[{idx}]") for idx, item in enumerate(conversations)]

    async def create_conversation(self, token: str, user_id: str) -> str:
        """Create a fresh conversation and return its ID."""

        response = await self.client.post(
            "/api/conversations",
            headers=self._headers(token),
            json={"user_id": user_id},
        )
        if response.status_code != 200:
            LOG.error("create_conversation failed: %s - %s", response.status_code, response.text)
        response.raise_for_status()
        payload = cast(CreateConversationResponse, require_mapping(response.json(), context="create_conversation response"))
        return require_str(payload.get("conversation_id"), context="create_conversation response.conversation_id")

    async def get_transcript(self, token: str, conversation_id: str) -> list[ConversationTurn]:
        """Load the conversation history so the chat window can re-render it."""

        response = await self.client.get(f"/api/conversations/{conversation_id}/turns", headers=self._headers(token))
        if response.status_code == 200:
            payload = cast(TranscriptResponse, require_mapping(response.json(), context="get_transcript response"))
            turns = require_list(payload.get("turns"), context="get_transcript response.turns")
            return [normalize_conversation_turn(item, context=f"get_transcript response.turns[{idx}]") for idx, item in enumerate(turns)]
        return []

    async def submit_turn(
        self,
        token: str,
        conversation_id: str,
        text: str,
        user_id: str | None = None,
        workflow_id: str | None = None,
    ) -> str:
        """Submit one user message and receive the run ID for streaming."""

        LOG.debug("Submitting turn: conv_id=%s, user_id=%s, text_len=%s", conversation_id, user_id, len(text))
        payload = {"text": text}
        if user_id:
            payload["user_id"] = user_id
        if workflow_id:
            payload["workflow_id"] = workflow_id

        response = await self.client.post(
            f"/api/conversations/{conversation_id}/turns:answer",
            headers=self._headers(token),
            json=payload,
        )
        if response.status_code != 202:
            LOG.error("submit_turn failed: %s - %s", response.status_code, response.text)
        response.raise_for_status()
        payload = cast(SubmitTurnResponse, require_mapping(response.json(), context="submit_turn response"))
        run_id = require_str(payload.get("run_id"), context="submit_turn response.run_id")
        LOG.info("turn submitted: run_id=%s", run_id)
        return run_id

    async def get_run(self, token: str, run_id: str) -> RunResponse:
        """Fetch run metadata, usually as a fallback when events are delayed."""

        response = await self.client.get(f"/api/runs/{run_id}", headers=self._headers(token))
        response.raise_for_status()
        return cast(RunResponse, require_mapping(response.json(), context="get_run response"))

    async def get_run_events(self, token: str, run_id: str, after_seq: int = 0) -> list[StreamEventPayload]:
        """Poll the backend for a batch of run events.

        Polling is the fallback mode when SSE is not available or not desired.
        The caller passes `after_seq` so it only receives events it has not seen.
        """

        response = await self.client.get(
            f"/api/runs/{run_id}/events/poll",
            headers=self._headers(token),
            params={"after_seq": int(after_seq or 0)},
        )
        if response.status_code != 200:
            LOG.error("get_run_events failed: %s - %s", response.status_code, response.text)
        response.raise_for_status()
        payload = cast(RunEventsResponse, require_mapping(response.json(), context="get_run_events response"))
        events = require_list(payload.get("events"), context="get_run_events response.events")
        return [
            cast(StreamEventPayload, normalize_run_event(item, context=f"get_run_events response.events[{idx}]"))
            for idx, item in enumerate(events)
        ]

    async def stream_events(self, token: str, run_id: str, after_seq: int = 0) -> AsyncIterator[StreamEventPayload]:
        """Stream SSE events from the backend.

        This method hides the `httpx.stream(...)` boilerplate and yields
        normalized event dictionaries one-by-one.
        """

        headers = self._headers(token)
        url = f"/api/runs/{run_id}/events"
        params = {"after_seq": int(after_seq or 0)}
        started_at = time.perf_counter()
        _trace_sse_graph("stream-open", run_id=run_id, after_seq=params["after_seq"], url=url)

        try:
            async for event in self._stream_events_httpx_sse(headers=headers, url=url, params=params):
                _trace_sse_graph(
                    "backend-received",
                    run_id=run_id,
                    transport="httpx-sse",
                    seq=event.get("seq"),
                    event=event.get("event_type") or event.get("type"),
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )
                yield event
            _trace_sse_graph("stream-complete", run_id=run_id, transport="httpx-sse", elapsed_ms=int((time.perf_counter() - started_at) * 1000))
            return
        except Exception as exc:
            _trace_sse_graph("stream-fallback", run_id=run_id, transport="httpx-sse", error=repr(exc))
            LOG.warning("httpx-sse stream failed for run_id=%s, falling back to custom parser: %s", run_id, exc)

        async for event in self._stream_events_custom(headers=headers, url=url, params=params):
            _trace_sse_graph(
                "backend-received",
                run_id=run_id,
                transport="custom",
                seq=event.get("seq"),
                event=event.get("event_type") or event.get("type"),
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            )
            yield event
        _trace_sse_graph("stream-complete", run_id=run_id, transport="custom", elapsed_ms=int((time.perf_counter() - started_at) * 1000))

    @staticmethod
    def _parse_event_payload(raw_data: str) -> dict[str, Any]:
        """Turn an SSE data payload into a dictionary when possible."""

        if not raw_data:
            return {}

        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            return {"data": raw_data}

        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    async def _stream_events_httpx_sse(
        self,
        *,
        headers: dict[str, str],
        url: str,
        params: dict[str, int],
    ) -> AsyncIterator[StreamEventPayload]:
        """Stream events using the optional httpx-sse helper library."""

        if aconnect_sse is None:
            raise RuntimeError("httpx-sse is not installed")

        async with aconnect_sse(self.client, "GET", url, headers=headers, params=params) as event_source:
            async for event in event_source.aiter_sse():
                normalized: dict[str, Any] = {}

                if getattr(event, "id", None) is not None:
                    normalized["seq"] = event.id
                    normalized["sse_id"] = event.id
                if getattr(event, "event", None):
                    normalized["event_type"] = event.event

                payload = self._parse_event_payload(getattr(event, "data", ""))
                normalized.update(payload)
                if not normalized.get("event_type"):
                    normalized["event_type"] = "message"
                yield cast(StreamEventPayload, normalized)

    async def _stream_events_custom(
        self,
        *,
        headers: dict[str, str],
        url: str,
        params: dict[str, int],
    ) -> AsyncIterator[StreamEventPayload]:
        """Fallback SSE parser for servers or test environments without httpx-sse."""

        async with self.client.stream("GET", url, headers=headers, params=params) as response:
            if response.status_code != 200:
                body = await response.aread()
                LOG.error("stream_events failed: %s - %s", response.status_code, body.decode("utf-8", errors="replace"))
                response.raise_for_status()

            current: dict[str, Any] = {"data_lines": []}

            async for line in response.aiter_lines():
                if line == "":
                    if current.get("id") is None and current.get("event") is None and not current.get("data_lines"):
                        current = {"data_lines": []}
                        continue

                    raw_data = "\n".join(current.get("data_lines") or [])
                    event: dict[str, Any] = {}

                    if current.get("id") is not None:
                        event["seq"] = current["id"]
                        event["sse_id"] = current["id"]
                    if current.get("event") is not None:
                        event["event_type"] = current["event"]

                    event.update(self._parse_event_payload(raw_data))
                    if event:
                        yield cast(StreamEventPayload, event)

                    current = {"data_lines": []}
                    continue

                if line.startswith("id:"):
                    current["id"] = line[3:].strip()
                    continue

                if line.startswith("event:"):
                    current["event"] = line[6:].strip()
                    continue

                if line.startswith("data:"):
                    current.setdefault("data_lines", []).append(line[5:].lstrip())
                    continue

                if line.startswith(":"):
                    continue

            if current.get("id") is not None or current.get("event") is not None or current.get("data_lines"):
                raw_data = "\n".join(current.get("data_lines") or [])
                event: dict[str, Any] = {}
                if current.get("id") is not None:
                    event["seq"] = current["id"]
                    event["sse_id"] = current["id"]
                if current.get("event") is not None:
                    event["event_type"] = current["event"]
                event.update(self._parse_event_payload(raw_data))
                if event:
                    yield cast(StreamEventPayload, event)

    async def cancel_run(self, token: str, run_id: str) -> bool:
        """Request cancellation of a running job."""

        headers = self._headers(token)
        response = await self.client.post(f"/api/runs/{run_id}/cancel", headers=headers)
        return response.status_code == 202

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""

        await self.client.aclose()
