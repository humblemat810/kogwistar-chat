"""Test the full chat flow: auth → create conv → submit turn → stream events."""
import httpx
import asyncio

BASE = "http://localhost:28110"

async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        # 1. Get token
        r = await c.get("/api/auth/login", params={"redirect_uri": "http://localhost:5001/"}, follow_redirects=False)
        token = r.headers["location"].split("token=")[1]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"✓ Token acquired")

        # 2. Create conversation
        r2 = await c.post("/api/conversations", headers=headers, json={"user_id": "dev-user-id"})
        conv_id = r2.json()["conversation_id"]
        print(f"✓ Conversation: {conv_id}")

        # 3. Submit a turn (correct URL: turns:answer)
        r3 = await c.post(
            f"/api/conversations/{conv_id}/turns:answer",
            headers=headers,
            json={"text": "Hello, what can you do?"}
        )
        print(f"  Submit turn → {r3.status_code}: {r3.text[:200]}")
        
        if r3.status_code == 202:
            run_id = r3.json()["run_id"]
            print(f"✓ Run ID: {run_id}")

            # 4. Stream events (correct URL: /api/runs/{run_id}/events)
            print(f"  Streaming events from /api/runs/{run_id}/events ...")
            event_count = 0
            async with c.stream("GET", f"/api/runs/{run_id}/events", headers=headers) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        event_count += 1
                        if event_count <= 5:
                            print(f"    Event {event_count}: {line[:120]}...")
                        elif event_count == 6:
                            print(f"    ... (streaming more events)")
            print(f"✓ Total events received: {event_count}")
        else:
            print(f"✗ Submit failed: {r3.text}")

asyncio.run(main())
