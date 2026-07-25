"""Deterministic HTTP backend for opt-in browser E2E; never calls an LLM."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONVERSATION_ID = "fake-conversation-0001"
RUN_ID = "fake-run-0001"
TOKEN = "fake-browser-token"
PROMPT = "Explain the deterministic evidence path."
ANSWER = "Deterministic answer: evidence is linked to the completed run snapshot."


def response(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FakeCoreHandler(BaseHTTPRequestHandler):
    server_version = "KogwistarFakeCore/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/auth/dev-token":
            response(self, 200, {"token": TOKEN})
            return
        if path == "/api/conversations":
            response(self, 200, {"conversation_id": CONVERSATION_ID})
            return
        if path == f"/api/conversations/{CONVERSATION_ID}/turns:answer":
            payload = self._json()
            if payload.get("text") != PROMPT:
                response(self, 400, {"detail": "unexpected deterministic prompt"})
                return
            response(self, 202, {"run_id": RUN_ID})
            return
        response(self, 404, {"detail": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == f"/api/runs/{RUN_ID}/events":
            events = [
                {"seq": 1, "event_type": "run.stage", "payload": {"stage": "answering"}},
                {"seq": 2, "event_type": "output.delta", "payload": {"delta": ANSWER}},
                {"seq": 3, "event_type": "output.completed", "payload": {"assistant_text": ANSWER}},
                {"seq": 4, "event_type": "run.completed", "payload": {"status": "completed"}},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for event in events:
                body = json.dumps(event)
                self.wfile.write(f"id: {event['seq']}\nevent: {event['event_type']}\ndata: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
        elif path == "/api/auth/me":
            response(self, 200, {"user_id": "fake-user", "display_name": "Screenshot User"})
        elif path == "/api/conversations":
            response(self, 200, {"conversations": [{"id": CONVERSATION_ID, "turn_count": 1}]})
        elif path == f"/api/conversations/{CONVERSATION_ID}/turns":
            response(self, 200, {"turns": [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": ANSWER}]})
        elif path == f"/api/runs/{RUN_ID}":
            response(self, 200, {"run_id": RUN_ID, "conversation_id": CONVERSATION_ID, "workflow_id": "agentic_answering.v2", "status": "completed", "terminal": True})
        elif path == f"/api/runs/{RUN_ID}/events/poll":
            response(self, 200, {"events": [
                {"seq": 1, "event_type": "run.stage", "payload": {"stage": "answering"}},
                {"seq": 2, "event_type": "output.delta", "payload": {"delta": ANSWER}},
                {"seq": 3, "event_type": "output.completed", "payload": {"assistant_text": ANSWER}},
                {"seq": 4, "event_type": "run.completed", "payload": {"status": "completed"}},
            ]})
        elif path == f"/api/runs/{RUN_ID}/steps":
            response(self, 200, {"run_id": RUN_ID, "steps": [{"step_seq": 1, "node_id": "answer", "status": "completed"}]})
        elif path == f"/api/runs/{RUN_ID}/checkpoints":
            response(self, 200, {"run_id": RUN_ID, "checkpoints": [{"checkpoint_id": "fake-checkpoint-0001", "status": "completed"}]})
        elif path == f"/api/runs/{RUN_ID}/evidence":
            response(self, 200, {"version": "evidence.v1", "run_id": RUN_ID, "snapshot_id": "fake-snapshot-0001", "evidence_refs": [{"ref": "fake-node-0001", "summary": "Deterministic source"}]})
        elif path == f"/api/runs/{RUN_ID}/resume-contract":
            response(self, 200, {"version": "resume-contract.v1", "resumable": False, "options": []})
        else:
            response(self, 404, {"detail": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28111)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), FakeCoreHandler).serve_forever()


if __name__ == "__main__":
    main()
