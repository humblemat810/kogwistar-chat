from fasthtml.common import *
from graph_api import GraphAPI
import os
import asyncio
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse

# Import UI Components
from components import ThreePanelLayout, Sidebar, ChatPanel, RightPanel

load_dotenv()

hdrs = (
    Link(rel='stylesheet', href='https://cdn.jsdelivr.net/npm/inter-ui@3.19.3/inter.min.css'),
    Script(src="https://unpkg.com/htmx.org@1.9.10/dist/ext/sse.js"),
    Link(rel="stylesheet", href="/static/style.css"),
    Script(src="/static/app.js", defer=True)
)

app, rt = fast_app(
    debug=True, 
    hdrs=hdrs,
    secret_key=os.getenv("SECRET_KEY", "default-secret-key")
)

graph_api = GraphAPI()

def Layout(*c):
    return Title("GraphRAG Chat"), ThreePanelLayout(*c)

@rt("/login")
def get():
    return Title("Login"), Main(
        Section(cls="login-section panel shadow-premium", style="margin: auto; margin-top: 10vh; max-width: 400px; padding: 2rem;")(
            Header(H1("Welcome to GraphRAG", cls="vibrant-text")),
            Form(hx_post="/login", hx_target="body")(
                Label("Username"),
                Input(name="username", placeholder="Enter your username", required=True, cls="chat-input"),
                Button("Login", cls="btn-vibrant block-btn", style="margin-top: 1.5rem")
            )
        )
    )

@rt("/login")
async def post(username: str, session):
    try:
        token = await graph_api.get_dev_token(username)
        session['token'] = token
        session['username'] = username
        return Redirect("/")
    except Exception as e:
        return Div(f"Login failed: {str(e)}", cls="error-msg")

@rt("/")
async def get(session):
    if 'token' not in session: return Redirect("/login")
    token = session['token']
    username = session['username']
    
    # Temporarily we don't have the backend API, so this returns []
    # or we can auto-create one if none exist.
    convs = await graph_api.list_conversations(token, username)
    if not convs:
        # Create a new conversation if user has none
        conv_id = await graph_api.create_conversation(token, username)
        return Redirect(f"/c/{conv_id}")
    else:
        # Redirect to latest
        latest_conv = convs[0]['id']
        return Redirect(f"/c/{latest_conv}")

@rt("/c/{conv_id}")
async def get_conv(conv_id: str, session):
    if 'token' not in session: return Redirect("/login")
    token = session['token']
    username = session['username']
    
    convs = await graph_api.list_conversations(token, username)
    # If the backend is not ready, we mock the current conversation in the list
    if not any(c.get('id') == conv_id for c in convs):
        convs.insert(0, {'id': conv_id, 'turn_count': 0})
        
    session['conversation_id'] = conv_id
    
    # Load past transcript
    transcript = await graph_api.get_transcript(token, conv_id)
    
    return Layout(
        Sidebar(conversations=convs),
        ChatPanel(messages=transcript, current_conv_id=conv_id),
        RightPanel()
    )

@rt("/new-chat")
async def post_new(session):
    if 'token' not in session: return Redirect("/login")
    token = session['token']
    username = session['username']
    conv_id = await graph_api.create_conversation(token, username)
    return Redirect(f"/c/{conv_id}")

@rt("/send-message")
async def post_msg(message: str, conv_id: str, session):
    if not message: return ""
    token = session.get('token')
    
    user_msg = Div(
        Div(session.get('username', 'You'), cls="msg-sender"),
        Div(message, cls="msg-body user-msg shadow-premium"),
        cls="chat-message-container align-right"
    )
    
    run_id = await graph_api.submit_turn(token, conv_id, message)
    
    assistant_placeholder = Div(
        Div("Assistant", cls="msg-sender"),
        Div(id=f"run-{run_id}", cls="msg-body assistant-msg streaming-content shadow-premium",
            hx_ext="sse", 
            sse_connect=f"/events/{run_id}",
            sse_swap="message")(
            Span("...", cls="thinking-dot"),
        ),
        cls="chat-message-container"
    )
    
    return user_msg, assistant_placeholder

@rt("/events/{run_id}")
async def get_events(run_id: str, session):
    token = session.get('token')
    conv_id = session.get('conversation_id')
    
    async def event_generator():
        cumulative_text = ""
        has_started_output = False
        try:
            async for event in graph_api.stream_events(token, conv_id, run_id):
                event_type = event.get("type")
                payload = event.get("payload", {})
                
                if not has_started_output:
                    if event_type == "run.stage":
                        stage = payload.get("stage", "")
                        # Sent to the events-window via App.js interception or just embedded here
                        yield dict(data=f'<div class="thinking-state" id="ts-{run_id}"><span class="thinking-dot">Thinking...</span> <small>{stage}</small></div>', event="message")
                    elif event_type == "reasoning.summary":
                        summary = payload.get("summary", "")
                        yield dict(data=f'<div class="thinking-state" id="rs-{run_id}"><span class="thinking-dot">Thinking...</span> <small>{summary}</small></div>', event="message")
                
                if event_type == "output.delta":
                    has_started_output = True
                    delta = payload.get("delta", "")
                    cumulative_text += delta
                    html_output = cumulative_text.replace("```python", "<pre><code>").replace("```", "</code></pre>")
                    yield dict(data=html_output, event="message")
                
                elif event_type == "run.completed":
                    html_output = cumulative_text.replace("```python", "<pre><code>").replace("```", "</code></pre>")
                    yield dict(data=html_output, event="message")
                    break
        except Exception as e:
            yield dict(data=f'<div class="error-msg">Stream Error: {str(e)}</div>', event="message")

    return EventSourceResponse(event_generator())

@rt("/static/{path:path}")
def static_files(path: str):
    return FileResponse(f"static/{path}")

@rt("/logout")
def get_logout(session):
    session.clear()
    return Redirect("/login")

serve()
