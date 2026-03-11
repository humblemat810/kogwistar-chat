from fasthtml.common import *
from graph_api import GraphAPI
import os
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse

# Import UI Components
from components import ThreePanelLayout, Sidebar, ChatPanel, RightPanel

load_dotenv()

SERVER_URL = os.getenv("GRAPHRAG_SERVER_URL", "http://localhost:28110")
CHAT_APP_PORT = int(os.getenv("CHAT_APP_PORT", "5001"))
CHAT_APP_URL = os.getenv("CHAT_APP_URL", f"http://localhost:{CHAT_APP_PORT}")

hdrs = (
    Link(rel='stylesheet', href='https://cdn.jsdelivr.net/npm/inter-ui@3.19.3/inter.min.css'),
    # HTMX 2.x compatible SSE extension (separate package)
    Script(src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"),
    Link(rel="stylesheet", href="/static/style.css"),
    Script(src="/static/app.js", defer=True),
    # Prevent SSE auto-reconnect spam (useful when debugging backend with breakpoints)
    Script("""
        document.addEventListener('htmx:sseError', function(evt) {
            console.warn('[SSE] Connection error, NOT auto-reconnecting (debug mode)');
            var source = evt.detail?.source;
            if (source) { source.close(); }
            evt.preventDefault();
        });
    """),
)

app, rt = fast_app(
    debug=True, 
    hdrs=hdrs,
    secret_key=os.getenv("SECRET_KEY", "default-secret-key")
)

graph_api = GraphAPI()

# ---------------------------------------------------------------------------
# Auth flow:
#   1. /login shows a page with a "Login with GraphRAG" button
#   2. Button redirects browser to GRAPHRAG_SERVER/api/auth/login
#   3. Backend (dev mode) auto-mints JWT and redirects to UI_URL/?token=...
#   4. Our / route catches ?token=, stores in session, proceeds to chat
# ---------------------------------------------------------------------------

def Layout(*c):
    return Title("GraphRAG Chat"), ThreePanelLayout(*c)

@rt("/login")
def get_login():
    """Show login page with a redirect button to the GraphRAG auth endpoint."""
    # Pass our own URL as redirect_uri so the backend redirects back here (dev mode only)
    from urllib.parse import urlencode
    login_url = f"{SERVER_URL}/api/auth/login?" + urlencode({"redirect_uri": f"{CHAT_APP_URL}/"})
    return Title("Login"), Main(
        Section(cls="panel shadow-premium", style="margin: auto; margin-top: 10vh; max-width: 480px; padding: 2.5rem;")(
            Header(H1("GraphRAG Chat", cls="vibrant-text", style="font-size: 2rem; text-align: center;")),
            P("Click below to authenticate with the GraphRAG server.", 
              style="text-align: center; color: var(--text-dim); margin-bottom: 2rem;"),
            A("Login with GraphRAG →", href=login_url, 
              cls="btn-vibrant block-btn", 
              style="display: block; text-align: center; text-decoration: none; padding: 1rem; font-size: 1.1rem;"),
        )
    )

@rt("/")
async def get_home(session, token: str = None):
    """
    Landing page. If ?token= is present (redirect from backend auth), store it.
    Otherwise check session for existing token.
    """
    # Handle token from auth redirect
    if token:
        session['token'] = token
        # Fetch user info from the backend
        try:
            me = await graph_api.get_me(token)
            session['username'] = me.get('display_name') or me.get('email') or me.get('user_id', 'User')
            session['user_id'] = me.get('user_id', '')
        except Exception:
            session['username'] = 'User'
            session['user_id'] = ''
        return Redirect("/")  # Clean URL by redirecting without query params

    # Check if already authenticated
    if 'token' not in session:
        return Redirect("/login")

    token = session['token']
    user_id = session.get('user_id', '')
    
    # Try to list existing conversations
    try:
        convs = await graph_api.list_conversations(token, user_id)
    except Exception:
        convs = []
    
    if not convs:
        # Create a new conversation if user has none
        try:
            conv_id = await graph_api.create_conversation(token, user_id)
            return Redirect(f"/c/{conv_id}")
        except Exception as e:
            return Title("Error"), Main(
                Div(f"Failed to create conversation: {e}", cls="error-msg",
                    style="margin: 2rem; padding: 1rem;")
            )
    else:
        return Redirect(f"/c/{convs[0]['id']}")

@rt("/c/{conv_id}")
async def get_conv(conv_id: str, session):
    if 'token' not in session: return Redirect("/login")
    token = session['token']
    user_id = session.get('user_id', '')
    username = session.get('username', 'User')
    
    try:
        convs = await graph_api.list_conversations(token, user_id)
    except Exception:
        convs = []
    
    # Ensure current conversation shows in sidebar even if listing fails
    if not any(c.get('id') == conv_id for c in convs):
        convs.insert(0, {'id': conv_id, 'turn_count': 0})
        
    session['conversation_id'] = conv_id
    
    # Load past transcript
    try:
        transcript = await graph_api.get_transcript(token, conv_id)
    except Exception:
        transcript = []
    
    return Layout(
        Sidebar(conversations=convs),
        ChatPanel(messages=transcript, current_conv_id=conv_id),
        RightPanel()
    )

@rt("/new-chat")
async def post_new(session):
    if 'token' not in session: return Redirect("/login")
    token = session['token']
    user_id = session.get('user_id', '')
    conv_id = await graph_api.create_conversation(token, user_id)
    return Redirect(f"/c/{conv_id}")

@rt("/send-message")
async def post_msg(message: str, conv_id: str, session):
    if not message: return ""
    token = session.get('token')
    username = session.get('username', 'You')
    
    user_msg = Div(
        Div(username, cls="msg-sender"),
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
                # Backend sends flat dicts: {"event_type": "...", "stage": "...", "delta": "...", ...}
                event_type = event.get("event_type")
                
                if not has_started_output:
                    if event_type == "run.stage":
                        stage = event.get("stage", "")
                        yield dict(data=f'<div class="thinking-state" id="ts-{run_id}"><span class="thinking-dot">Thinking...</span> <small>{stage}</small></div>', event="message")
                    elif event_type == "reasoning.summary":
                        summary = event.get("summary", "")
                        yield dict(data=f'<div class="thinking-state" id="rs-{run_id}"><span class="thinking-dot">Thinking...</span> <small>{summary}</small></div>', event="message")
                
                if event_type == "output.delta":
                    has_started_output = True
                    delta = event.get("delta", "")
                    cumulative_text += delta
                    html_output = cumulative_text.replace("```python", "<pre><code>").replace("```", "</code></pre>")
                    yield dict(data=html_output, event="message")
                
                elif event_type == "run.completed":
                    if not cumulative_text:
                        cumulative_text = "(No output)"
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

serve(port=CHAT_APP_PORT)
