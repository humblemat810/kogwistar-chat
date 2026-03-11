from fasthtml.common import *

def Sidebar(conversations=None):
    if conversations is None:
        conversations = []
        
    conv_items = [
        Li(
            A(f"Chat {c.get('id', '...')} ({c.get('turn_count', 0)} turns)", 
              href=f"/c/{c.get('id')}", 
              cls="conv-link"),
            cls="conv-item"
        ) for c in conversations
    ]
    
    if not conv_items:
        conv_items = [Li("No previous conversations", cls="fallback-text")]
    
    return Aside(
        Div(
            H2("History", cls="sidebar-title"),
            Ul(*conv_items, cls="conv-list", id="conversation-list"),
            cls="sidebar-content"
        ),
        Div(
            Button("+ New Chat", cls="btn-vibrant block-btn", hx_post="/new-chat"),
            A("Logout", href="/logout", cls="logout-link"),
            cls="sidebar-footer"
        ),
        cls="panel sidebar-panel shadow-premium"
    )

def ChatPanel(messages=None, current_conv_id=None):
    if messages is None:
        messages = []
        
    msg_elements = []
    for msg in messages:
        sender = "user" if msg.get("role") == "user" else "Assistant"
        content = msg.get('content', '')
        cls_name = "user-msg" if sender == "user" else "assistant-msg"
        align_cls = "align-right" if sender == "user" else "align-left"
        
        # Simple markdown pre formatting for history
        content_html = content.replace("```python", "<pre><code>").replace("```", "</code></pre>") if "```" in content else content
        
        msg_elements.append(
            Div(
                Div(sender.capitalize(), cls="msg-sender"),
                Div(NotStr(content_html), cls=f"msg-body {cls_name} shadow-premium"),
                cls=f"chat-message-container {align_cls}"
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
                    Button("Send", cls="btn-vibrant")
                ),
                hx_on__after_request="this.querySelector('#message-input').value = ''"
            )
        ),
        cls="panel center-panel shadow-premium"
    )

def RightPanel():
    return Aside(
        H2("Agent Events", cls="sidebar-title"),
        Div(id="events-window", cls="events-window", style="flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.5rem;"),
        cls="panel right-panel shadow-premium"
    )

def ThreePanelLayout(*c):
    return Main(*c, cls="layout-3-panel container")
