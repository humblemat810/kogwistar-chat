"""Manual end-to-end API smoke test.

This script exercises the same flow the frontend uses:
auth -> create conversation -> submit a turn -> stream run events -> cancel.

It also saves every streamed event to disk so you can inspect the trace after
the run finishes. The script prints both the Windows path and a file URI so the
output is easy to click from a terminal or editor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.manual

BASE = "http://localhost:28110"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _enable_live_console() -> None:
    """Force stdout/stderr to flush line-by-line when the runtime supports it."""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)


def _default_trace_dir() -> Path:
    return Path(__file__).resolve().parent / "artifacts"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def main() -> int:
    _enable_live_console()

    parser = argparse.ArgumentParser(description="GraphRAG API smoke test with event tracing.")
    parser.add_argument("--base-url", default=BASE, help="GraphRAG backend base URL")
    parser.add_argument(
        "--trace-dir",
        default=str(_default_trace_dir()),
        help="Directory where streamed events and summary files will be written",
    )
    parser.add_argument(
        "--message",
        default="Hello, explain how graphrag works.",
        help="User message to submit for the test run",
    )
    parser.add_argument(
        "--no-cancel",
        action="store_true",
        help="Do not cancel the run after a few streamed events",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only the one-line summary for each streamed event",
    )
    args = parser.parse_args()

    trace_dir = Path(args.trace_dir).expanduser().resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_base = trace_dir / f"test_api-trace-{_utc_stamp()}"
    trace_jsonl = trace_base.with_suffix(".jsonl")
    trace_summary = trace_base.with_suffix(".summary.json")

    print(f"Connecting to {args.base_url}...")
    print(f"Trace file: {trace_jsonl}")
    print(f"Trace URI : {trace_jsonl.as_uri()}")

    events: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "base_url": args.base_url,
        "trace_jsonl": str(trace_jsonl),
        "conversation_id": None,
        "run_id": None,
        "event_count": 0,
        "has_thought": False,
        "terminal_event": None,
        "cancel_requested": False,
        "status": "started",
    }

    async with httpx.AsyncClient(base_url=args.base_url, timeout=60) as c:
        # 1. Get a token with RW access.
        try:
            r = await c.post("/auth/dev-token", json={"username": "test-user", "role": "rw", "ns": "conversation"})
            if r.status_code != 200:
                print(f"ERROR: Token generation failed: {r.status_code} {r.text}")
                summary["status"] = "token_failed"
                _write_jsonl(trace_jsonl, events)
                trace_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
                return 1
            token = r.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            print("OK: Token acquired (role=rw)")
        except Exception as e:
            print(f"ERROR: Connection failed: {e}")
            summary["status"] = "connection_failed"
            _write_jsonl(trace_jsonl, events)
            trace_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            return 1

        # 2. Create conversation.
        r2 = await c.post("/api/conversations", headers=headers, json={"user_id": "test-user"})
        if r2.status_code != 200:
            print(f"ERROR: Create conversation failed: {r2.status_code} {r2.text}")
            summary["status"] = "create_conversation_failed"
            _write_jsonl(trace_jsonl, events)
            trace_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            return 1

        data2 = r2.json()
        conv_id = data2.get("conversation_id") or data2.get("id")
        if not conv_id:
            print(f"ERROR: Response missing ID: {data2}")
            summary["status"] = "missing_conversation_id"
            _write_jsonl(trace_jsonl, events)
            trace_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            return 1
        summary["conversation_id"] = conv_id
        print(f"OK: Conversation: {conv_id}")

        # 3. Submit a turn.
        r3 = await c.post(
            f"/api/conversations/{conv_id}/turns:answer",
            headers=headers,
            json={"text": args.message},
        )

        if r3.status_code != 202:
            print(f"ERROR: Submit failed: {r3.status_code} {r3.text}")
            summary["status"] = "submit_failed"
            _write_jsonl(trace_jsonl, events)
            trace_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            return 1

        run_id = r3.json()["run_id"]
        summary["run_id"] = run_id
        print(f"OK: Run ID: {run_id}")

        # 4. Read run metadata.
        r4 = await c.get(f"/api/runs/{run_id}", headers=headers)
        print(f"OK: Run Metadata ({r4.status_code}): status={r4.json().get('status')}")

        # 5. Stream events and persist each one.
        print(f"Streaming events from /api/runs/{run_id}/events ...")
        async with c.stream("GET", f"/api/runs/{run_id}/events", headers=headers) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue

                raw_data = line[6:]
                received_at = datetime.now(timezone.utc).isoformat()
                try:
                    data = json.loads(raw_data)
                    event_type = data.get("event_type")
                    seq = data.get("seq")
                    events.append(
                        {
                            "received_at": received_at,
                            "seq": seq,
                            "event_type": event_type,
                            "data": data,
                            "raw": raw_data,
                        }
                    )
                    summary["event_count"] += 1
                    if event_type == "reasoning.summary":
                        summary["has_thought"] = True

                    if summary["event_count"] <= 3:
                        print(f"  Event {summary['event_count']}: type={event_type}")

                    # Print a compact live summary for every event as it arrives.
                    print(
                        f"[event seq={seq} type={event_type}] "
                        f"{json.dumps(data, ensure_ascii=False, separators=(',', ':'))}"
                    )
                    if not args.summary_only:
                        print(json.dumps(data, indent=2, ensure_ascii=False))

                    # Cancel after a few events so we can test the terminal flow.
                    if (
                        not args.no_cancel
                        and summary["event_count"] == 3
                        and event_type not in {"run.completed", "run.failed", "run.cancelled"}
                    ):
                        print("  ! Requesting cancellation...")
                        r_cancel = await c.post(f"/api/runs/{run_id}/cancel", headers=headers)
                        summary["cancel_requested"] = True
                        print(f"    Cancel response: {r_cancel.status_code}")

                    if event_type in {"run.completed", "run.failed", "run.cancelled"}:
                        summary["terminal_event"] = event_type
                        print(f"OK: Terminal event received: {event_type}")
                        break
                except Exception as e:
                    events.append(
                        {
                            "received_at": received_at,
                            "seq": None,
                            "event_type": "parse_error",
                            "error": str(e),
                            "raw": raw_data,
                        }
                    )
                    print(f"  Error parsing SSE line: {line}")

    summary["status"] = "ok"
    _write_jsonl(trace_jsonl, events)
    trace_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"OK: Total events received: {summary['event_count']}")
    print(f"OK: Reasoning/Thought captured: {summary['has_thought']}")
    print(f"Trace saved to: {trace_jsonl}")
    print(f"Trace link: {trace_jsonl.as_uri()}")
    print(f"Summary saved to: {trace_summary}")
    print(f"Summary link: {trace_summary.as_uri()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
