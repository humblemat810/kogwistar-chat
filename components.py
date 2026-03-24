from __future__ import annotations

"""FastHTML component builders used by the chat UI.

This file intentionally stays close to plain Python functions instead of using
class-based components. That makes the rendering flow easier to follow for
engineers coming from Flask/Django or React:

- each function returns a ready-to-render HTML subtree
- the caller decides where that subtree gets inserted
- HTMX attributes on the returned nodes drive partial updates without custom JS

If you are reading this for the first time, the important idea is:
the server renders HTML fragments, and HTMX swaps those fragments into the page.
"""

import html

from fasthtml.common import *


def render_assistant_text(text: str):
    """Render assistant content safely.

    We escape the text first so model output cannot inject HTML/JS into the page.
    After escaping, we do a tiny formatting pass for markdown-style code fences.
    This is deliberately lightweight; it is not a full markdown renderer.
    """

    escaped = html.escape(str(text or ""))
    html_output = escaped.replace("```python", "<pre><code>").replace("```", "</code></pre>")
    return NotStr(html_output)


def render_thinking_state(*, run_id: str, stage: str | None = None):
    """Render the placeholder shown while a run is still streaming.

    In HTMX terms, this is the assistant message body that remains on screen
    until we receive token output or a terminal event.
    """

    return Div(
        Span("Thinking", cls="thinking-dot"),
        Span(f" - {stage}" if stage else "...", cls="thinking-state-label"),
        Button(
            "Stop",
            hx_post=f"/cancel-run/{run_id}",
            hx_swap="none",
            cls="btn-mini btn-outline",
            style="margin-left: 1rem; font-size: 0.7rem; padding: 0.2rem 0.5rem;",
            hx_on__after_request="this.remove()",
        ),
        cls="thinking-state",
    )


def Sidebar(conversations=None):
    """Render the left sidebar.

    The sidebar is just a list of conversation links plus a "new chat" action.
    Each conversation link uses `hx_get` so clicking it fetches only the chat
    layout rather than doing a full browser navigation.
    """

    if conversations is None:
        conversations = []

    conv_items = [
        Li(
            A(
                f"Chat {c.get('id', '...')} ({c.get('turn_count', 0)} turns)",
                hx_get=f"/c/{c.get('id')}",
                hx_target="#main-content",
                hx_push_url="true",
                cls="conv-link",
            ),
            cls="conv-item",
        )
        for c in conversations
    ]

    if not conv_items:
        conv_items = [Li("No previous conversations", cls="fallback-text")]

    return Aside(
        Div(
            H2("History", cls="sidebar-title"),
            Ul(*conv_items, cls="conv-list", id="conversation-list"),
            cls="sidebar-content",
        ),
        Div(
            Button("+ New Chat", cls="btn-vibrant block-btn", hx_post="/new-chat"),
            A("Logout", href="/logout", cls="logout-link"),
            cls="sidebar-footer",
        ),
        cls="panel sidebar-panel shadow-premium",
    )


def render_user_message(*, username: str, message: str):
    """Render a message bubble from the human user."""

    return Div(
        Div(username, cls="msg-sender"),
        Div(message, cls="msg-body user-msg shadow-premium"),
        cls="chat-message-container align-right",
    )


def render_assistant_container(
    *,
    run_id: str,
    text: str = "",
    stage: str | None = None,
    error: str | None = None,
    poll_url: str | None = None,
    poll_interval_ms: int = 750,
    sse_url: str | None = None,
):
    """Render the assistant message container.

    This wrapper holds the actual assistant body and also attaches the HTMX
    behavior that keeps the message updated:

    - `poll_url` enables polling mode using `hx_get` + `hx_trigger`
    - `sse_url` enables SSE mode using the HTMX SSE extension

    Only one of those modes is active at a time.
    """

    attrs = {"id": f"run-{run_id}", "cls": "msg-body assistant-msg streaming-content shadow-premium"}
    if poll_url:
        # HTMX polling mode: the browser re-requests the fragment every N ms.
        attrs.update(
            {
                "hx_get": poll_url,
                "hx_trigger": f"every {int(poll_interval_ms)}ms",
                "hx_swap": "outerHTML",
            }
        )
    if sse_url:
        # SSE mode: the HTMX SSE extension listens to the server push channel.
        attrs.update({"hx_ext": "sse", "sse_connect": sse_url, "sse_swap": "message"})

    if error:
        body = Div(error, cls="error-msg")
    elif text:
        body = render_assistant_text(text)
    else:
        body = render_thinking_state(run_id=run_id, stage=stage)

    return Div(
        Div("Assistant", cls="msg-sender"),
        Div(body, **attrs),
        cls="chat-message-container align-left",
    )


def render_event_log_item(*, run_id: str, seq: int, label: str, detail: str = ""):
    """Render one event row in the right-hand event log.

    `hx_swap_oob` means "out-of-band swap". HTMX can insert this fragment into
    `#events-window` even if the current request is primarily updating the chat
    message area.
    """

    detail_text = f" - {detail}" if detail else ""
    return Div(
        Div(f"run {run_id}", cls="event-run-id"),
        Div(f"#{seq} {label}{detail_text}", cls="event-text"),
        id=f"evt-{run_id}-{seq}",
        cls="event-log-item shadow-premium",
        hx_swap_oob="afterbegin:#events-window",
    )


def ChatPanel(messages=None, current_conv_id=None):
    """Render the center conversation panel.

    `messages` is the transcript returned by the backend. We render the full
    history into the chat window and keep the input form at the bottom.
    """

    if messages is None:
        messages = []

    msg_elements = []
    for msg in messages:
        # The backend uses `role=user|assistant`; we translate that to layout
        # classes that control alignment and bubble styling.
        role = str(msg.get("role") or "assistant")
        sender = "user" if role == "user" else "Assistant"
        content = msg.get("content", "")
        cls_name = "user-msg" if sender == "user" else "assistant-msg"
        align_cls = "align-right" if sender == "user" else "align-left"

        msg_elements.append(
            Div(
                Div(sender.capitalize(), cls="msg-sender"),
                Div(render_assistant_text(content), cls=f"msg-body {cls_name} shadow-premium"),
                cls=f"chat-message-container {align_cls}",
            )
        )

    return Section(
        Header(H1("GraphRAG Assistant", cls="vibrant-text chat-header")),
        Div(*msg_elements, id="chat-window", cls="chat-window"),
        Footer(
            Form(hx_post="/send-message", hx_target="#chat-window", hx_swap="beforeend")(
                Input(type="hidden", name="conv_id", value=current_conv_id or ""),
                Group(
                    Input(name="message", placeholder="Type your message...", id="message-input", cls="chat-input"),
                    Button("Send", cls="btn-vibrant"),
                ),
                # After the browser receives the server response, clear the
                # text field so the user can type the next message immediately.
                hx_on__after_request="this.querySelector('#message-input').value = ''",
            )
        ),
        cls="panel center-panel shadow-premium",
    )


def RightPanel():
    """Render the event stream panel on the right side."""

    return Aside(
        H2("Agent Events", cls="sidebar-title"),
        Div(id="events-window", cls="events-window", style="flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.5rem;"),
        cls="panel right-panel shadow-premium",
    )


def ThreePanelLayout(*c):
    """Wrap the three panels in the page-level main container."""

    return Main(*c, cls="layout-3-panel container", id="main-content")
