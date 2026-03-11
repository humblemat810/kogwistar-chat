import httpx
import json
import logging
import os
from typing import AsyncGenerator, Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:8000")

class GraphAPI:
    def __init__(self, base_url: str = SERVER_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def get_dev_token(self, username: str) -> str:
        """Obtains a JWT token from the dev-token endpoint."""
        response = await self.client.post("/auth/dev-token", params={"sub": username})
        response.raise_for_status()
        return response.json()["access_token"]

    async def create_conversation(self, token: str, user_id: str) -> str:
        """Creates a new conversation and returns the conversation_id."""
        headers = {"Authorization": f"Bearer {token}"}
        response = await self.client.post(
            "/api/conversations", 
            headers=headers,
            json={"user_id": user_id}
        )
        response.raise_for_status()
        return response.json()["conversation_id"]

    async def list_conversations(self, token: str, user_id: str) -> list:
        # Stub for the backend API we'll be adding soon
        # headers = {"Authorization": f"Bearer {token}"}
        # response = await self.client.get("/api/conversations", headers=headers)
        # if response.status_code == 200:
        #     return response.json().get("conversations", [])
        return []

    async def get_transcript(self, token: str, conversation_id: str) -> list:
        headers = {"Authorization": f"Bearer {token}"}
        response = await self.client.get(f"/api/conversations/{conversation_id}/turns", headers=headers)
        if response.status_code == 200:
            return response.json().get("turns", [])
        return []

    async def submit_turn(self, token: str, conversation_id: str, text: str) -> str:
        """Submits a chat turn and returns the run_id."""
        headers = {"Authorization": f"Bearer {token}"}
        response = await self.client.post(
            f"/api/conversations/{conversation_id}/turns",
            headers=headers,
            json={"text": text}
        )
        response.raise_for_status()
        return response.json()["run_id"]

    async def stream_events(self, token: str, conversation_id: str, run_id: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Streams SSE events for a specific run."""
        headers = {"Authorization": f"Bearer {token}"}
        url = f"/api/conversations/{conversation_id}/runs/{run_id}/events"
        
        async with self.client.stream("GET", url, headers=headers) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        event_data = json.loads(line[6:])
                        yield event_data
                    except json.JSONDecodeError:
                        logging.error(f"Failed to parse SSE event: {line}")
                        continue

    async def close(self):
        await self.client.aclose()
