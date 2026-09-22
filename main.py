from __future__ import annotations

"""FastHTML entrypoint for the GraphRAG chat frontend.

This file is the main orchestration layer. It does not contain business logic
for retrieval or generation; instead it:

- manages login/session state
- calls the backend GraphRAG API through `GraphAPI`
- returns HTML fragments that HTMX swaps into the page
- keeps polling/SSE state for live assistant runs

If you are new to HTMX/FastHTML, the key mental model is:
the browser mostly submits normal HTTP requests, and the server responds with
HTML fragments instead of JSON.
"""

import logging
import os
import time
import uuid
from urllib.parse import urlencode
from typing import Any, cast

from dotenv import load_dotenv
from fasthtml.common import *
from sse_starlette.sse import EventSourceResponse

import asyncio as _asyncio

from app_contracts import (
    ConversationSummary,
    DebugPanelState,
    DebugRunState,
    RunEventRecord,
    RunState,
    SessionData,
    StreamEventPayload,
)
from components import (
    ChatPanel,
    FourPanelLayout,
    RightPanel,
    Sidebar,
    ScriptQueuePanel,
    render_assistant_body,
    render_assistant_container,
    render_assistant_text,
    render_event_log_item,
    render_right_panel_controls,
    render_right_panel_viewport,
    RunInspector,
    render_stream_shim,
    render_thinking_state,
    render_user_message,
)
from graph_api import GraphAPI
from sse_contracts import sse_route_contract, validate_sse_route_contracts

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:28110")
CHAT_WORKFLOW_ID = os.getenv("CHAT_WORKFLOW_ID", "agentic_answering.v2").strip()
CHAT_APP_PORT = int(os.getenv("CHAT_APP_PORT", "5173"))
CHAT_APP_URL = os.getenv("CHAT_APP_URL", f"http://localhost:{CHAT_APP_PORT}")
POLL_INTERVAL_MS = int(os.getenv("CHAT_POLL_INTERVAL_MS", "750"))
CHAT_STREAM_MODE = str(os.getenv("CHAT_STREAM_MODE", "poll")).strip().lower()
if CHAT_STREAM_MODE not in {"poll", "sse"}:
    CHAT_STREAM_MODE = "poll"
CHAT_RELOAD = str(os.getenv("CHAT_RELOAD", "true")).strip().lower() not in {"0", "false", "no", "off"}
LOG_LEVEL = str(os.getenv("CHAT_LOG_LEVEL", "INFO")).upper()
CHAT_LOG_PATH = os.getenv("CHAT_LOG_PATH", "chat_app.log")
PAYLOAD_LOG_PATH = os.getenv("CHAT_PAYLOAD_LOG_PATH", "payload.log")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(CHAT_LOG_PATH, encoding="utf-8")
    ]
)
LOG = logging.getLogger("htmx.main")
PAYLOAD_LOG = logging.getLogger("htmx.payload")
if not PAYLOAD_LOG.handlers:
    _payload_handler = logging.FileHandler(PAYLOAD_LOG_PATH, encoding="utf-8")
    _payload_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    PAYLOAD_LOG.addHandler(_payload_handler)
PAYLOAD_LOG.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
PAYLOAD_LOG.propagate = False
RUN_EVENT_CACHE: dict[str, list[RunEventRecord]] = {}
AUTH_TOKEN_CACHE: dict[str, str] = {}


def _trace_session_size(session: SessionData, *, stage: str, run_id: str | None = None) -> None:
    """Log an approximate serialized session size for cookie overflow debugging."""

    try:
        import json

        raw_session = dict(session)
        approx = len(json.dumps(raw_session, default=str, ensure_ascii=False))
        key_sizes = ",".join(
            f"{key}:{len(json.dumps(value, default=str, ensure_ascii=False))}"
            for key, value in sorted(raw_session.items())
        )
    except Exception:
        approx = -1
        key_sizes = "unavailable"
    LOG.info(
        "session-size stage=%s run_id=%s approx_bytes=%s keys=%s key_bytes=%s",
        stage,
        run_id or "",
        approx,
        ",".join(sorted(str(key) for key in session.keys())),
        key_sizes,
    )


def _ensure_auth_key(session: SessionData) -> str:
    """Return the small cookie-stored key used to look up the real bearer token."""

    auth_key = str(session.get("auth_key") or "").strip()
    if auth_key:
        return auth_key
    auth_key = uuid.uuid4().hex
    session["auth_key"] = auth_key
    return auth_key


def _store_session_token(session: SessionData, token: str) -> None:
    """Store the backend bearer token in server memory instead of the cookie."""

    auth_key = _ensure_auth_key(session)
    AUTH_TOKEN_CACHE[auth_key] = str(token)
    # Migrate older cookie-backed sessions by removing the legacy token field.
    session.pop("token", None)
    _trace_session_size(session, stage="store_session_token")


def _get_session_token(session: SessionData) -> str:
    """Resolve the current bearer token from the server-side cache or legacy session."""

    auth_key = str(session.get("auth_key") or "").strip()
    if auth_key and auth_key in AUTH_TOKEN_CACHE:
        return AUTH_TOKEN_CACHE[auth_key]

    legacy_token = str(session.get("token") or "").strip()
    if legacy_token:
        _store_session_token(session, legacy_token)
        return legacy_token
    return ""


def _clear_session_token(session: SessionData) -> None:
    """Remove the cached auth token for this browser session."""

    auth_key = str(session.get("auth_key") or "").strip()
    if auth_key:
        AUTH_TOKEN_CACHE.pop(auth_key, None)
    session.pop("auth_key", None)
    session.pop("token", None)


def _trace_route(route_name: str, **fields) -> None:
    """Emit a compact route-entry trace with the most useful state."""

    payload = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None and v != "")
    LOG.info("route=%s %s", route_name, payload)


def _trace_sse_server(stage: str, *, run_id: str, **fields: object) -> None:
    """Emit a high-signal SSE timing line to both the logger and stdout."""

    stamp_ms = int(time.time() * 1000)
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    line = f"[SSE RELAY] t_ms={stamp_ms} stage={stage} run_id={run_id}"
    if extras:
        line = f"{line} {extras}"
    LOG.info(line)
    print(line, flush=True)


def _trace_payload(stage: str, *, run_id: str, payload: object, **fields: object) -> None:
    """Write full payload-oriented traces into a separate file for inspection."""

    try:
        import json

        payload_text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        payload_text = str(payload)

    meta = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    PAYLOAD_LOG.info(
        "stage=%s run_id=%s size=%s %s payload=%s",
        stage,
        run_id,
        len(payload_text),
        meta,
        payload_text,
    )


def _render_sse_fragment(node: object) -> str:
    """Serialize one FastHTML node for SSE delivery.

    `str(node)` on FastHTML elements is too lossy here and collapses into a
    text-like representation. HTMX SSE needs the rendered HTML fragment.
    """

    if node is None:
        return ""
    if isinstance(node, str):
        return node
    return repr(node)




def _patch_reload_logging() -> None:
    """Make reload output show the actual file paths that triggered it.

    Uvicorn's watchfiles supervisor only prints a generic "change detected"
    line by default. For noisy editor saves, that is not enough to understand
    what is bouncing the server, so we wrap the watcher and log the paths.
    """

    try:
        import uvicorn.supervisors.watchfilesreload as watchfilesreload
    except Exception:
        return

    original = watchfilesreload.WatchFilesReload.should_restart
    if getattr(original, "_htmxchat_patched", False):
        return

    def should_restart(self):
        changed_paths = original(self)
        if changed_paths:
            path_list = ", ".join(sorted(str(path) for path in changed_paths))
            LOG.info("reload triggered by: %s", path_list)
        return changed_paths

    should_restart._htmxchat_patched = True  # type: ignore[attr-defined]
    watchfilesreload.WatchFilesReload.should_restart = should_restart
    logging.getLogger("watchfiles.main").setLevel(logging.WARNING)


_patch_reload_logging()

# These headers are injected into every page.
hdrs = (
    Link(rel="stylesheet", href="https://cdn.jsdelivr.net/npm/inter-ui@3.19.3/inter.min.css"),
    Script(src="https://unpkg.com/htmx.org@1.9.12"),
    Script(src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"),
    Link(rel="stylesheet", href="/static/style.css"),
    Script(src="/static/app.js", defer=True),
)

async def lifespan(app):
    LOG.info("=== Chat App Starting ===")
    LOG.info("SERVER_URL: %s", SERVER_URL)
    LOG.info("CHAT_WORKFLOW_ID: %s", CHAT_WORKFLOW_ID or "(server default)")
    LOG.info("CHAT_APP_PORT: %s", CHAT_APP_PORT)
    LOG.info("CHAT_STREAM_MODE: %s", CHAT_STREAM_MODE)
    # Fail fast if any SSE-only route lost its explicit SSE return contract.
    validate_sse_route_contracts()
    for k, v in os.environ.items():
        if k.startswith(("HTTP", "GRAPHRAG", "CHAT")):
            LOG.info("ENV %s: %s", k, v)
    LOG.info("==========================")
    yield
    LOG.info("=== Chat App Shutting Down ===")

app, rt = fast_app(
    debug=True, 
    hdrs=hdrs, 
    secret_key=os.getenv("SECRET_KEY", "default-secret-key"),
    lifespan=lifespan,
    pico=True,
)

graph_api = GraphAPI()


def Layout(*c):
    """Wrap the page inside the title and four-panel layout."""

    return Title("GraphRAG Chat"), FourPanelLayout(*c)


def _session_conversations(session: SessionData) -> list[ConversationSummary]:
    """Read the cached conversation list from the user session."""

    return list(session.get("conversations") or [])


def _remember_conversation(session: SessionData, conv_id: str, *, turn_count: int | None = None) -> None:
    """Store one conversation at the front of the session cache.

    We keep only the newest 20 conversations so the session stays compact.
    """

    convs = [c for c in _session_conversations(session) if c.get("id") != conv_id]
    item: ConversationSummary = {"id": conv_id, "turn_count": int(turn_count or 0)}
    convs.insert(0, item)
    session["conversations"] = convs[:20]


def _sync_conversations(session: SessionData, conversations: list[ConversationSummary]) -> list[ConversationSummary]:
    """Replace the cached conversation list with the latest backend data."""

    session["conversations"] = list(conversations or [])[:5]
    _trace_session_size(session, stage="sync_conversations")
    return _session_conversations(session)


def _init_run_state(session: SessionData, *, run_id: str, conv_id: str, user_text: str) -> None:
    """Create the initial per-run progress record stored in the session.

    We need this state so polling/SSE can reconstruct the assistant bubble if
    the browser reloads or reconnects mid-run.
    """

    runs = dict(session.get("run_state") or {})
    runs[run_id] = {
        "run_id": run_id,
        "conversation_id": conv_id,
        "last_seq": 0,
        "text": "",
        "stage": "queued",
        "terminal": False,
        "error": None,
        "user_text": user_text,
        "mode": CHAT_STREAM_MODE,
    }
    session["run_state"] = runs
    _trace_session_size(session, stage="init_run_state", run_id=run_id)
    RUN_EVENT_CACHE[run_id] = []


def _load_run_state(session: SessionData, run_id: str) -> RunState:
    """Read the run state for one in-flight or finished assistant turn."""

    runs = dict(session.get("run_state") or {})
    state = dict(runs.get(run_id) or {})
    state.setdefault("run_id", run_id)
    state.setdefault("last_seq", 0)
    state.setdefault("text", "")
    state.setdefault("stage", "queued")
    state.setdefault("terminal", False)
    state.setdefault("error", None)
    state.setdefault("mode", CHAT_STREAM_MODE)
    if "events" not in state:
        state["events"] = list(RUN_EVENT_CACHE.get(run_id) or [])
    return cast(RunState, state)


def _has_run_state(session: SessionData, run_id: str) -> bool:
    """Tell whether this browser session already tracks the run."""

    runs = dict(session.get("run_state") or {})
    return bool(run_id and run_id in runs)


def _save_run_state(session: SessionData, state: RunState) -> None:
    """Persist an updated run state back into the session."""

    runs = dict(session.get("run_state") or {})
    slim_state = dict(state)
    slim_state.pop("events", None)
    runs[state["run_id"]] = slim_state
    session["run_state"] = runs
    _trace_session_size(session, stage="save_run_state", run_id=str(state.get("run_id") or ""))


async def _hydrate_run_state_once(session: SessionData, token: str, run_id: str) -> RunState:
    """Fetch one backend snapshot for an untracked historical run.

    A run from an older conversation may not exist in the current browser
    session yet. Without one backend check, we would treat it as `queued` and
    incorrectly open live SSE even though it has already finished.
    """

    state = _load_run_state(session, run_id)
    if _has_run_state(session, run_id):
        return state

    try:
        events = await graph_api.get_run_events(token, run_id, after_seq=0)
        for evt in events:
            _ingest_run_event(state, evt)
    except Exception:
        LOG.exception("failed to hydrate run events run_id=%s", run_id)

    try:
        run = await graph_api.get_run(token, run_id)
        if bool(run.get("terminal")):
            state["terminal"] = True
        state["stage"] = str(run.get("status") or state.get("stage") or "queued")
    except Exception:
        LOG.exception("failed to hydrate run status run_id=%s", run_id)

    _save_run_state(session, state)
    return state


def _set_last_run_id(session: SessionData, run_id: str | None) -> None:
    """Remember the newest run id so the debug panel can prefill itself."""

    run_id = str(run_id or "").strip()
    if run_id:
        session["last_run_id"] = run_id


def _current_run_id(session: SessionData) -> str:
    """Return the most recent run id we know about for this browser session."""

    last_run_id = str(session.get("last_run_id") or "").strip()
    if last_run_id:
        return last_run_id

    runs = dict(session.get("run_state") or {})
    if runs:
        # `dict` keeps insertion order, so the newest tracked run is the last key.
        return str(next(reversed(runs.keys())))

    return ""


def _load_panel_state(session: SessionData) -> DebugPanelState:
    """Read the global right-panel state for this browser session."""

    state = dict(session.get("debug_panel") or {})
    state.setdefault("run_id", _current_run_id(session))
    state.setdefault("mode", "history")
    state.setdefault("paused", False)
    return cast(DebugPanelState, state)


def _save_panel_state(session: SessionData, state: DebugPanelState) -> None:
    """Persist the global right-panel state back into the session."""

    session["debug_panel"] = dict(state)
    _trace_session_size(session, stage="save_panel_state", run_id=str(state.get("run_id") or ""))


def _sync_panel_to_run(session: SessionData, run_id: str, *, auto_live: bool = False) -> None:
    """Point the right panel at the newest run without destroying history mode."""

    panel_state = _load_panel_state(session)
    panel_state["run_id"] = run_id
    if auto_live and str(panel_state.get("mode") or "history") == "live" and not bool(panel_state.get("paused")):
        panel_state["mode"] = "live"
    _save_panel_state(session, panel_state)


def _load_debug_state(session: SessionData, run_id: str) -> DebugRunState:
    """Read the per-run debug UI state from the session."""

    debug_runs = dict(session.get("debug_state") or {})
    state = dict(debug_runs.get(run_id) or {})
    state.setdefault("paused", False)
    state.setdefault("resume_seq", 0)
    state.setdefault("cleared", False)
    return cast(DebugRunState, state)


def _save_debug_state(session: SessionData, run_id: str, state: DebugRunState) -> None:
    """Persist the per-run debug UI state back into the session."""

    debug_runs = dict(session.get("debug_state") or {})
    debug_runs[run_id] = dict(state)
    session["debug_state"] = debug_runs
    _trace_session_size(session, stage="save_debug_state", run_id=run_id)


def _debug_resume_seq(session: SessionData, run_id: str) -> int:
    """Pick the sequence number the debug SSE stream should resume from."""

    debug_state = _load_debug_state(session, run_id)
    resume_seq = int(debug_state.get("resume_seq") or 0)
    if resume_seq:
        return resume_seq
    run_state = _load_run_state(session, run_id)
    return int(run_state.get("last_seq") or 0)


def _panel_resume_seq(session: SessionData, run_id: str) -> int:
    """Expose the resume sequence for the run currently shown on the right."""

    if not run_id:
        return 0
    return int(_debug_resume_seq(session, run_id) or 0)


def _append_run_event(
    state: RunState,
    event: StreamEventPayload,
    *,
    seq: int,
    event_type: str,
    payload: dict,
    label: str,
    detail: str,
) -> None:
    """Store a normalized event record in the per-run session state."""

    events = list(cast(list[RunEventRecord], state.get("events") or []))
    if any(int(item.get("seq") or 0) == int(seq) and str(item.get("event_type") or "") == event_type for item in events):
        return

    def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Keep the session copy of event payloads small enough for cookie storage."""

        compact: dict[str, Any] = {}
        preferred_keys = (
            "stage",
            "summary",
            "delta",
            "message",
            "status",
            "assistant_text",
            "text",
            "output",
            "content",
            "event",
            "event_type",
            "type",
            "seq",
            "step_seq",
            "workflow_node_id",
            "assistant_turn_node_id",
            "run_id",
        )
        for key in preferred_keys:
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
                if len(value) > 240:
                    value = f"{value[:237]}..."
            elif not isinstance(value, (int, float, bool)):
                value = str(value)
                if len(value) > 240:
                    value = f"{value[:237]}..."
            compact[key] = value
        if compact:
            return compact

        for key, value in payload.items():
            if key in compact:
                continue
            if isinstance(value, str) and len(value) > 240:
                value = f"{value[:237]}..."
            elif not isinstance(value, (str, int, float, bool)):
                value = str(value)
                if len(value) > 240:
                    value = f"{value[:237]}..."
            compact[key] = value
            if len(compact) >= 6:
                break
        return compact

    events.append(
        cast(RunEventRecord, {
            "seq": seq,
            "event_type": event_type,
            "payload": _compact_payload(dict(payload or {})),
            "label": label,
            "detail": detail,
        })
    )
    events = events[-60:]
    state["events"] = events
    run_id = str(state.get("run_id") or "").strip()
    if run_id:
        RUN_EVENT_CACHE[run_id] = list(events)


def _event_seq(evt: StreamEventPayload, payload: dict, state: RunState) -> int:
    """Resolve the best sequence number available for an event."""

    raw_seq = evt.get("seq")
    if raw_seq is None:
        raw_seq = evt.get("id")
    if raw_seq is None:
        raw_seq = payload.get("seq")
    if raw_seq is None:
        raw_seq = payload.get("step_seq")
    if raw_seq is None:
        raw_seq = int(state.get("last_seq") or 0) + 1
    try:
        return int(raw_seq or 0)
    except (TypeError, ValueError):
        return int(state.get("last_seq") or 0) + 1


def _event_label(event_type: str, payload: dict) -> tuple[str, str]:
    """Convert backend event names into user-friendly log labels."""

    if event_type == "run.stage":
        return "Stage", str(payload.get("stage") or "running")
    if event_type == "reasoning.summary":
        return "Thought", str(payload.get("summary") or "")
    if event_type == "output.delta":
        delta = str(payload.get("delta") or "")
        if not delta:
            delta = str(payload.get("data") or "")
        preview = delta.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        return "Text", preview
    if event_type == "output.completed":
        completed_text = str(
            payload.get("assistant_text")
            or payload.get("text")
            or payload.get("output")
            or payload.get("content")
            or payload.get("data")
            or ""
        )
        preview = completed_text.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        return "Output.completed", preview
    if event_type == "run.completed":
        return "Completed", ""
    if event_type == "run.failed":
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("detail") or error.get("code")
        return "Failed", str(error or payload.get("message") or "")
    if event_type == "run.cancelled":
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("detail") or error.get("code")
        return "Cancelled", str(error or payload.get("message") or "")
    return event_type.replace("run.", "").capitalize(), ""


def _event_payload(evt: StreamEventPayload) -> dict:
    """Normalize backend event payloads.

    Some backend responses nest useful data under `payload`, while others send
    flat event dictionaries. This helper makes the downstream rendering code
    treat both shapes the same way.
    """

    payload = dict(evt.get("payload") or {})
    if payload:
        return payload
    return {
        key: value
        for key, value in evt.items()
        if key not in {"seq", "event_type", "type", "payload"}
    }


def _sse_event_marker(run_id: str, seq: int) -> str:
    """Keep a stable, inert marker for browser/debug trace correlation."""

    return f"<!-- evt-{run_id}-{seq} -->"


def _ingest_run_event(state: RunState, evt: StreamEventPayload) -> tuple[int, str, dict, str, str]:
    """Normalize one backend event and fold it into the session state.

    We use this in the normal chat stream and in the debug panel so both code
    paths keep the same event history and the same terminal-state logic.
    """

    payload = _event_payload(evt)
    seq = _event_seq(evt, payload, state)
    event_type = str(evt.get("event_type") or evt.get("type") or "")
    label, detail = _event_label(event_type, payload)

    state["last_seq"] = max(int(state.get("last_seq") or 0), seq)
    _append_run_event(state, evt, seq=seq, event_type=event_type, payload=payload, label=label, detail=detail)

    if event_type == "run.stage":
        state["stage"] = str(payload.get("stage") or evt.get("stage") or state.get("stage") or "running")
    elif event_type == "reasoning.summary":
        summary = str(payload.get("summary") or evt.get("summary") or "").strip()
        if summary:
            state["stage"] = summary
    elif event_type == "output.delta":
        delta = str(payload.get("delta") or evt.get("delta") or payload.get("data") or evt.get("data") or "")
        if delta:
            state["text"] = f"{state.get('text', '')}{delta}"
    elif event_type == "output.completed":
        completed_text = str(
            payload.get("assistant_text")
            or payload.get("text")
            or payload.get("output")
            or payload.get("content")
            or payload.get("data")
            or evt.get("assistant_text")
            or evt.get("text")
            or evt.get("output")
            or evt.get("content")
            or evt.get("data")
            or ""
        )
        if completed_text:
            state["text"] = completed_text
        if not str(state.get("stage") or "").strip() or str(state.get("stage") or "") == "queued":
            state["stage"] = "completed"
    elif event_type == "run.completed":
        state["terminal"] = True
        state["stage"] = "completed"
    elif event_type in {"run.failed", "run.cancelled"}:
        state["terminal"] = True
        state["stage"] = event_type.replace("run.", "")
        err = payload.get("message") or evt.get("message")
        if not err:
            error = payload.get("error") or evt.get("error")
            if isinstance(error, dict):
                err = error.get("message") or error.get("detail") or error.get("code")
            elif error:
                err = error
        err = err or state.get("error")
        if err:
            state["error"] = str(err)

    return seq, event_type, payload, label, detail


def _render_debug_event_rows(run_id: str, events: list[RunEventRecord]) -> list:
    """Build a static event list for the debug panel.

    These rows do not use `hx_swap_oob` because we want them to render as a
    normal list when the user clicks "Load Events Once".
    """

    rows = []
    for item in events:
        seq = int(item.get("seq") or 0)
        label = str(item.get("label") or item.get("event_type") or "Event")
        detail = str(item.get("detail") or "")
        rows.append(
            render_event_log_item(
                run_id=run_id,
                seq=seq,
                label=label,
                detail=detail,
                swap_oob_target=None,
            )
        )
    return rows


def _render_debug_window(
    *,
    run_id: str,
    session: SessionData,
    events: list[RunEventRecord],
    live: bool = False,
    source: str = "session",
    swap_oob_target: str | None = None,
) -> Div:
    """Build the viewport content for the right sidebar.

    The outer control surface lives in `RightPanel(...)`. This function only
    renders the status + event log area so the routes can swap the viewport
    without duplicating a second toolbar inside it.
    """

    run_state = _load_run_state(session, run_id)
    debug_state = _load_debug_state(session, run_id)
    panel_state = _load_panel_state(session)
    paused = bool(debug_state.get("paused"))
    run_terminal = bool(run_state.get("terminal"))
    effective_live = bool(live and not paused and not run_terminal)
    cleared = bool(debug_state.get("cleared"))
    resume_seq = int(debug_state.get("resume_seq") or run_state.get("last_seq") or 0)
    rows = [] if cleared and not live else _render_debug_event_rows(run_id, events)
    display_mode = "live" if effective_live else "history"
    if run_terminal:
        panel_state["mode"] = "history"

    status_bits = [f"source: {source}", f"seq: {int(run_state.get('last_seq') or 0)}"]
    if cleared:
        status_bits.insert(0, "cleared")
    if paused:
        status_bits.insert(0, f"paused at #{resume_seq}")
    elif effective_live:
        status_bits.insert(0, "live")
    elif live and run_terminal:
        status_bits.insert(0, f"live unavailable: terminal")
    elif run_state.get("terminal"):
        status_bits.insert(0, f"terminal: {run_state.get('stage') or 'done'}")
    if run_state.get("stage") and not run_state.get("terminal") and not live:
        status_bits.insert(0, f"stage: {run_state.get('stage')}")

    return render_right_panel_viewport(
        run_id=run_id,
        mode=display_mode,
        paused=paused,
        source=source,
        rows=rows,
        status_bits=status_bits,
        live_url=(f"/debug/run-events/{run_id}/stream?after_seq={resume_seq}" if effective_live else None),
        swap_oob_target=swap_oob_target,
    )


def _render_controls_oob(session: SessionData, run_id: str | None = None) -> Div:
    """Render the right-panel controls as an out-of-band replacement."""

    panel_state = _load_panel_state(session)
    resolved_run_id = str(run_id or panel_state.get("run_id") or _current_run_id(session) or "").strip()
    debug_state = _load_debug_state(session, resolved_run_id) if resolved_run_id else {"paused": False, "resume_seq": 0}
    run_state = _load_run_state(session, resolved_run_id) if resolved_run_id else {"terminal": False}
    mode = str(panel_state.get("mode") or "history")
    if bool(run_state.get("terminal")):
        mode = "history"
    return render_right_panel_controls(
        current_run_id=resolved_run_id,
        mode=mode,
        paused=bool(panel_state.get("paused") or debug_state.get("paused")),
        resume_seq=int(debug_state.get("resume_seq") or _panel_resume_seq(session, resolved_run_id)),
        terminal=bool(run_state.get("terminal")),
        swap_oob_target="outerHTML:#debug-panel-controls",
    )


def _render_events_window_oob(session: SessionData, run_id: str, *, live: bool, source: str = "session") -> Div:
    """Render the right-panel viewport as an out-of-band replacement."""

    state = _load_run_state(session, run_id)
    events = [] if bool(_load_debug_state(session, run_id).get("cleared")) and not live else list(cast(list[RunEventRecord], state.get("events") or []))
    return _render_debug_window(
        run_id=run_id,
        session=session,
        events=events,
        live=live,
        source=source,
        swap_oob_target="innerHTML:#events-window",
    )


def _should_push_event_to_debug_panel(session: SessionData, run_id: str) -> bool:
    """Only append event rows when the right panel is watching this run."""

    panel_state = _load_panel_state(session)
    selected_run_id = str(panel_state.get("run_id") or "").strip()
    if selected_run_id != str(run_id or "").strip():
        return False
    return True


@rt("/cancel-run/{run_id}")
async def post_cancel(run_id: str, session):
    """Cancel a running assistant job.

    HTMX submits a small POST when the user clicks the Stop button inside the
    assistant bubble. We return an empty response because the UI removes the
    button on the client side.
    """

    token = _get_session_token(session)
    if not token:
        return ""
    try:
        outcome = await graph_api.cancel_run(token, run_id)
        LOG.info("cancel_run run_id=%s outcome=%s", run_id, outcome.get("status"))
        if outcome.get("status") == "already-terminal":
            return Div("Run already finished.", cls="transport-notice")
        return Div("Cancellation requested.", cls="transport-notice")
    except Exception as exc:
        LOG.exception("cancel_run failed run_id=%s", run_id)
        return Div(f"Cancellation failed: {exc}", cls="error-msg")


def _handle_debug_clear(run_id: str, session):
    """Clear the visible debug viewport without deleting stored run history."""

    run_state = _load_run_state(session, run_id)
    debug_state = _load_debug_state(session, run_id)
    panel_state = _load_panel_state(session)
    resume_seq = int(run_state.get("last_seq") or debug_state.get("resume_seq") or 0)

    debug_state["paused"] = True
    debug_state["resume_seq"] = resume_seq
    debug_state["cleared"] = True
    _save_debug_state(session, run_id, debug_state)
    _set_last_run_id(session, run_id)
    panel_state["run_id"] = run_id
    panel_state["paused"] = True
    _save_panel_state(session, panel_state)

    return (
        _render_debug_window(run_id=run_id, session=session, events=[], live=False, source="cleared"),
        _render_controls_oob(session, run_id),
    )


def _handle_debug_toggle_suspend(run_id: str, session):
    """Suspend or resume live debug SSE for the selected run."""

    run_state = _load_run_state(session, run_id)
    debug_state = _load_debug_state(session, run_id)
    panel_state = _load_panel_state(session)
    resume_seq = int(run_state.get("last_seq") or debug_state.get("resume_seq") or 0)

    paused = not bool(debug_state.get("paused"))
    debug_state["paused"] = paused
    debug_state["resume_seq"] = resume_seq
    debug_state["cleared"] = False
    _save_debug_state(session, run_id, debug_state)
    _set_last_run_id(session, run_id)
    panel_state["run_id"] = run_id
    panel_state["mode"] = "live"
    panel_state["paused"] = paused
    _save_panel_state(session, panel_state)

    return _render_debug_window(
        run_id=run_id,
        session=session,
        events=list(cast(list[RunEventRecord], run_state.get("events") or [])),
        live=not paused,
        source="session",
    ), _render_controls_oob(session, run_id)


@rt("/debug/run-events/clear")
async def post_debug_run_events_clear(run_id: str = "", session=None):
    """Clear the visible debug event list using the single control form."""

    run_id = str(run_id or _load_panel_state(session).get("run_id") or _current_run_id(session) or "").strip()
    if not run_id:
        return Div("Enter a run id to clear its viewport.", cls="fallback-text")
    return _handle_debug_clear(run_id, session)


@rt("/debug/run-events/{run_id}/clear")
async def post_debug_run_events_clear_path(run_id: str, session):
    """Backward-compatible clear route for older HTMX controls."""

    return _handle_debug_clear(run_id, session)


@rt("/debug/run-events/toggle-suspend")
async def post_debug_run_events_toggle_suspend(run_id: str = "", session=None):
    """Suspend or resume the live SSE viewport using the control form."""

    run_id = str(run_id or _load_panel_state(session).get("run_id") or _current_run_id(session) or "").strip()
    if not run_id:
        return Div("Enter a run id before toggling live SSE.", cls="fallback-text")
    return _handle_debug_toggle_suspend(run_id, session)


@rt("/debug/run-events/{run_id}/suspend")
async def post_debug_run_events_suspend_path(run_id: str, session):
    """Backward-compatible suspend route for older HTMX controls."""

    return _handle_debug_toggle_suspend(run_id, session)


async def _ensure_conversation(session) -> str:
    """Create a conversation if the session does not already have one.

    This helper is currently unused by the routes below, but it captures the
    standard pattern: make sure we have a conversation ID, create one if not,
    and remember it in the session.
    """

    conv_id = str(session.get("conversation_id") or "").strip()
    if conv_id:
        return conv_id
    token = _get_session_token(session)
    user_id = session.get("user_id", "")
    conv_id = await graph_api.create_conversation(token, user_id)
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)
    LOG.info("created conversation conv_id=%s", conv_id)
    return conv_id


@rt("/login/dev")
async def post_login_dev(session):
    """Mints a dev token with full namespaces and logs in."""
    try:
        # Requesting multiple namespaces as a list for better Pydantic validation
        token_data = await graph_api.get_dev_token(role="rw", ns=["conversation", "workflow", "docs"])
        token = token_data.get("token") or token_data.get("access_token")
        if not token:
            raise ValueError("No token returned from backend")
        
        _store_session_token(session, token)
        # Fetch user info for the session
        try:
            me = await graph_api.get_me(token)
            session["username"] = me.get("display_name") or me.get("email") or me.get("user_id", "DevUser")
            session["user_id"] = me.get("user_id", "dev_user")
        except Exception:
            session["username"] = "DevUser"
            session["user_id"] = "dev_user"
            
        LOG.info("dev login success user_id=%s namespaces=%s", session["user_id"], "conversation,workflow,docs")
        return Redirect("/")
    except Exception as exc:
        LOG.exception("Dev login failed")
        return f"Login failed: {exc}"


@rt("/login")
def get_login():
    """Render the standalone login page."""

    login_url = f"{SERVER_URL}/api/auth/login?" + urlencode({"return_to": f"{CHAT_APP_URL}/"})
    return Title("Login"), Main(
        Section(cls="panel shadow-premium", style="margin: auto; margin-top: 10vh; max-width: 480px; padding: 2.5rem;")(
            Header(H1("GraphRAG Chat", cls="vibrant-text", style="font-size: 2rem; text-align: center;")),
            P("Click below to authenticate with the GraphRAG server.", style="text-align: center; color: var(--text-dim); margin-bottom: 2rem;"),
            A("Login with OIDC (Keycloak) ->", href=login_url, cls="btn-vibrant block-btn", style="display: block; text-align: center; text-decoration: none; padding: 1rem; font-size: 1.1rem; margin-bottom: 1rem;"),
            Hr(),
            P("Or use Developer Token (Automatic)", style="text-align: center; color: var(--text-dim); margin-top: 1rem;"),
            Button("Dev Login (RW Access) ->", hx_post="/login/dev", cls="btn-vibrant block-btn", style="width: 100%; padding: 1rem; font-size: 1.1rem; filter: hue-rotate(45deg);"),
        )
    )


@rt("/")
async def get_home(session, token: str | None = None):
    """Landing page / login callback handler.

    The backend login flow redirects back here with `?token=...`. If a token is
    present, we store it in the session and redirect again so the page reloads
    in a clean state.
    """

    if token:
        _store_session_token(session, token)
        try:
            me = await graph_api.get_me(token)
            session["username"] = me.get("display_name") or me.get("email") or me.get("user_id", "User")
            session["user_id"] = me.get("user_id", "")
            LOG.info("login success user_id=%s username=%s", session.get("user_id"), session.get("username"))
        except Exception:
            session["username"] = "User"
            session["user_id"] = ""
            LOG.exception("failed to fetch /api/auth/me after login")
        return Redirect("/")

    token = _get_session_token(session)
    if not token:
        return Redirect("/login")

    user_id = session.get("user_id", "")

    try:
        convs = await graph_api.list_conversations(token, user_id)
        _sync_conversations(session, convs)
        LOG.info("loaded conversations count=%s", len(convs))
    except Exception:
        convs = []
        LOG.exception("failed to list conversations")

    if not convs:
        try:
            conv_id = await graph_api.create_conversation(token, user_id)
            session["conversation_id"] = conv_id
            _remember_conversation(session, conv_id)
            return Redirect(f"/c/{conv_id}")
        except Exception as e:
            return Title("Error"), Main(Div(f"Failed to create conversation: {e}", cls="error-msg", style="margin: 2rem; padding: 1rem;"))
    return Redirect(f"/c/{convs[0]['id']}")


@rt("/c/{conv_id}")
async def get_conv(conv_id: str, session, request: Request):
    """Render one conversation view.

    This route returns either:
    - the full page, when loaded normally in the browser
    - only the panel fragments, when HTMX requests the route for a sidebar click
    """

    token = _get_session_token(session)
    if not token:
        return Redirect("/login")

    _trace_route(
        "get_conv",
        conv_id=conv_id,
        panel_mode=str(_load_panel_state(session).get("mode") or "history"),
        panel_run_id=str(_load_panel_state(session).get("run_id") or _current_run_id(session) or ""),
    )

    user_id = session.get("user_id", "")

    try:
        convs = await graph_api.list_conversations(token, user_id)
        _sync_conversations(session, convs)
    except Exception:
        convs = _session_conversations(session)
        LOG.exception("failed to refresh sidebar conversations")

    if not any(c.get("id") == conv_id for c in convs):
        convs = list(convs)
        convs.insert(0, {"id": conv_id, "turn_count": 0})
        _sync_conversations(session, convs)

    session["conversation_id"] = conv_id

    try:
        transcript = await graph_api.get_transcript(token, conv_id)
        LOG.info("loaded transcript conv_id=%s turns=%s", conv_id, len(transcript))
    except Exception:
        transcript = []
        LOG.exception("failed to load transcript conv_id=%s", conv_id)

    panel_state = _load_panel_state(session)
    panel_run_id = str(panel_state.get("run_id") or _current_run_id(session) or "").strip()
    if panel_run_id and token and not _has_run_state(session, panel_run_id):
        await _hydrate_run_state_once(session, token, panel_run_id)
    if panel_run_id and bool(_load_run_state(session, panel_run_id).get("terminal")):
        panel_state["mode"] = "history"
        panel_state["paused"] = False
        _save_panel_state(session, panel_state)
    res_sidebar = Sidebar(conversations=convs)
    res_chat = ChatPanel(messages=transcript, current_conv_id=conv_id)
    res_right = RightPanel(
        current_run_id=panel_run_id,
        mode=str(panel_state.get("mode") or "history"),
        paused=bool(panel_state.get("paused")),
        resume_seq=_panel_resume_seq(session, panel_run_id),
        terminal=bool(_load_run_state(session, panel_run_id).get("terminal")) if panel_run_id else False,
    )
    res_script_queue = ScriptQueuePanel()

    if "hx-request" in request.headers:
        # HTMX requests only need the inner fragments, not the full page chrome.
        return (res_sidebar, res_chat, res_right, res_script_queue)

    return Layout(res_sidebar, res_chat, res_right, res_script_queue)


@rt("/new-chat")
async def post_new(session):
    """Create a brand-new conversation and redirect to it."""

    token = _get_session_token(session)
    if not token:
        return Redirect("/login")
    user_id = session.get("user_id", "")
    conv_id = await graph_api.create_conversation(token, user_id)
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)
    LOG.info("new chat conv_id=%s", conv_id)
    return Redirect(f"/c/{conv_id}")


@rt("/send-message")
async def post_msg(message: str, conv_id: str, session):
    """Submit one human message to the backend and return HTMX fragments."""

    text = str(message or "").strip()
    LOG.info("POST /send-message message_len=%s conv_id=%s", len(text), conv_id)
    if not text:
        return ""
    token = _get_session_token(session)
    if not token:
        LOG.warning("Unauthorized /send-message attempt")
        return Redirect("/login")

    _trace_route(
        "post_msg",
        conv_id=conv_id,
        message_len=len(text),
        panel_mode=str(_load_panel_state(session).get("mode") or "history"),
        last_run_id=str(session.get("last_run_id") or ""),
    )

    username = session.get("username", "You")
    user_id = session.get("user_id", "user")
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)

    try:
        run_id = await graph_api.submit_turn(
            token,
            conv_id,
            text,
            user_id=user_id,
            workflow_id=CHAT_WORKFLOW_ID,
        )
    except Exception as exc:
        LOG.exception("submit_turn failed conv_id=%s", conv_id)
        # Return an error message to the UI
        return render_user_message(username=username, message=text), Div(f"Error: {exc}", cls="error-msg")

    _init_run_state(session, run_id=run_id, conv_id=conv_id, user_text=text)
    _set_last_run_id(session, run_id)
    _sync_panel_to_run(session, run_id, auto_live=True)
    LOG.info("submit_turn success conv_id=%s run_id=%s", conv_id, run_id)

    panel_state = _load_panel_state(session)
    is_live_panel = str(panel_state.get("mode") or "history") == "live" and not bool(panel_state.get("paused"))

    if CHAT_STREAM_MODE == "sse":
        sse_url = f"/events/{run_id}"
        LOG.info("assistant transport run_id=%s mode=sse sse_url=%s", run_id, sse_url)
        assistant = render_assistant_container(run_id=run_id, sse_url=f"/events/{run_id}")
    else:
        poll_url = f"/runs/{run_id}/poll?after_seq=0"
        LOG.info("assistant transport run_id=%s mode=poll poll_url=%s", run_id, poll_url)
        assistant = render_assistant_container(
            run_id=run_id,
            poll_url=poll_url,
            poll_interval_ms=POLL_INTERVAL_MS,
        )

    panel_updates: list = [_render_controls_oob(session, run_id)]
    if is_live_panel:
        debug_state = _load_debug_state(session, run_id)
        debug_state["paused"] = False
        debug_state["cleared"] = False
        debug_state["resume_seq"] = 0
        _save_debug_state(session, run_id, debug_state)
        panel_updates.append(_render_events_window_oob(session, run_id, live=True, source="live"))

    return (render_user_message(username=username, message=text), assistant, *panel_updates)


@rt("/runs/{run_id}/inspector")
async def get_run_inspector(run_id: str, session):
    """Render read-only lifecycle and evidence details for one backend run."""

    token = _get_session_token(session)
    if not token:
        return RunInspector(run={}, steps=[], checkpoints=[], evidence={}, error="Please log in again.")

    try:
        run = await graph_api.get_run(token, run_id)
        steps, checkpoints, evidence, resume_contract = await _asyncio.gather(
            graph_api.get_run_steps(token, run_id),
            graph_api.get_run_checkpoints(token, run_id),
            graph_api.get_run_evidence(token, run_id),
            graph_api.get_resume_contract(token, run_id),
        )
        return RunInspector(
            run=run,
            steps=steps,
            checkpoints=checkpoints,
            evidence=evidence,
            resume_contract=resume_contract,
        )
    except Exception as exc:
        LOG.exception("run inspector failed run_id=%s", run_id)
        return RunInspector(run={}, steps=[], checkpoints=[], evidence={}, error=f"Run inspection failed: {exc}")


@rt("/runs/{run_id}/resume")
async def post_run_resume(run_id: str, suspended_node_id: str, suspended_token_id: str, session):
    token = _get_session_token(session)
    if not token:
        return Div("Please log in again.", cls="error-msg")
    try:
        run = await graph_api.get_run(token, run_id)
        result = await graph_api.resume_run(
            token,
            run_id,
            suspended_node_id=suspended_node_id,
            suspended_token_id=suspended_token_id,
            workflow_id=str(run.get("workflow_id") or ""),
            conversation_id=str(run.get("conversation_id") or ""),
            turn_node_id=str(run.get("turn_node_id") or ""),
        )
        return Div(f"Resume submitted: {result.get('workflow_status') or 'accepted'}", cls="transport-notice")
    except Exception as exc:
        LOG.exception("resume failed run_id=%s", run_id)
        return Div(f"Resume failed: {exc}", cls="error-msg")


@rt("/runs/{run_id}/poll")
async def get_run_poll(run_id: str, after_seq: int = 0, session=None):
    """Poll the backend for new run events.

    This is the simpler fallback streaming mode. The browser keeps asking for
    updates, and we re-render the assistant bubble each time.
    """

    token = _get_session_token(session)
    if not token:
        return render_assistant_body(run_id=run_id, error="Please log in again.", terminal=True)

    state = _load_run_state(session, run_id)
    _trace_route(
        "get_run_poll",
        run_id=run_id,
        after_seq=after_seq,
        terminal=bool(state.get("terminal")),
        stage=str(state.get("stage") or ""),
        last_seq=int(state.get("last_seq") or 0),
    )
    if state.get("terminal"):
        _set_last_run_id(session, run_id)
        LOG.info("assistant transport terminal run_id=%s mode=poll stage=%s", run_id, state.get("stage"))
        return render_assistant_body(
            run_id=run_id,
            text=str(state.get("text") or ""),
            stage=str(state.get("stage") or "queued"),
            error=(str(state.get("error")) if state.get("error") else None),
            poll_url=None,
            poll_interval_ms=POLL_INTERVAL_MS,
            terminal=True,
        )

    last_seq = max(int(after_seq or 0), int(state.get("last_seq") or 0))

    try:
        events = await graph_api.get_run_events(token, run_id, after_seq=last_seq)
    except Exception as exc:
        state["error"] = f"Failed to load run events: {exc}"
        state["terminal"] = True
        _save_run_state(session, state)
        LOG.exception("poll failed run_id=%s after_seq=%s", run_id, last_seq)
        return render_assistant_body(
            run_id=run_id,
            text=state.get("text") or "",
            stage=state.get("stage"),
            error=state["error"],
            terminal=True,
        )

    oob_logs = []
    push_debug_rows = _should_push_event_to_debug_panel(session, run_id)
    for evt in events:
        seq, event_type, payload, label, detail = _ingest_run_event(state, evt)
        # Stream fragments must not target a mutable child node.  The complete
        # event window is replaced once terminal, avoiding OOB race/ordering
        # failures when HTMX processes the first SSE response.
        LOG.info("poll event run_id=%s seq=%s event=%s detail=%s push_debug=%s", run_id, seq, label, detail, push_debug_rows)

    if not bool(state.get("terminal")):
        try:
            run = await graph_api.get_run(token, run_id)
            if bool(run.get("terminal")):
                state["terminal"] = True
                state["stage"] = str(run.get("status") or state.get("stage") or "completed")
                LOG.info("poll terminal fallback run_id=%s status=%s", run_id, state["stage"])
        except Exception:
            LOG.exception("failed to fetch run status fallback run_id=%s", run_id)

    _save_run_state(session, state)
    _set_last_run_id(session, run_id)
    if state.get("terminal"):
        LOG.info("assistant transport terminal run_id=%s mode=poll stage=%s", run_id, state.get("stage"))

    assistant = render_assistant_body(
        run_id=run_id,
        text=str(state.get("text") or ""),
        stage=str(state.get("stage") or "queued"),
        error=(str(state.get("error")) if state.get("error") else None),
        poll_url=(None if state.get("terminal") else f"/runs/{run_id}/poll?after_seq={int(state.get('last_seq') or 0)}"),
        poll_interval_ms=POLL_INTERVAL_MS,
        terminal=bool(state.get("terminal")),
    )
    return (assistant, *oob_logs)


@rt("/events/{run_id}")
@sse_route_contract
async def get_events(run_id: str, session) -> EventSourceResponse:
    """Stream backend events through SSE.

    This path is the lower-latency live mode. Instead of polling, the browser
    keeps one long-lived connection open and the server pushes each event.
    """

    token = _get_session_token(session)
    if not token:
        async def expired_gen():
            yield dict(data='<div class="error-msg">Authentication expired.</div>', event="message")
        return EventSourceResponse(expired_gen())

    state = _load_run_state(session, run_id)
    _trace_route(
        "get_events",
        run_id=run_id,
        terminal=bool(state.get("terminal")),
        last_seq=int(state.get("last_seq") or 0),
    )
    _set_last_run_id(session, run_id)
    LOG.info("sse connect run_id=%s last_seq=%s", run_id, state.get("last_seq"))
    stream_started_at = time.perf_counter()
    _trace_sse_server("connect", run_id=run_id, last_seq=int(state.get("last_seq") or 0), terminal=bool(state.get("terminal")))

    if state.get("terminal"):
        async def terminal_gen():
            body = render_assistant_body(
                run_id=run_id,
                text=str(state.get("text") or ""),
                stage=str(state.get("stage") or "completed"),
                error=(str(state.get("error")) if state.get("error") else None),
                terminal=True,
                swap_oob_target=f"outerHTML:#run-{run_id}",
            )
            shim = render_stream_shim(
                run_id=run_id,
                active=False,
                swap_oob_target=f"outerHTML:#run-stream-{run_id}",
            )
            payload = f"{_render_sse_fragment(body)}{_render_sse_fragment(shim)}"
            _trace_sse_server(
                "emit-terminal-snapshot",
                run_id=run_id,
                stage_name=str(state.get("stage") or "completed"),
                html_len=len(payload),
                elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000),
            )
            yield dict(data=payload, event="message")

        return EventSourceResponse(terminal_gen())

    async def event_generator():
        try:
            async for event in graph_api.stream_events(token, run_id, after_seq=int(state.get("last_seq") or 0)):
                _trace_sse_server(
                    "relay-received",
                    run_id=run_id,
                    raw_seq=event.get("seq"),
                    raw_event=event.get("event_type") or event.get("type"),
                    elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000),
                )
                _trace_payload(
                    "relay-received",
                    run_id=run_id,
                    seq=event.get("seq"),
                    event_type=event.get("event_type") or event.get("type"),
                    payload=event,
                )
                seq, event_type, payload, label, detail = _ingest_run_event(state, event)
                push_debug_rows = _should_push_event_to_debug_panel(session, run_id)
                LOG.info("sse event run_id=%s seq=%s event=%s detail=%s push_debug=%s", run_id, seq, label, detail, push_debug_rows)
                _save_run_state(session, state)
                oob_log = ""

                body = render_assistant_body(
                    run_id=run_id,
                    text=str(state.get("text") or ""),
                    stage=str(state.get("stage") or "queued"),
                    error=(str(state.get("error")) if state.get("error") else None),
                    sse_url=(None if state.get("terminal") else f"/events/{run_id}"),
                    terminal=bool(state.get("terminal")),
                    swap_oob_target=f"outerHTML:#run-{run_id}",
                )

                if event_type in {"run.completed", "run.failed", "run.cancelled"}:
                    LOG.info("assistant transport terminal run_id=%s mode=sse event=%s", run_id, event_type)
                    shim = render_stream_shim(
                        run_id=run_id,
                        active=False,
                        swap_oob_target=f"outerHTML:#run-stream-{run_id}",
                    )
                    events_window = _render_events_window_oob(session, run_id, live=False, source="terminal")
                    message_html = f"{_sse_event_marker(run_id, seq)}{_render_sse_fragment(body)}{_render_sse_fragment(events_window)}{_render_sse_fragment(oob_log)}{_render_sse_fragment(shim)}"
                    _trace_sse_server(
                        "relay-emitting",
                        run_id=run_id,
                        seq=seq,
                        event=event_type,
                        terminal=True,
                        html_len=len(message_html),
                        elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000),
                    )
                    _trace_payload(
                        "relay-emitting",
                        run_id=run_id,
                        seq=seq,
                        event_type=event_type,
                        payload=message_html,
                    )
                    yield dict(data=message_html, event="message")
                    break

                message_html = f"{_sse_event_marker(run_id, seq)}{_render_sse_fragment(body)}{_render_sse_fragment(oob_log)}"
                _trace_sse_server(
                    "relay-emitting",
                    run_id=run_id,
                    seq=seq,
                    event=event_type,
                    terminal=False,
                    html_len=len(message_html),
                    elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000),
                )
                _trace_payload(
                    "relay-emitting",
                    run_id=run_id,
                    seq=seq,
                    event_type=event_type,
                    payload=message_html,
                )
                yield dict(data=message_html, event="message")
        except Exception as e:
            LOG.exception("sse stream error run_id=%s", run_id)
            _trace_sse_server("relay-error", run_id=run_id, error=repr(e), elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000))
            body = render_assistant_body(
                run_id=run_id,
                text=str(state.get("text") or ""),
                stage=str(state.get("stage") or "failed"),
                error=f"Stream Error: {str(e)}",
                terminal=True,
                swap_oob_target=f"outerHTML:#run-{run_id}",
            )
            shim = render_stream_shim(
                run_id=run_id,
                active=False,
                swap_oob_target=f"outerHTML:#run-stream-{run_id}",
            )
            payload = f"{_render_sse_fragment(body)}{_render_sse_fragment(shim)}"
            _trace_sse_server("relay-emitting-error", run_id=run_id, html_len=len(payload))
            yield dict(data=payload, event="message")

    return EventSourceResponse(event_generator())


@rt("/debug/run-events")
async def get_debug_run_events(run_id: str | None = None, mode: str = "once", after_seq: int = 0, session=None):
    """Render a one-shot or live debug view for any saved run id.

    HTMX sends the selected mode from the button the user clicked. We keep this
    separate from the normal chat stream so you can inspect a run without
    creating a new message turn.
    """

    if session is None:
        return Div("Session unavailable.", cls="error-msg")

    run_id = str(run_id or _current_run_id(session)).strip()
    mode = str(mode or "history").strip().lower()
    after_seq = int(after_seq or 0)
    if mode == "once":
        mode = "history"

    if not run_id:
        return Div("Enter a run id to inspect its events.", cls="fallback-text")

    _set_last_run_id(session, run_id)
    token = _get_session_token(session)
    _trace_route(
        "get_debug_run_events",
        run_id=run_id,
        mode=mode,
        after_seq=after_seq,
        panel_mode=str(_load_panel_state(session).get("mode") or "history"),
        terminal=bool(_load_run_state(session, run_id).get("terminal")),
        has_state=_has_run_state(session, run_id),
    )
    if token and not _has_run_state(session, run_id):
        await _hydrate_run_state_once(session, token, run_id)
    debug_state = _load_debug_state(session, run_id)
    panel_state = _load_panel_state(session)
    panel_state["run_id"] = run_id
    if bool(_load_run_state(session, run_id).get("terminal")):
        panel_state["mode"] = "history"
        _save_panel_state(session, panel_state)

    if mode == "live":
        if bool(_load_run_state(session, run_id).get("terminal")):
            panel_state["mode"] = "history"
            panel_state["paused"] = False
            debug_state["paused"] = False
            debug_state["resume_seq"] = int(_load_run_state(session, run_id).get("last_seq") or 0)
            _save_debug_state(session, run_id, debug_state)
            _save_panel_state(session, panel_state)
            return (
                _render_debug_window(
                    run_id=run_id,
                    session=session,
                    events=list(cast(list[RunEventRecord], _load_run_state(session, run_id).get("events") or [])),
                    live=False,
                    source="terminal",
                ),
                _render_controls_oob(session, run_id),
            )

        panel_state["mode"] = "live"
        panel_state["paused"] = False
        debug_state["paused"] = False
        debug_state["resume_seq"] = (
            after_seq
            or int(debug_state.get("resume_seq") or 0)
            or int(_load_run_state(session, run_id).get("last_seq") or 0)
        )
        debug_state["cleared"] = False
        _save_debug_state(session, run_id, debug_state)
        _save_panel_state(session, panel_state)

        return (
            _render_debug_window(
                run_id=run_id,
                session=session,
                events=list(cast(list[RunEventRecord], _load_run_state(session, run_id).get("events") or [])),
                live=True,
                source="live",
            ),
            _render_controls_oob(session, run_id),
        )

    state = _load_run_state(session, run_id)
    events = list(cast(list[RunEventRecord], state.get("events") or []))
    source = "session"
    panel_state["mode"] = "history"
    panel_state["paused"] = False
    debug_state["cleared"] = False

    if not events:
        if not token:
            return Div("Authentication expired.", cls="error-msg")

        try:
            fetched_events = await graph_api.get_run_events(token, run_id, after_seq=after_seq)
        except Exception as exc:
            LOG.exception("debug load failed run_id=%s", run_id)
            return Div(f"Failed to load events for {run_id}: {exc}", cls="error-msg")

        source = "backend"
        for evt in fetched_events:
            _ingest_run_event(state, evt)
        _save_run_state(session, state)
        events = list(cast(list[RunEventRecord], state.get("events") or []))

    debug_state["paused"] = False
    debug_state["resume_seq"] = int(state.get("last_seq") or 0)
    _save_debug_state(session, run_id, debug_state)
    _save_panel_state(session, panel_state)

    return (
        _render_debug_window(
            run_id=run_id,
            session=session,
            events=events,
            live=False,
            source=source,
        ),
        _render_controls_oob(session, run_id),
    )

@rt("/debug/run-events/{run_id}/stream")
@sse_route_contract
async def get_debug_run_events_stream(run_id: str, session, after_seq: int = 0) -> EventSourceResponse:
    """Stream a saved run into the debug panel without re-running it."""

    token = _get_session_token(session)
    if not token:
        async def expired_gen():
            yield dict(data='<div class="error-msg">Authentication expired.</div>', event="message")

        return EventSourceResponse(expired_gen())

    state = _load_run_state(session, run_id)
    _trace_route(
        "get_debug_run_events_stream",
        run_id=run_id,
        after_seq=after_seq,
        terminal=bool(state.get("terminal")),
        panel_mode=str(_load_panel_state(session).get("mode") or "history"),
        has_state=_has_run_state(session, run_id),
    )
    _set_last_run_id(session, run_id)
    if not _has_run_state(session, run_id):
        state = await _hydrate_run_state_once(session, token, run_id)
    start_seq = int(after_seq or _debug_resume_seq(session, run_id) or 0)
    panel_state = _load_panel_state(session)
    panel_state["run_id"] = run_id

    if not bool(state.get("terminal")):
        try:
            run = await graph_api.get_run(token, run_id)
            if bool(run.get("terminal")):
                state["terminal"] = True
                state["stage"] = str(run.get("status") or state.get("stage") or "completed")
                _save_run_state(session, state)
                panel_state["mode"] = "history"
                panel_state["paused"] = False
                _save_panel_state(session, panel_state)
                LOG.info("debug live fallback run_id=%s status=%s", run_id, state["stage"])
                async def terminal_gen():
                    yield dict(
                        data=f"{str(_render_events_window_oob(session, run_id, live=False, source='terminal'))}{str(_render_controls_oob(session, run_id))}",
                        event="message",
                    )

                return EventSourceResponse(terminal_gen())
        except Exception:
            LOG.exception("failed to fetch debug run status fallback run_id=%s", run_id)

    if bool(state.get("terminal")):
        panel_state["mode"] = "history"
        panel_state["paused"] = False
        debug_state = _load_debug_state(session, run_id)
        debug_state["paused"] = False
        debug_state["resume_seq"] = int(state.get("last_seq") or 0)
        _save_debug_state(session, run_id, debug_state)
        _save_panel_state(session, panel_state)
        async def terminal_gen():
            yield dict(
                data=f"{str(_render_events_window_oob(session, run_id, live=False, source='terminal'))}{str(_render_controls_oob(session, run_id))}",
                event="message",
            )

        return EventSourceResponse(terminal_gen())

    panel_state["mode"] = "live"
    panel_state["paused"] = False
    _save_panel_state(session, panel_state)
    stream_started_at = time.perf_counter()
    _trace_sse_server(
        "debug-live-open",
        run_id=run_id,
        after_seq=start_seq,
        terminal=bool(state.get("terminal")),
        panel_mode="live",
    )

    async def event_generator():
        try:
            async for event in graph_api.stream_events(token, run_id, after_seq=start_seq):
                _trace_sse_server(
                    "debug-live-received",
                    run_id=run_id,
                    raw_seq=event.get("seq"),
                    raw_event=event.get("event_type") or event.get("type"),
                    elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000),
                )
                _trace_payload(
                    "debug-live-received",
                    run_id=run_id,
                    seq=event.get("seq"),
                    event_type=event.get("event_type") or event.get("type"),
                    payload=event,
                )
                seq, event_type, payload, label, detail = _ingest_run_event(state, event)
                _save_run_state(session, state)
                debug_state = _load_debug_state(session, run_id)
                debug_state["resume_seq"] = int(state.get("last_seq") or seq)
                _save_debug_state(session, run_id, debug_state)

                # Do not append into optional #events-list from a long-lived
                # debug stream; inspector can replace that node mid-stream.
                # Terminal branch replaces the complete event window.
                oob_log = ""
                banner = Div(f"Watching run {run_id} · {label} #{seq}", cls="debug-status-line")

                # The banner keeps the SSE target alive while the rows append into
                # the shared `#events-window` panel via HTMX out-of-band swaps.
                if event_type in {"run.completed", "run.failed", "run.cancelled"}:
                    panel_state["mode"] = "history"
                    panel_state["paused"] = False
                    _save_panel_state(session, panel_state)
                    _trace_sse_server(
                        "debug-live-emitting-terminal",
                        run_id=run_id,
                        seq=seq,
                        event=event_type,
                        html_len=len(_render_sse_fragment(oob_log)),
                        elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000),
                    )
                    _trace_payload(
                        "debug-live-emitting",
                        run_id=run_id,
                        seq=seq,
                        event_type=event_type,
                        payload=f"{_render_sse_fragment(oob_log)}{_render_sse_fragment(_render_events_window_oob(session, run_id, live=False, source='terminal'))}{_render_sse_fragment(_render_controls_oob(session, run_id))}",
                    )
                    yield dict(
                        data=f"{_sse_event_marker(run_id, seq)}{_render_sse_fragment(oob_log)}{_render_sse_fragment(_render_events_window_oob(session, run_id, live=False, source='terminal'))}{_render_sse_fragment(_render_controls_oob(session, run_id))}",
                        event="message",
                    )
                    break

                _trace_sse_server(
                    "debug-live-emitting",
                    run_id=run_id,
                    seq=seq,
                    event=event_type,
                    html_len=len(_render_sse_fragment(oob_log)),
                    elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000),
                )
                _trace_payload(
                    "debug-live-emitting",
                    run_id=run_id,
                    seq=seq,
                    event_type=event_type,
                    payload=_render_sse_fragment(oob_log),
                )
                yield dict(data=f"{_sse_event_marker(run_id, seq)}{_render_sse_fragment(oob_log)}", event="message")
        except Exception as exc:
            LOG.exception("debug stream error run_id=%s", run_id)
            _trace_sse_server("debug-live-error", run_id=run_id, error=repr(exc), elapsed_ms=int((time.perf_counter() - stream_started_at) * 1000))
            yield dict(
                data=f'<div class="error-msg">Debug stream error: {str(exc)}</div>{str(_render_events_window_oob(session, run_id, live=False, source="error"))}{str(_render_controls_oob(session, run_id))}',
                event="message",
            )

    return EventSourceResponse(event_generator())


@rt("/static/{path:path}")
def static_files(path: str):
    """Serve files from the local `static/` directory."""

    return FileResponse(f"static/{path}")


@rt("/logout")
def get_logout(session):
    """Clear the session and send the user back to the login screen."""

    _clear_session_token(session)
    session.clear()
    return Redirect("/login")


_RELOAD_EXCLUDES = [
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.git/**",
    "**/.venv/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.swp",
    "**/*.swo",
    "**/*.tmp",
    "**/*.log",
    "**/*.jsonl",
    "**/artifacts/**",
    "chat_app.log",
]

# Keep hot-reload useful for code and UI edits, but ignore the common noisy
# files that editors, test runs, and the app itself generate.
serve(port=CHAT_APP_PORT, reload=CHAT_RELOAD, reload_excludes=_RELOAD_EXCLUDES)
