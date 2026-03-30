"""Manual live API integration test.

This script exercises the same flow the frontend uses:
auth -> auth/me -> list conversations -> create conversation -> submit a turn -> stream run events.

It saves request/response metadata, raw SSE chunks, and parsed SSE events to
disk so you can inspect whether delivery was incremental or end-batched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.manual

BASE = os.getenv("LIVE_API_BASE_URL", "http://127.0.0.1:28110")
DEFAULT_NS = os.getenv("LIVE_API_NS", "docs,conversation,workflow,wisdom")
DEFAULT_STREAM_TIMEOUT_S = float(os.getenv("LIVE_SSE_TIMEOUT_S", "45"))
DEFAULT_MIN_SPREAD_MS = int(os.getenv("LIVE_SSE_MIN_SPREAD_MS", "150"))


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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _request_json(
    client: httpx.AsyncClient,
    trace_http: list[dict[str, Any]],
    method: str,
    path: str,
    *,
    expected_status: int | None = None,
    **kwargs: Any,
) -> httpx.Response:
    request_started = time.perf_counter()
    trace_http.append(
        {
            "direction": "request",
            "at": _now_iso(),
            "method": method.upper(),
            "path": path,
            "json": kwargs.get("json"),
            "params": kwargs.get("params"),
        }
    )
    response = await client.request(method, path, **kwargs)
    elapsed_ms = int((time.perf_counter() - request_started) * 1000)
    preview = response.text[:500]
    trace_http.append(
        {
            "direction": "response",
            "at": _now_iso(),
            "method": method.upper(),
            "path": path,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "text_preview": preview,
        }
    )
    if expected_status is not None and response.status_code != expected_status:
        raise AssertionError(f"{method.upper()} {path} expected {expected_status}, got {response.status_code}: {preview}")
    return response


async def _stream_sse_trace(
    client: httpx.AsyncClient,
    path: str,
    *,
    headers: dict[str, str],
    trace_http: list[dict[str, Any]],
    trace_chunks: list[dict[str, Any]],
    trace_events: list[dict[str, Any]],
    timeout_s: float,
) -> dict[str, Any]:
    stream_started = time.perf_counter()
    chunk_count = 0
    first_event_ms: int | None = None
    last_event_ms: int | None = None
    first_chunk_ms: int | None = None
    decoder = None
    try:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")()
    except Exception:  # pragma: no cover - extremely unlikely
        decoder = None

    current: dict[str, Any] = {"data_lines": []}
    buffer = ""

    def flush_current() -> dict[str, Any] | None:
        nonlocal current, first_event_ms, last_event_ms
        if current.get("id") is None and current.get("event") is None and not current.get("data_lines"):
            current = {"data_lines": []}
            return None

        raw_data = "\n".join(current.get("data_lines") or [])
        payload: dict[str, Any]
        try:
            parsed = json.loads(raw_data) if raw_data else {}
            payload = parsed if isinstance(parsed, dict) else {"data": parsed}
        except json.JSONDecodeError:
            payload = {"data": raw_data}

        received_monotonic_ms = int((time.perf_counter() - stream_started) * 1000)
        if first_event_ms is None:
            first_event_ms = received_monotonic_ms
        last_event_ms = received_monotonic_ms

        item = {
            "received_at": _now_iso(),
            "received_monotonic_ms": received_monotonic_ms,
            "seq": current.get("id") or payload.get("seq"),
            "event_type": current.get("event") or payload.get("event_type") or "message",
            "data": payload,
            "raw": raw_data,
        }
        trace_events.append(item)
        current = {"data_lines": []}
        return item

    trace_http.append({"direction": "request", "at": _now_iso(), "method": "GET", "path": path, "stream": True})
    async with client.stream("GET", path, headers=headers) as response:
        trace_http.append(
            {
                "direction": "response",
                "at": _now_iso(),
                "method": "GET",
                "path": path,
                "status_code": response.status_code,
                "headers": dict(response.headers),
            }
        )
        if response.status_code != 200:
            body = await response.aread()
            raise AssertionError(f"GET {path} expected 200, got {response.status_code}: {body.decode('utf-8', errors='replace')}")

        async with asyncio.timeout(timeout_s):
            async for chunk in response.aiter_bytes():
                chunk_count += 1
                chunk_ms = int((time.perf_counter() - stream_started) * 1000)
                if first_chunk_ms is None:
                    first_chunk_ms = chunk_ms
                text = decoder.decode(chunk, final=False) if decoder is not None else chunk.decode("utf-8", errors="replace")
                trace_chunks.append(
                    {
                        "received_at": _now_iso(),
                        "received_monotonic_ms": chunk_ms,
                        "chunk_index": chunk_count,
                        "size_bytes": len(chunk),
                        "text_preview": text[:500],
                    }
                )
                buffer += text

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")
                    if line == "":
                        item = flush_current()
                        if item and item["event_type"] in {"run.completed", "run.failed", "run.cancelled"}:
                            return {
                                "chunk_count": chunk_count,
                                "first_chunk_ms": first_chunk_ms,
                                "first_event_ms": first_event_ms,
                                "last_event_ms": last_event_ms,
                            }
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

    item = flush_current()
    if item:
        return {
            "chunk_count": chunk_count,
            "first_chunk_ms": first_chunk_ms,
            "first_event_ms": first_event_ms,
            "last_event_ms": last_event_ms,
        }
    return {
        "chunk_count": chunk_count,
        "first_chunk_ms": first_chunk_ms,
        "first_event_ms": first_event_ms,
        "last_event_ms": last_event_ms,
    }


async def run_live_sse_probe(
    *,
    base_url: str = BASE,
    trace_dir: Path | None = None,
    message: str = "Hello, explain how GraphRAG works and show your progress as stages.",
    namespace: str = DEFAULT_NS,
    username: str = "test-user",
    stream_timeout_s: float = DEFAULT_STREAM_TIMEOUT_S,
    min_stream_spread_ms: int = DEFAULT_MIN_SPREAD_MS,
) -> dict[str, Any]:
    trace_dir = (trace_dir or _default_trace_dir()).expanduser().resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_base = trace_dir / f"test_api-trace-{_utc_stamp()}"
    trace_events_path = trace_base.with_suffix(".events.jsonl")
    trace_chunks_path = trace_base.with_suffix(".chunks.jsonl")
    trace_http_path = trace_base.with_suffix(".http.jsonl")
    trace_summary_path = trace_base.with_suffix(".summary.json")

    trace_http: list[dict[str, Any]] = []
    trace_chunks: list[dict[str, Any]] = []
    trace_events: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "base_url": base_url,
        "namespace": namespace,
        "message": message,
        "events_trace": str(trace_events_path),
        "chunks_trace": str(trace_chunks_path),
        "http_trace": str(trace_http_path),
        "summary_trace": str(trace_summary_path),
        "conversation_id": None,
        "run_id": None,
        "event_count": 0,
        "chunk_count": 0,
        "terminal_event": None,
        "status": "started",
        "stream_spread_ms": None,
        "min_stream_spread_ms": min_stream_spread_ms,
    }

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            token_response = await _request_json(
                client,
                trace_http,
                "POST",
                "/auth/dev-token",
                expected_status=200,
                json={"username": username, "role": "rw", "ns": namespace},
            )
            token = token_response.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}

            me_response = await _request_json(client, trace_http, "GET", "/api/auth/me", expected_status=200, headers=headers)
            summary["me"] = me_response.json()

            list_response = await _request_json(client, trace_http, "GET", "/api/conversations", expected_status=200, headers=headers)
            summary["conversation_count_before"] = len(list_response.json().get("conversations") or [])

            conversation_response = await _request_json(
                client,
                trace_http,
                "POST",
                "/api/conversations",
                expected_status=200,
                headers=headers,
                json={"user_id": username},
            )
            conversation_payload = conversation_response.json()
            conversation_id = conversation_payload.get("conversation_id") or conversation_payload.get("id")
            if not conversation_id:
                raise AssertionError(f"Create conversation response missing id: {conversation_payload}")
            summary["conversation_id"] = conversation_id

            submit_response = await _request_json(
                client,
                trace_http,
                "POST",
                f"/api/conversations/{conversation_id}/turns:answer",
                expected_status=202,
                headers=headers,
                json={"text": message, "workflow_id": "agentic_answering.v2"},
            )
            run_id = submit_response.json()["run_id"]
            summary["run_id"] = run_id

            run_response = await _request_json(client, trace_http, "GET", f"/api/runs/{run_id}", expected_status=200, headers=headers)
            summary["run_status_initial"] = run_response.json().get("status")

            stream_meta = await _stream_sse_trace(
                client,
                f"/api/runs/{run_id}/events?after_seq=0",
                headers=headers,
                trace_http=trace_http,
                trace_chunks=trace_chunks,
                trace_events=trace_events,
                timeout_s=stream_timeout_s,
            )

            summary["event_count"] = len(trace_events)
            summary["chunk_count"] = int(stream_meta.get("chunk_count") or 0)
            summary["first_chunk_ms"] = stream_meta.get("first_chunk_ms")
            summary["first_event_ms"] = stream_meta.get("first_event_ms")
            summary["last_event_ms"] = stream_meta.get("last_event_ms")
            if trace_events:
                summary["terminal_event"] = trace_events[-1]["event_type"]
                summary["event_types"] = [item["event_type"] for item in trace_events]
                summary["stream_spread_ms"] = int((trace_events[-1]["received_monotonic_ms"] or 0) - (trace_events[0]["received_monotonic_ms"] or 0))
            else:
                summary["event_types"] = []
                summary["stream_spread_ms"] = 0

            if len(trace_events) < 2:
                raise AssertionError(f"Expected multiple SSE events, got {len(trace_events)}")
            if summary["terminal_event"] not in {"run.completed", "run.failed", "run.cancelled"}:
                raise AssertionError(f"Did not observe terminal SSE event, got {summary['terminal_event']!r}")
            if summary["stream_spread_ms"] < min_stream_spread_ms:
                raise AssertionError(
                    f"SSE looked batched: spread={summary['stream_spread_ms']}ms, expected >= {min_stream_spread_ms}ms"
                )

            summary["status"] = "ok"
            return summary
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = repr(exc)
        raise
    finally:
        _write_jsonl(trace_events_path, trace_events)
        _write_jsonl(trace_chunks_path, trace_chunks)
        _write_jsonl(trace_http_path, trace_http)
        _write_json(trace_summary_path, summary)


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
        "--namespace",
        default=DEFAULT_NS,
        help="Namespaces to request in the dev token",
    )
    parser.add_argument(
        "--stream-timeout-s",
        type=float,
        default=DEFAULT_STREAM_TIMEOUT_S,
        help="Fail the probe if the SSE stream does not terminate within this timeout",
    )
    parser.add_argument(
        "--min-stream-spread-ms",
        type=int,
        default=DEFAULT_MIN_SPREAD_MS,
        help="Minimum event arrival spread required to treat the SSE delivery as real streaming",
    )
    args = parser.parse_args()

    print(f"Connecting to {args.base_url}...")
    try:
        summary = await run_live_sse_probe(
            base_url=args.base_url,
            trace_dir=Path(args.trace_dir),
            message=args.message,
            namespace=args.namespace,
            stream_timeout_s=args.stream_timeout_s,
            min_stream_spread_ms=args.min_stream_spread_ms,
        )
    except Exception as exc:
        print(f"ERROR: live SSE probe failed: {exc}")
        return 1

    print(f"OK: run_id={summary['run_id']} conversation_id={summary['conversation_id']}")
    print(f"OK: events={summary['event_count']} chunks={summary['chunk_count']} spread_ms={summary['stream_spread_ms']}")
    print(f"OK: terminal_event={summary['terminal_event']}")
    print(f"Events trace : {Path(summary['events_trace']).as_uri()}")
    print(f"Chunks trace : {Path(summary['chunks_trace']).as_uri()}")
    print(f"HTTP trace   : {Path(summary['http_trace']).as_uri()}")
    print(f"Summary trace: {Path(summary['summary_trace']).as_uri()}")
    return 0


def test_live_backend_sse_streams_not_single_batch():
    summary = asyncio.run(run_live_sse_probe())

    assert summary["status"] == "ok"
    assert summary["event_count"] >= 2
    assert summary["terminal_event"] in {"run.completed", "run.failed", "run.cancelled"}
    assert summary["stream_spread_ms"] >= summary["min_stream_spread_ms"]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
