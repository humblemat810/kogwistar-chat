from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:28110")


class GraphAPI:
    def __init__(self, base_url: str = SERVER_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def get_me(self, token: str) -> dict[str, Any]:
        response = await self.client.get("/api/auth/me", headers=self._headers(token))
        response.raise_for_status()
        return response.json()

    async def create_conversation(self, token: str, user_id: str) -> str:
        response = await self.client.post(
            "/api/conversations",
            headers=self._headers(token),
            json={"user_id": user_id},
        )
        if response.status_code != 200:
            logging.error("create_conversation failed: %s — %s", response.status_code, response.text)
        response.raise_for_status()
        return str(response.json()["conversation_id"])

    async def get_transcript(self, token: str, conversation_id: str) -> list[dict[str, Any]]:
        response = await self.client.get(f"/api/conversations/{conversation_id}/turns", headers=self._headers(token))
        if response.status_code == 200:
            return list(response.json().get("turns") or [])
        return []

    async def submit_turn(self, token: str, conversation_id: str, text: str) -> str:
        response = await self.client.post(
            f"/api/conversations/{conversation_id}/turns:answer",
            headers=self._headers(token),
            json={"text": text},
        )
        if response.status_code != 202:
            logging.error("submit_turn failed: %s — %s", response.status_code, response.text)
        response.raise_for_status()
        return str(response.json()["run_id"])

    async def get_run(self, token: str, run_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/runs/{run_id}", headers=self._headers(token))
        response.raise_for_status()
        return response.json()

    async def get_run_events(self, token: str, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"/api/runs/{run_id}/events/poll",
            headers=self._headers(token),
            params={"after_seq": int(after_seq or 0)},
        )
        if response.status_code != 200:
            logging.error("get_run_events failed: %s — %s", response.status_code, response.text)
        response.raise_for_status()
        return list(response.json().get("events") or [])

    async def close(self) -> None:
        await self.client.aclose()
