from __future__ import annotations

from fasthtml.common import *


def _simple_markdown_html(content: str) -> str:
    text = str(content or "")
    if "```" in text:
        text = text.replace("```python", "<pre><code>").replace("```", "</code></pre>")
    return text.replace("\n", "<br>")


def render_user_message(*, username: str, message: str):
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
    terminal: bool = False,
):
    body_children = []
    if text:
        body_children.append(NotStr(_simple_markdown_html(text)))
    elif error:
        body_children.append(Span("No output.", cls="assistant-placeholder"))
    else:
        body_children.append(Span("Thinking…", cls="assistant-placeholder"))

    if stage and not terminal:
        body_children.append(Div(f"Status: {stage}", cls="thinking-state"))
    if error:
        body_children.append(Div(error, cls="error-msg"))

    attrs = {
        "id": f"run-{run_id}",
        "cls": "msg-body assistant-msg streaming-content shadow-premium",
        "data_run_id": run_id,
    }
    if poll_url and not terminal:
        attrs.update(
            {
                "hx_get": poll_url,
                "hx_trigger": f"load, every {int(poll_interval_ms)}ms",
                "hx_swap": "outerHTML",
            }
        )

    return Div(
        Div("Assistant", cls="msg-sender"),
        Div(*body_children, **attrs),
        cls="chat-message-container align-left",
    )


def Sidebar(conversations=None):
    conversations = list(conversations or [])
    conv_items = [
        Li(A(f"Chat {c.get('id', '...')}", href=f"/c/{c.get('id')}", cls="conv-link"), cls="conv-item")
        for c in conversations
    ]
    if not conv_items:
        conv_items = [Li("No previous conversations", cls="fallback-text")]

    return Aside(
        Div(H2("History", cls="sidebar-title"), Ul(*conv_items, cls="conv-list", id="conversation-list"), cls="sidebar-content"),
        Div(Button("+ New Chat", cls="btn-vibrant block-btn", hx_post="/new-chat"), A("Logout", href="/logout", cls="logout-link"), cls="sidebar-footer"),
        cls="panel sidebar-panel shadow-premium",
    )


def ChatPanel(messages=None, current_conv_id=None):
    messages = list(messages or [])
    msg_elements = []
    for msg in messages:
        role = str(msg.get("role") or "assistant")
        sender = "You" if role == "user" else "Assistant"
        content = msg.get("content") or msg.get("text") or ""
        cls_name = "user-msg" if role == "user" else "assistant-msg"
        align_cls = "align-right" if role == "user" else "align-left"
        msg_elements.append(
            Div(
                Div(sender, cls="msg-sender"),
                Div(NotStr(_simple_markdown_html(content)), cls=f"msg-body {cls_name} shadow-premium"),
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
                hx_on__after_request="this.querySelector('#message-input').value = ''",
            )
        ),
        cls="panel center-panel shadow-premium",
    )


def RightPanel():
    return Aside(
        H2("Status", cls="sidebar-title"),
        Div(
            P("Minimal demo mode: HTMX polling from the UI server to the GraphRAG backend.", cls="status-note"),
            P("The durable run/event model stays on the backend; transport can evolve later.", cls="status-note"),
            id="events-window",
            cls="events-window",
        ),
        cls="panel right-panel shadow-premium",
    )


def ThreePanelLayout(*c):
    return Main(*c, cls="layout-3-panel container")
