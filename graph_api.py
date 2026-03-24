from __future__ import annotations

"""HTTP client wrapper for the GraphRAG backend.

The rest of the app should not care about raw HTTP details, status codes, or
endpoint paths. This class centralizes those concerns so the route handlers in
`main.py` stay readable.
"""

import json
import logging
import os
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:28110")
LOG = logging.getLogger("htmx.graph_api")


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

    async def get_dev_token(self, role: str = "ro", ns: str | list[str] = "docs") -> dict[str, Any]:
        """Mint a developer token for testing (dev-only endpoint)."""

        payload = {"role": role, "ns": ns, "username": "dev_user"}
        headers = {"Content-Type": "application/json"}
        LOG.info("Requesting dev token from %s/auth/dev-token with payload: %s", self.base_url, payload)
        
        try:
            response = await self.client.post("/auth/dev-token", json=payload, headers=headers)
            if response.status_code != 200:
                LOG.error("get_dev_token failed: status=%s body=%s", response.status_code, response.text)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            LOG.exception("Exception during get_dev_token call")
            raise

    async def get_me(self, token: str) -> dict[str, Any]:
        """Fetch the currently authenticated user profile."""

        response = await self.client.get("/api/auth/me", headers=self._headers(token))
        response.raise_for_status()
        return response.json()

    async def list_conversations(self, token: str, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return the conversation list for the current user.

        `user_id` is accepted because the frontend already tracks it in the
        session, but the backend authorizes the request using the bearer token.
        """

        response = await self.client.get("/api/conversations", headers=self._headers(token))
        if response.status_code != 200:
            LOG.error("list_conversations failed: %s - %s", response.status_code, response.text)
        response.raise_for_status()
        return list(response.json().get("conversations") or [])

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
        return str(response.json()["conversation_id"])

    async def get_transcript(self, token: str, conversation_id: str) -> list[dict[str, Any]]:
        """Load the conversation history so the chat window can re-render it."""

        response = await self.client.get(f"/api/conversations/{conversation_id}/turns", headers=self._headers(token))
        if response.status_code == 200:
            return list(response.json().get("turns") or [])
        return []

    async def submit_turn(self, token: str, conversation_id: str, text: str, user_id: str | None = None) -> str:
        """Submit one user message and receive the run ID for streaming."""

        LOG.debug("Submitting turn: conv_id=%s, user_id=%s, text_len=%s", conversation_id, user_id, len(text))
        payload = {"text": text}
        if user_id:
            payload["user_id"] = user_id

        response = await self.client.post(
            f"/api/conversations/{conversation_id}/turns:answer",
            headers=self._headers(token),
            json=payload,
        )
        if response.status_code != 202:
            LOG.error("submit_turn failed: %s - %s", response.status_code, response.text)
        response.raise_for_status()
        run_id = str(response.json()["run_id"])
        LOG.info("turn submitted: run_id=%s", run_id)
        return run_id

    async def get_run(self, token: str, run_id: str) -> dict[str, Any]:
        """Fetch run metadata, usually as a fallback when events are delayed."""

        response = await self.client.get(f"/api/runs/{run_id}", headers=self._headers(token))
        response.raise_for_status()
        return response.json()

    async def get_run_events(self, token: str, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
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
        return list(response.json().get("events") or [])

    async def stream_events(self, token: str, run_id: str, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Stream SSE events from the backend.

        This method hides the `httpx.stream(...)` boilerplate and yields decoded
        JSON payloads one-by-one.
        """

        headers = self._headers(token)
        url = f"/api/runs/{run_id}/events"
        params = {"after_seq": int(after_seq or 0)}

        async with self.client.stream("GET", url, headers=headers, params=params) as response:
            if response.status_code != 200:
                body = await response.aread()
                LOG.error("stream_events failed: %s - %s", response.status_code, body.decode("utf-8", errors="replace"))
                response.raise_for_status()

            async for line in response.aiter_lines():
                # SSE lines arrive as `data: {...}`. We only care about data lines
                # because the backend currently uses JSON payloads there.
                if line.startswith("data: "):
                    try:
                        event_data = json.loads(line[6:])
                        yield event_data
                    except json.JSONDecodeError:
                        LOG.error("Failed to parse SSE event: %s", line)
                        continue

    async def cancel_run(self, token: str, run_id: str) -> bool:
        """Request cancellation of a running job."""

        headers = self._headers(token)
        response = await self.client.post(f"/api/runs/{run_id}/cancel", headers=headers)
        return response.status_code == 202

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""

        await self.client.aclose()
