from __future__ import annotations

import os
from urllib.parse import urlencode

from dotenv import load_dotenv
from fasthtml.common import *

from components import ChatPanel, RightPanel, Sidebar, ThreePanelLayout, render_assistant_container, render_user_message
from graph_api import GraphAPI

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:28110")
CHAT_APP_PORT = int(os.getenv("CHAT_APP_PORT", "5001"))
CHAT_APP_URL = os.getenv("CHAT_APP_URL", f"http://localhost:{CHAT_APP_PORT}")
POLL_INTERVAL_MS = int(os.getenv("CHAT_POLL_INTERVAL_MS", "750"))

hdrs = (
    Link(rel="stylesheet", href="https://cdn.jsdelivr.net/npm/inter-ui@3.19.3/inter.min.css"),
    Link(rel="stylesheet", href="/static/style.css"),
    Script(src="https://unpkg.com/htmx.org@1.9.12"),
    Script(src="/static/app.js", defer=True),
)

app, rt = fast_app(
    debug=True,
    hdrs=hdrs,
    secret_key=os.getenv("SECRET_KEY", "default-secret-key"),
)

graph_api = GraphAPI()


def Layout(*c):
    return Title("GraphRAG Chat"), ThreePanelLayout(*c)


def _session_conversations(session) -> list[dict]:
    return list(session.get("conversations") or [])


def _remember_conversation(session, conv_id: str) -> None:
    convs = [c for c in _session_conversations(session) if c.get("id") != conv_id]
    convs.insert(0, {"id": conv_id, "turn_count": 0})
    session["conversations"] = convs[:20]


def _init_run_state(session, *, run_id: str, conv_id: str, user_text: str) -> None:
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
    }
    session["run_state"] = runs


def _load_run_state(session, run_id: str) -> dict:
    runs = dict(session.get("run_state") or {})
    state = dict(runs.get(run_id) or {})
    state.setdefault("run_id", run_id)
    state.setdefault("last_seq", 0)
    state.setdefault("text", "")
    state.setdefault("stage", "queued")
    state.setdefault("terminal", False)
    state.setdefault("error", None)
    return state


def _save_run_state(session, state: dict) -> None:
    runs = dict(session.get("run_state") or {})
    runs[state["run_id"]] = dict(state)
    session["run_state"] = runs


async def _ensure_conversation(session) -> str:
    conv_id = str(session.get("conversation_id") or "").strip()
    if conv_id:
        _remember_conversation(session, conv_id)
        return conv_id
    token = session["token"]
    user_id = session.get("user_id", "")
    conv_id = await graph_api.create_conversation(token, user_id)
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)
    return conv_id


@rt("/login")
def get_login():
    login_url = f"{SERVER_URL}/api/auth/login?" + urlencode({"redirect_uri": f"{CHAT_APP_URL}/"})
    return Title("Login"), Main(
        Section(cls="panel shadow-premium", style="margin: auto; margin-top: 10vh; max-width: 480px; padding: 2.5rem;")(
            Header(H1("GraphRAG Chat", cls="vibrant-text", style="font-size: 2rem; text-align: center;")),
            P(
                "Click below to authenticate with the GraphRAG server.",
                style="text-align: center; color: var(--text-dim); margin-bottom: 2rem;",
            ),
            A(
                "Login with GraphRAG →",
                href=login_url,
                cls="btn-vibrant block-btn",
                style="display: block; text-align: center; text-decoration: none; padding: 1rem; font-size: 1.1rem;",
            ),
        )
    )


@rt("/logout")
def get_logout(session):
    session.clear()
    return Redirect("/login")


@rt("/")
async def get_home(session, token: str | None = None):
    if token:
        session["token"] = token
        try:
            me = await graph_api.get_me(token)
            session["username"] = me.get("display_name") or me.get("email") or me.get("user_id", "User")
            session["user_id"] = me.get("user_id", "")
        except Exception:
            session["username"] = "User"
            session["user_id"] = ""
        return Redirect("/")

    if "token" not in session:
        return Redirect("/login")

    conv_id = await _ensure_conversation(session)
    return Redirect(f"/c/{conv_id}")


@rt("/c/{conv_id}")
async def get_conv(conv_id: str, session):
    if "token" not in session:
        return Redirect("/login")

    token = session["token"]
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)

    try:
        transcript = await graph_api.get_transcript(token, conv_id)
    except Exception:
        transcript = []

    return Layout(
        Sidebar(conversations=_session_conversations(session)),
        ChatPanel(messages=transcript, current_conv_id=conv_id),
        RightPanel(),
    )


@rt("/new-chat")
async def post_new(session):
    if "token" not in session:
        return Redirect("/login")
    token = session["token"]
    user_id = session.get("user_id", "")
    conv_id = await graph_api.create_conversation(token, user_id)
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)
    return Redirect(f"/c/{conv_id}")


@rt("/send-message")
async def post_msg(message: str, conv_id: str, session):
    text = str(message or "").strip()
    if not text:
        return ""
    token = session.get("token")
    if not token:
        return Redirect("/login")

    username = session.get("username", "You")
    session["conversation_id"] = conv_id
    _remember_conversation(session, conv_id)

    run_id = await graph_api.submit_turn(token, conv_id, text)
    _init_run_state(session, run_id=run_id, conv_id=conv_id, user_text=text)

    return (
        render_user_message(username=username, message=text),
        render_assistant_container(run_id=run_id, poll_url=f"/runs/{run_id}/poll?after_seq=0", poll_interval_ms=POLL_INTERVAL_MS),
    )


@rt("/runs/{run_id}/poll")
async def get_run_poll(run_id: str, after_seq: int = 0, session=None):
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
        return render_assistant_container(run_id=run_id, text=state.get("text") or "", stage=state.get("stage"), error=state["error"])

    for evt in events:
        seq = int(evt.get("seq") or last_seq)
        state["last_seq"] = max(int(state.get("last_seq") or 0), seq)
        event_type = str(evt.get("event_type") or evt.get("type") or "")
        payload = evt.get("payload") or {}

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
            state["terminal"] = bool(run.get("terminal"))
            if run.get("status"):
                state["stage"] = str(run.get("status"))
        except Exception:
            pass

    _save_run_state(session, state)
    next_after_seq = int(state.get("last_seq") or 0)
    text = state.get("text") or ""
    if not text and not state.get("terminal"):
        text = ""
    return render_assistant_container(
        run_id=run_id,
        text=text,
        stage=state.get("stage"),
        error=state.get("error"),
        poll_url=None if state.get("terminal") else f"/runs/{run_id}/poll?after_seq={next_after_seq}",
        poll_interval_ms=POLL_INTERVAL_MS,
        terminal=bool(state.get("terminal")),
    )


@rt("/static/{path:path}")
def static_files(path: str):
    return FileResponse(f"static/{path}")
