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
from urllib.parse import urlencode
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fasthtml.common import *
from sse_starlette.sse import EventSourceResponse

from components import (
    ChatPanel,
    RightPanel,
    Sidebar,
    ThreePanelLayout,
    render_assistant_container,
    render_assistant_text,
    render_event_log_item,
    render_thinking_state,
    render_user_message,
)
from graph_api import GraphAPI

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:28110")
CHAT_APP_PORT = int(os.getenv("CHAT_APP_PORT", "5173"))
CHAT_APP_URL = os.getenv("CHAT_APP_URL", f"http://localhost:{CHAT_APP_PORT}")
POLL_INTERVAL_MS = int(os.getenv("CHAT_POLL_INTERVAL_MS", "750"))
CHAT_STREAM_MODE = str(os.getenv("CHAT_STREAM_MODE", "poll")).strip().lower()
if CHAT_STREAM_MODE not in {"poll", "sse"}:
    CHAT_STREAM_MODE = "poll"
LOG_LEVEL = str(os.getenv("CHAT_LOG_LEVEL", "INFO")).upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("chat_app.log", encoding="utf-8")
    ]
)
LOG = logging.getLogger("htmx.main")

# These headers are injected into every page.
hdrs = (
    Link(rel="stylesheet", href="https://cdn.jsdelivr.net/npm/inter-ui@3.19.3/inter.min.css"),
    Script(src="https://unpkg.com/htmx.org@1.9.12"),
    Script(src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"),
    Link(rel="stylesheet", href="/static/style.css"),
    Script(src="/static/app.js", defer=True),
)

@asynccontextmanager
async def lifespan(app):
    LOG.info("=== Chat App Starting ===")
    LOG.info("SERVER_URL: %s", SERVER_URL)
    LOG.info("CHAT_APP_PORT: %s", CHAT_APP_PORT)
    LOG.info("CHAT_STREAM_MODE: %s", CHAT_STREAM_MODE)
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
    lifespan=lifespan
)

graph_api = GraphAPI()


def Layout(*c):
    """Wrap the page inside the title and three-panel layout."""

    return Title("GraphRAG Chat"), ThreePanelLayout(*c)


def _session_conversations(session) -> list[dict]:
    """Read the cached conversation list from the user session."""

    return list(session.get("conversations") or [])


def _remember_conversation(session, conv_id: str, *, turn_count: int | None = None) -> None:
    """Store one conversation at the front of the session cache.

    We keep only the newest 20 conversations so the session stays compact.
    """

    convs = [c for c in _session_conversations(session) if c.get("id") != conv_id]
    item = {"id": conv_id, "turn_count": int(turn_count or 0)}
    convs.insert(0, item)
    session["conversations"] = convs[:20]


def _sync_conversations(session, conversations: list[dict]) -> list[dict]:
    """Replace the cached conversation list with the latest backend data."""

    session["conversations"] = list(conversations or [])[:20]
    return _session_conversations(session)


def _init_run_state(session, *, run_id: str, conv_id: str, user_text: str) -> None:
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


def _load_run_state(session, run_id: str) -> dict:
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
    return state


def _save_run_state(session, state: dict) -> None:
    """Persist an updated run state back into the session."""

    runs = dict(session.get("run_state") or {})
    runs[state["run_id"]] = dict(state)
    session["run_state"] = runs


def _event_label(event_type: str, payload: dict) -> tuple[str, str]:
    """Convert backend event names into user-friendly log labels."""

    if event_type == "run.stage":
        return "Stage", str(payload.get("stage") or "running")
    if event_type == "reasoning.summary":
        return "Thought", str(payload.get("summary") or "")
    if event_type == "output.delta":
        delta = str(payload.get("delta") or "")
        preview = delta.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        return "Text", preview
    if event_type == "run.completed":
        return "Completed", ""
    if event_type == "run.failed":
        return "Failed", str(payload.get("message") or "")
    if event_type == "run.cancelled":
        return "Cancelled", str(payload.get("message") or "")
    return event_type.replace("run.", "").capitalize(), ""


def _event_payload(evt: dict) -> dict:
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


@rt("/cancel-run/{run_id}")
async def post_cancel(run_id: str, session):
    """Cancel a running assistant job.

    HTMX submits a small POST when the user clicks the Stop button inside the
    assistant bubble. We return an empty response because the UI removes the
    button on the client side.
    """

    token = session.get("token")
    if not token:
        return ""
    success = await graph_api.cancel_run(token, run_id)
    LOG.info("cancel_run run_id=%s success=%s", run_id, success)
    return ""


async def _ensure_conversation(session) -> str:
    """Create a conversation if the session does not already have one.

    This helper is currently unused by the routes below, but it captures the
    standard pattern: make sure we have a conversation ID, create one if not,
    and remember it in the session.
    """

    conv_id = str(session.get("conversation_id") or "").strip()
    if conv_id:
        return conv_id
    token = session["token"]
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
        token = token_data.get("access_token")
        if not token:
            raise ValueError("No token returned from backend")
        
        session["token"] = token
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

    login_url = f"{SERVER_URL}/api/auth/login?" + urlencode({"redirect_uri": f"{CHAT_APP_URL}/"})
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
        session["token"] = token
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

    if "token" not in session:
        return Redirect("/login")

    token = session["token"]
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

    if "token" not in session:
        return Redirect("/login")

    token = session["token"]
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

    res_sidebar = Sidebar(conversations=convs)
    res_chat = ChatPanel(messages=transcript, current_conv_id=conv_id)
    res_right = RightPanel()

    if "hx-request" in request.headers:
        # HTMX requests only need the inner fragments, not the full page chrome.
        return (res_sidebar, res_chat, res_right)

    return Layout(res_sidebar, res_chat, res_right)


@rt("/new-chat")
async def post_new(session):
    """Create a brand-new conversation and redirect to it."""

    if "token" not in session:
        return Redirect("/login")
    token = session["token"]
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
    token = session.get("token")
    if not token:
        LOG.warning("Unauthorized /send-message attempt")
        return Redirect("/login")

    username = session.get("username", "You")
    user_id = session.get("user_id", "user")
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)

    try:
        run_id = await graph_api.submit_turn(token, conv_id, text, user_id=user_id)
    except Exception as exc:
        LOG.exception("submit_turn failed conv_id=%s", conv_id)
        # Return an error message to the UI
        return render_user_message(username=username, message=text), Div(f"Error: {exc}", cls="error-msg")

    _init_run_state(session, run_id=run_id, conv_id=conv_id, user_text=text)
    LOG.info("submit_turn success conv_id=%s run_id=%s", conv_id, run_id)

    if CHAT_STREAM_MODE == "sse":
        assistant = render_assistant_container(run_id=run_id, sse_url=f"/events/{run_id}")
    else:
        assistant = render_assistant_container(
            run_id=run_id,
            poll_url=f"/runs/{run_id}/poll?after_seq=0",
            poll_interval_ms=POLL_INTERVAL_MS,
        )
    return (render_user_message(username=username, message=text), assistant)


@rt("/runs/{run_id}/poll")
async def get_run_poll(run_id: str, after_seq: int = 0, session=None):
    """Poll the backend for new run events.

    This is the simpler fallback streaming mode. The browser keeps asking for
    updates, and we re-render the assistant bubble each time.
    """

    token = session.get("token")
    if not token:
        return render_assistant_container(run_id=run_id, text="Authentication expired.", error="Please log in again.")

    state = _load_run_state(session, run_id)
    last_seq = max(int(after_seq or 0), int(state.get("last_seq") or 0))

    try:
        events = await graph_api.get_run_events(token, run_id, after_seq=last_seq)
    except Exception as exc:
        state["error"] = f"Failed to load run events: {exc}"
        state["terminal"] = True
        _save_run_state(session, state)
        LOG.exception("poll failed run_id=%s after_seq=%s", run_id, last_seq)
        return render_assistant_container(run_id=run_id, text=state.get("text") or "", stage=state.get("stage"), error=state["error"])

    oob_logs = []
    for evt in events:
        seq = int(evt.get("seq") or last_seq)
        state["last_seq"] = max(int(state.get("last_seq") or 0), seq)
        event_type = str(evt.get("event_type") or evt.get("type") or "")
        payload = _event_payload(evt)
        label, detail = _event_label(event_type, payload)
        oob_logs.append(render_event_log_item(run_id=run_id, seq=seq, label=label, detail=detail))
        LOG.info("poll event run_id=%s seq=%s event=%s detail=%s", run_id, seq, label, detail)

        if event_type == "run.stage":
            state["stage"] = str(payload.get("stage") or evt.get("stage") or state.get("stage") or "running")
        elif event_type == "reasoning.summary":
            summary = str(payload.get("summary") or evt.get("summary") or "").strip()
            if summary:
                state["stage"] = summary
        elif event_type == "output.delta":
            delta = str(payload.get("delta") or evt.get("delta") or "")
            if delta:
                state["text"] = f"{state.get('text', '')}{delta}"
        elif event_type == "run.completed":
            state["terminal"] = True
            state["stage"] = "completed"
        elif event_type in {"run.failed", "run.cancelled"}:
            state["terminal"] = True
            state["stage"] = event_type.replace("run.", "")
            err = payload.get("message") or evt.get("message") or state.get("error")
            if err:
                state["error"] = str(err)

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
    assistant = render_assistant_container(
        run_id=run_id,
        text=str(state.get("text") or ""),
        stage=str(state.get("stage") or "queued"),
        error=(str(state.get("error")) if state.get("error") else None),
        poll_url=(None if state.get("terminal") else f"/runs/{run_id}/poll?after_seq={int(state.get('last_seq') or 0)}"),
        poll_interval_ms=POLL_INTERVAL_MS,
    )
    return (assistant, *oob_logs)


@rt("/events/{run_id}")
async def get_events(run_id: str, session):
    """Stream backend events through SSE.

    This path is the lower-latency live mode. Instead of polling, the browser
    keeps one long-lived connection open and the server pushes each event.
    """

    token = session.get("token")
    if not token:
        async def expired_gen():
            yield dict(data='<div class="error-msg">Authentication expired.</div>', event="message")
        return EventSourceResponse(expired_gen())

    state = _load_run_state(session, run_id)
    LOG.info("sse connect run_id=%s last_seq=%s", run_id, state.get("last_seq"))

    async def event_generator():
        try:
            async for event in graph_api.stream_events(token, run_id, after_seq=int(state.get("last_seq") or 0)):
                seq = int(event.get("seq") or state.get("last_seq") or 0)
                if seq:
                    state["last_seq"] = max(int(state.get("last_seq") or 0), seq)

                event_type = str(event.get("event_type") or "message")
                payload = _event_payload(event)
                label, detail = _event_label(event_type, payload)
                LOG.info("sse event run_id=%s seq=%s event=%s detail=%s", run_id, seq, label, detail)

                oob_log = render_event_log_item(run_id=run_id, seq=seq, label=label, detail=detail)

                if event_type == "run.stage":
                    state["stage"] = str(payload.get("stage") or state.get("stage") or "running")
                    _save_run_state(session, state)
                    html = str(render_thinking_state(run_id=run_id, stage=state["stage"]))
                    yield dict(data=f"{html}{str(oob_log)}", event="message")

                elif event_type == "reasoning.summary":
                    summary = str(payload.get("summary") or "").strip()
                    if summary:
                        state["stage"] = summary
                        _save_run_state(session, state)
                        html = str(render_thinking_state(run_id=run_id, stage=summary))
                        yield dict(data=f"{html}{str(oob_log)}", event="message")

                elif event_type == "output.delta":
                    delta = str(payload.get("delta") or "")
                    if delta:
                        state["text"] = f"{state.get('text', '')}{delta}"
                        _save_run_state(session, state)
                        html_output = str(render_assistant_text(state["text"]))
                        yield dict(data=f"{html_output}{str(oob_log)}", event="message")

                elif event_type == "run.completed":
                    state["terminal"] = True
                    state["stage"] = "completed"
                    _save_run_state(session, state)
                    html_output = str(render_assistant_text(state.get("text") or "(No output)"))
                    yield dict(data=f"{html_output}{str(oob_log)}", event="message")
                    break

                elif event_type in {"run.failed", "run.cancelled"}:
                    state["terminal"] = True
                    state["stage"] = event_type.replace("run.", "")
                    state["error"] = str(payload.get("message") or event_type)
                    _save_run_state(session, state)
                    html = str(Div(state["error"], cls="error-msg"))
                    yield dict(data=f"{html}{str(oob_log)}", event="message")
                    break

                else:
                    _save_run_state(session, state)
                    yield dict(data=str(oob_log), event="message")
        except Exception as e:
            LOG.exception("sse stream error run_id=%s", run_id)
            yield dict(data=f'<div class="error-msg">Stream Error: {str(e)}</div>', event="message")

    return EventSourceResponse(event_generator())


@rt("/static/{path:path}")
def static_files(path: str):
    """Serve files from the local `static/` directory."""

    return FileResponse(f"static/{path}")


@rt("/logout")
def get_logout(session):
    """Clear the session and send the user back to the login screen."""

    session.clear()
    return Redirect("/login")


serve(port=CHAT_APP_PORT)
