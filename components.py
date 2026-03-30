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
            cls="panel-head",
        ),
        Div(
            Ul(*conv_items, cls="conv-list scroll-area", id="conversation-list"),
            cls="panel-body",
        ),
        Div(
            Button("+ New Chat", cls="btn-vibrant block-btn", hx_post="/new-chat"),
            A("Logout", href="/logout", cls="logout-link"),
            cls="panel-foot sidebar-footer",
        ),
        cls="panel panel-left sidebar-panel shadow-premium",
    )


def render_user_message(*, username: str, message: str):
    """Render a message bubble from the human user."""

    return Div(
        Div(username, cls="msg-sender"),
        Div(message, cls="msg-body user-msg shadow-premium"),
        cls="chat-message-container align-right",
    )


def render_assistant_body(
    *,
    run_id: str,
    text: str = "",
    stage: str | None = None,
    error: str | None = None,
    poll_url: str | None = None,
    poll_interval_ms: int = 750,
    sse_url: str | None = None,
    swap_oob_target: str | None = None,
    terminal: bool = False,
):
    """Render the replaceable inner assistant bubble.

    This is the element HTMX polling replaces via `outerHTML`, and it is also
    the element we replace out-of-band from SSE terminal events. Keeping this
    as a dedicated function avoids the old mismatch where polling targeted the
    inner node but the server returned the whole outer chat container.
    """

    stream_mode = "static"
    if poll_url:
        stream_mode = "poll"
    elif sse_url:
        stream_mode = "sse"

    attrs = {
        "id": f"run-{run_id}",
        "cls": "msg-body assistant-msg streaming-content shadow-premium",
        "data_run_id": run_id,
        "data_stream_mode": stream_mode,
        "data_poll_url": str(poll_url or ""),
        "data_sse_url": str(sse_url or ""),
        "data_terminal": "true" if terminal else "false",
        "data_route_source": "assistant-bubble",
    }
    if poll_url:
        attrs.update(
            {
                "hx_get": poll_url,
                "hx_trigger": f"every {int(poll_interval_ms)}ms",
                "hx_swap": "outerHTML",
            }
        )
    if swap_oob_target:
        attrs["hx_swap_oob"] = swap_oob_target

    if error:
        content = Div(error, cls="error-msg")
    elif text:
        content = render_assistant_text(text)
    else:
        content = render_thinking_state(run_id=run_id, stage=stage)

    status_bits = [stream_mode.upper()]
    if terminal:
        status_bits.append("done")
    elif stage:
        status_bits.append(str(stage))

    return Div(
        Div(
            Span(" | ".join(status_bits), cls="assistant-transport-label"),
            cls="assistant-transport-row",
        ),
        Div(content, cls="assistant-content"),
        **attrs,
    )


def render_stream_shim(
    *,
    run_id: str,
    sse_url: str | None = None,
    active: bool = True,
    swap_oob_target: str | None = None,
    route_source: str = "assistant-stream-shim",
):
    """Render the hidden SSE transport node for one assistant run.

    HTMX's SSE extension reconnects on the element that owns `sse_connect`.
    Keeping that behavior on a small hidden shim lets us replace the visible
    bubble independently and remove the shim on terminal events.
    """

    attrs = {
        "id": f"run-stream-{run_id}",
        "cls": "stream-transport-shim",
        "data_run_id": run_id,
        "data_stream_mode": "sse" if active else "static",
        "data_sse_url": str(sse_url or ""),
        "data_route_source": route_source,
        "aria_hidden": "true",
    }
    if active and sse_url:
        attrs.update({"hx_ext": "sse", "sse_connect": sse_url, "sse_swap": "message"})
    if swap_oob_target:
        attrs["hx_swap_oob"] = swap_oob_target
    return Div("", **attrs)


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

    The outer wrapper stays stable in the chat transcript. The inner body is
    the part that polling or SSE updates while the message is streaming.
    """

    return Div(
        Div("Assistant", cls="msg-sender"),
        render_assistant_body(
            run_id=run_id,
            text=text,
            stage=stage,
            error=error,
            poll_url=poll_url,
            poll_interval_ms=poll_interval_ms,
            sse_url=sse_url,
            terminal=bool(error or text) and not poll_url and not sse_url and stage in {"completed", "failed", "cancelled"},
        ),
        render_stream_shim(
            run_id=run_id,
            sse_url=sse_url,
            active=bool(sse_url),
            route_source="assistant-stream-shim",
        ) if sse_url else "",
        cls="chat-message-container align-left",
    )


def render_event_log_item(
    *,
    run_id: str,
    seq: int,
    label: str,
    detail: str = "",
    swap_oob_target: str | None = "beforeend:#events-list",
):
    """Render one event row in the right-hand event log.

    `hx_swap_oob` means "out-of-band swap". HTMX can insert this fragment into
    `#events-list` even if the current request is primarily updating the chat
    message area.
    """

    detail_text = f" - {detail}" if detail else ""
    return Div(
        Div(f"run {run_id}", cls="event-run-id"),
        Div(f"#{seq} {label}{detail_text}", cls="event-text"),
        id=f"evt-{run_id}-{seq}",
        cls="event-log-item shadow-premium",
        **({"hx_swap_oob": swap_oob_target} if swap_oob_target else {}),
    )


def render_right_panel_controls(
    *,
    current_run_id: str = "",
    mode: str = "history",
    paused: bool = False,
    resume_seq: int = 0,
    terminal: bool = False,
    swap_oob_target: str | None = None,
):
    """Render the single control surface for the right panel."""

    is_live = str(mode or "history") == "live"
    suspend_label = f"Resume from #{resume_seq}" if paused else "Suspend SSE"
    attrs = {
        "id": "debug-panel-controls",
        "cls": "panel-head debug-panel-controls",
        "data_debug_mode": "live" if is_live else "history",
        "data_debug_paused": "true" if paused else "false",
        "data_resume_seq": str(int(resume_seq or 0)),
        "data_route_source": "debug-panel-controls",
    }
    if swap_oob_target:
        attrs["hx_swap_oob"] = swap_oob_target

    return Div(
        H2("Agent Events", cls="sidebar-title"),
        P(
            "Inspect one run in history or live mode.",
            cls="text-dim",
            style="margin-top: -0.5rem; margin-bottom: 0.75rem; font-size: 0.85rem;",
        ),
        Form(
            hx_get="/debug/run-events",
            hx_target="#events-window",
            hx_swap="innerHTML",
            id="debug-control-form",
            cls="debug-run-form",
            data_debug_mode="live" if is_live else "history",
            data_debug_paused="true" if paused else "false",
            data_route_source="debug-control-form",
        )(
            Label(
                "Run ID",
                Input(
                    name="run_id",
                    id="debug-run-id",
                    value=str(current_run_id or ""),
                    placeholder="run_...",
                    cls="chat-input",
                    style="width: 100%;",
                ),
                style="display: grid; gap: 0.25rem; font-size: 0.85rem;",
            ),
            Input(type="hidden", name="after_seq", id="debug-after-seq", value=str(int(resume_seq or 0))),
            Div(
                Button(
                    "History",
                    type="submit",
                    name="mode",
                    value="history",
                    data_route_source="debug-controls-history",
                    cls=("btn-vibrant" if not is_live else "btn-mini btn-outline"),
                ),
                Button(
                    "Live SSE",
                    type="submit",
                    name="mode",
                    value="live",
                    data_route_source="debug-controls-live",
                    cls=("btn-vibrant" if is_live else "btn-mini btn-outline"),
                ),
                Button(
                    "Clear",
                    type="button",
                    data_route_source="debug-controls-clear",
                    hx_post="/debug/run-events/clear",
                    hx_include="#debug-control-form",
                    hx_target="#events-window",
                    hx_swap="innerHTML",
                    cls="btn-mini btn-outline",
                ),
                Button(
                    suspend_label,
                    type="button",
                    data_route_source="debug-controls-suspend",
                    hx_post="/debug/run-events/toggle-suspend",
                    hx_include="#debug-control-form",
                    hx_target="#events-window",
                    hx_swap="innerHTML",
                    cls="btn-mini btn-outline",
                ),
                cls="debug-toolbar",
            ),
        ),
        Div(
            f"Mode: {'Live SSE' if is_live else 'History'}{' (completed)' if terminal else ''}{' (paused)' if paused else ''}",
            cls="text-dim debug-mode-note",
        ),
        **attrs,
    )


def render_right_panel_viewport(
    *,
    run_id: str,
    mode: str = "history",
    paused: bool = False,
    source: str = "session",
    rows: tuple | list = (),
    status_bits: tuple | list = (),
    live_url: str | None = None,
    swap_oob_target: str | None = None,
):
    """Render the viewport content only, without duplicating the toolbar."""

    attrs = {"cls": "debug-events-content"}
    if swap_oob_target:
        attrs["hx_swap_oob"] = swap_oob_target

    return Div(
        Div(
            Div(f"Run {run_id}", cls="debug-status-line"),
            Div(" | ".join(str(bit) for bit in status_bits), cls="text-dim debug-status-meta"),
            cls="debug-events-header",
        ),
        render_stream_shim(
            run_id=run_id,
            sse_url=live_url,
            active=bool(live_url) and not paused,
            route_source="debug-stream-shim",
        ) if mode == "live" else "",
        Div(
            *(rows or [Div("No events are loaded for this run.", cls="fallback-text", style="padding: 0.75rem;")]),
            id="events-list",
            cls="debug-event-list",
        ),
        **attrs,
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
        Header(H1("GraphRAG Assistant", cls="vibrant-text chat-header"), cls="panel-head"),
        Div(
            Div(*msg_elements, id="chat-window", cls="chat-window scroll-area"),
            cls="panel-body",
        ),
        Footer(
            Form(hx_post="/send-message", hx_target="#chat-window", hx_swap="beforeend")(
                Input(type="hidden", name="conv_id", value=current_conv_id or ""),
                Group(
                    Input(name="message", placeholder="Type your message...", id="message-input", cls="chat-input"),
                    Button("Send", cls="btn-vibrant"),
                ),
                hx_on__after_request="this.querySelector('#message-input').value = ''",
            ),
            cls="panel-foot",
        ),
        cls="panel panel-center center-panel shadow-premium",
    )


def RightPanel(
    current_run_id: str | None = None,
    *,
    mode: str = "history",
    paused: bool = False,
    resume_seq: int = 0,
    terminal: bool = False,
):
    """Render the event stream panel on the right side."""

    run_id = str(current_run_id or "").strip()

    return Aside(
        render_right_panel_controls(
            current_run_id=run_id,
            mode=mode,
            paused=paused,
            resume_seq=resume_seq,
            terminal=terminal,
        ),
        Div(
            Div(
                Div(
                    "Pick a run and click a debug button.",
                    cls="fallback-text",
                    style="padding: 0.75rem;",
                ),
                id="events-window",
                cls="events-window scroll-area",
                tabindex="0",
            ),
            cls="panel-body",
        ),
        cls="panel panel-right right-panel shadow-premium",
    )


def ScriptQueuePanel():
    """Render the fourth-column script queue and test editor."""

    return Aside(
        Div(
            H2("Script Queue", cls="sidebar-title"),
            P(
                "Submit a test script to append a synthetic SSE event into the third panel, then queue it here.",
                cls="text-dim",
                style="margin-top: -0.5rem; margin-bottom: 0.75rem; font-size: 0.85rem;",
            ),
            Div(
                Textarea(
                    "",
                    id="script-test-editor",
                    placeholder="Write a small Python snippet...",
                    cls="chat-input script-queue-editor",
                    rows=5,
                ),
                Div(
                    Label("Pyodide timeout (ms)", cls="script-queue-timeout-label", for_="py-worker-timeout-ms"),
                    Input(
                        id="py-worker-timeout-ms",
                        type="number",
                        value="5000",
                        min="500",
                        step="250",
                        cls="chat-input script-queue-timeout-input",
                    ),
                    P(
                        "If the worker does not answer in time, it is terminated and restarted automatically.",
                        cls="text-dim script-queue-timeout-note",
                    ),
                    cls="script-queue-timeout-shell",
                ),
                Div(
                    Button(
                        "Submit",
                        type="button",
                        cls="btn-vibrant script-queue-submit",
                        id="script-queue-submit",
                        onclick="window.submitScriptQueueFromEditor()",
                    ),
                    Button(
                        "Mock SSE Event",
                        type="button",
                        cls="btn-mini btn-outline script-queue-mock",
                        id="script-queue-mock",
                        onclick="window.emitMockSseCodeRunEvent()",
                    ),
                    cls="script-queue-submit-row",
                ),
                cls="script-queue-editor-shell",
            ),
            Div(
                Div(
                    H3("Pending Queue", cls="script-queue-subtitle"),
                    P(
                        "Approve the selected script to send it to Pyodide, or cancel to drop it.",
                        cls="text-dim",
                        style="margin-top: -0.25rem; margin-bottom: 0.5rem; font-size: 0.8rem;",
                    ),
                    Div(
                        "No scripts queued yet.",
                        id="script-queue-empty",
                        cls="fallback-text script-queue-empty",
                    ),
                    Div(
                        id="script-queue-list",
                        cls="script-queue-list scroll-area",
                        tabindex="0",
                    ),
                    cls="script-queue-body",
                ),
                cls="script-queue-stack",
            ),
            cls="panel-body",
        ),
        Footer(
            Div(
                Button(
                    "Approve Selected",
                    type="button",
                    cls="btn-vibrant script-queue-action",
                    id="script-queue-approve-btn",
                    onclick="window.approveSelectedScript()",
                    disabled=True,
                ),
                Button(
                    "Cancel Selected",
                    type="button",
                    cls="btn-mini btn-outline script-queue-action",
                    id="script-queue-cancel-btn",
                    onclick="window.cancelSelectedScript()",
                    disabled=True,
                ),
                cls="script-queue-actions",
            ),
            Div(
                "Front-end only queue. Submit inserts a synthetic SSE event.",
                cls="text-dim script-queue-note",
            ),
            cls="panel-foot script-queue-foot",
        ),
        cls="panel panel-right queue-panel shadow-premium",
    )


def ThreePanelLayout(*c):
    """Wrap the three panels in the page-level main container."""

    return Main(*c, cls="app-shell", id="main-content")


def FourPanelLayout(*c):
    """Wrap the four panels in the page-level main container."""

    return Main(*c, cls="app-shell", id="main-content")
