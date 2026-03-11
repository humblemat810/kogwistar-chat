# HTMX Chat App — Progress Summary

> **Date**: 2026-03-12  
> **Scope**: Two repos — `htmxchat` (frontend) and `graphrag_v2_working_tree` (backend)

---

## What Has Been Built

### 1. Frontend: FastHTML 3-Panel Chat App (`htmxchat/`)

A FastHTML + HTMX chat application that connects to the GraphRAG backend API.

| File | Purpose | Status |
|------|---------|--------|
| `main.py` | Main app — routes, auth, SSE streaming | ✅ Working |
| `components.py` | Modular UI: `Sidebar`, `ChatPanel`, `RightPanel`, `ThreePanelLayout` | ✅ Working |
| `graph_api.py` | httpx client for GraphRAG backend | ✅ Fixed (URLs corrected) |
| `static/style.css` | Glassmorphism dark theme, 3-column grid | ✅ Working |
| `static/app.js` | Pyodide worker + SSE handling | ✅ Created |
| `.env` | `GRAPHRAG_SERVER_URL`, `SECRET_KEY` | ✅ Configured |
| `.gitignore` | Standard Python/web ignores | ✅ Created |
| `test_api.py` | Diagnostic script for API testing | ✅ Utility |

### 2. Backend Changes (`graphrag_v2_working_tree/`)

| File | Change | Status |
|------|--------|--------|
| `server/auth/router.py` | Added `redirect_uri` param (dev-only, 400 in prod) | ✅ Done |
| `server/chat_api.py` | Added `GET /api/conversations` listing endpoint | ✅ Done |
| `server/chat_service.py` | Added `list_conversations_for_user()` method | ✅ Done |
| `engine_core/embedding_factory.py` | **NEW** — Pluggable embedding providers | ✅ Done |
| `engine_core/engine.py` | Uses `get_embedding_function()` factory | ✅ Done |
| `compose.yml` | Added `OLLAMA_HOST`, `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `ollama` service | ✅ Done |

---

## Architecture

```
┌─────────────────┐     ┌──────────────────────────────┐     ┌─────────────┐
│  Browser         │────▶│  FastHTML App (:5001)         │────▶│  GraphRAG    │
│  (HTMX + SSE)    │◀────│  main.py + components.py     │◀────│  Backend     │
│                  │     │  graph_api.py (httpx client)  │     │  (:28110)    │
└─────────────────┘     └──────────────────────────────┘     │  Docker      │
                                                             │  Chroma +    │
                                                             │  Ollama      │
                                                             └─────────────┘
```

## Auth Flow (Dev Mode)

```
1. User visits /login
2. Clicks "Login with GraphRAG →"
    → Redirects to: GET /api/auth/login?redirect_uri=http://localhost:5001/
3. Backend mints JWT (dev mode), redirects:
    → 307 → http://localhost:5001/?token=eyJ...
4. main.py / route stores token in session
5. Fetches user info from /api/auth/me
6. Redirects to /c/{first_conversation_id}
```

**Security**: `redirect_uri` is only accepted when `AUTH_MODE=dev`. In prod, it raises HTTP 400.

## API Endpoint Mapping

| Frontend calls (graph_api.py) | Backend route (chat_api.py) |
|---|----|
| `GET /api/auth/login?redirect_uri=...` | `auth/router.py` — mints JWT, redirects |
| `GET /api/auth/me` | Returns user info from token |
| `POST /api/conversations` | Creates conversation node in graph |
| `GET /api/conversations` | Lists user's conversations |
| `GET /api/conversations/{id}/turns` | Gets conversation transcript |
| `POST /api/conversations/{id}/turns:answer` | Submits user message, starts workflow run |
| `GET /api/runs/{run_id}/events` | SSE stream of run events |

## Embedding Factory

New `embedding_factory.py` supports pluggable embedding providers:

```
EMBEDDING_PROVIDER=ollama    → OllamaEmbeddingFunction (default)
EMBEDDING_PROVIDER=openai    → OpenAIEmbeddingFunction
EMBEDDING_PROVIDER=azure     → AzureEmbeddingFunction  
EMBEDDING_PROVIDER=google    → GoogleEmbeddingFunction
```

Additional env vars per provider:
- **ollama**: `OLLAMA_HOST` (default: `http://host.docker.internal:11434`)
- **openai**: `OPENAI_API_KEY`
- **azure**: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`
- **google**: `GOOGLE_API_KEY`

Docker profiles:
- `--profile chroma` — app + Chroma (uses host Ollama by default)
- `--profile chroma --profile ollama` — app + Chroma + Docker Ollama

---

## What Works ✅

1. **Login flow** — redirect-based auth with JWT tokens
2. **Conversation creation** — creates graph nodes via API
3. **Conversation listing** — sidebar shows user's conversations
4. **3-panel UI** — glassmorphism dark theme with sidebar, chat, events panels
5. **Embedding factory** — pluggable providers via env vars
6. **Docker setup** — Chroma + Ollama with proper host networking

## What's Next / Known Issues ⚠️

1. **Chat message submission** — URL fix just applied (`turns:answer`), needs testing
2. **SSE event streaming** — URL fix applied (`/api/runs/{id}/events`), needs testing
3. **Event panel** — Right panel shows agent events but SSE integration needs verification
4. **Transcript rendering** — Loading past messages on conversation switch needs testing
5. **Pyodide integration** — Python code execution in browser (app.js has the worker scaffolding)
6. **Error handling** — Need graceful UI errors instead of 500 pages
7. **Session expiry** — JWT tokens expire; need refresh logic or re-login prompt
8. **Mobile responsive** — CSS grid needs media queries for smaller screens

---

## How to Run

### Prerequisites
- Python 3.11+ with venv
- Docker Desktop
- Ollama running locally on port 11434

### Start Backend
```bash
# Set AUTH_MODE=dev for local development
$env:AUTH_MODE="dev"
docker compose -f <graphrag_repo>/compose.yml --profile chroma up -d --build
```

### Start Frontend
```bash
cd htmxchat
.venv\Scripts\activate
python main.py
# → http://localhost:5001
```
