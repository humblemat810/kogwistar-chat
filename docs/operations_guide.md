# Operations Guide

This repository is the FastHTML/HTMX frontend for the GraphRAG chat experience.
The backend GraphRAG service must be running separately and reachable through `GRAPHRAG_SERVER_URL`.

## Prerequisites

- Python 3.11+
- An installed virtual environment with the project dependencies from `requirements.txt`
- A running GraphRAG backend

## Environment Variables

- `GRAPHRAG_SERVER_URL` - GraphRAG backend base URL, for example `http://localhost:28110`
- `CHAT_APP_PORT` - frontend port, default `5001`
- `CHAT_APP_URL` - frontend base URL used for the login redirect, default `http://localhost:5001`
- `CHAT_WORKFLOW_ID` - backend answer workflow, default `debug.rag.v1`; set `agentic_answering.v2` to use the production workflow
- `CHAT_POLL_INTERVAL_MS` - polling interval for non-SSE mode, default `750`
- `CHAT_STREAM_MODE` - `poll` or `sse`, default `poll`
- `CHAT_LOG_LEVEL` - logging level, default `INFO`
- `SECRET_KEY` - session signing key

## Automated Start

Use the PowerShell script at `start_chat_app.ps1`.

Examples:

```powershell
.\start_chat_app.ps1
.\start_chat_app.ps1 -NoBrowser
.\start_chat_app.ps1 -Port 5001
.\start_chat_app.ps1 -PythonExe .\.venv\Scripts\python.exe
```

The script:

- sets the frontend port
- starts `main.py` in a separate process
- waits for the port to come up
- opens the login page in the browser unless `-NoBrowser` is used

## Manual Start

```powershell
$env:GRAPHRAG_SERVER_URL = "http://localhost:28110"
$env:CHAT_APP_PORT = "5001"
python main.py
```

Then open:

- `http://localhost:5001/login`

## Usage

1. Open the login page.
2. Click the GraphRAG login link.
3. After authentication, the app loads your conversations.
4. Select an existing conversation or start a new one.
5. Type a message and submit it.
6. Watch the assistant response stream in `poll` mode or `sse` mode.
7. Use the stop button on a streaming assistant message to cancel an in-flight run.

## Stop

- If launched manually in a terminal, press `Ctrl+C`.
- If launched by the script, close the spawned Python process.

## Troubleshooting

- If login redirects fail, confirm `CHAT_APP_URL` matches the browser address exactly.
- If conversations do not load, verify `GRAPHRAG_SERVER_URL` is reachable.
- If streaming does not work, try `CHAT_STREAM_MODE=poll` first, since it is the more tolerant fallback mode.

## HTMX/SSE Transport Pitfalls

- Polling only stops when the server returns replacement HTML without `hx_get`. Setting terminal state in Python is not enough if the rendered fragment still carries a poll URL.
- SSE routes must keep returning SSE responses all the way through terminal handling. If a route mounted by HTMX SSE returns plain HTML, the browser treats it as a broken stream and reconnects repeatedly.
- Historical runs can be missing from the current browser session. On the first inspection of an older run, the frontend should do one backend hydrate and persist `terminal`, `stage`, `last_seq`, and events before deciding whether to open live SSE.
- A saved debug panel can remember `mode=live` across reloads. If the selected run is already terminal, the server must overwrite the panel back to `history` and not leave a live shim in the DOM.
- The same stale-state issue applies to the assistant bubble. Once a run is terminal, the rendered bubble must remove `poll_url` so HTMX no longer emits interval requests.

## Recommended Debug Signals

- Watch `chat_app.log` for route-entry traces from `main.py`. The useful fields are `route`, `run_id`, `after_seq`, `terminal`, `panel_mode`, and `has_state`.
- In the browser console, use the `data_route_source` annotations to tell whether a request came from the assistant poll bubble, the assistant SSE shim, or the right-panel debug controls.
- If a route keeps firing, inspect the returned HTML fragment. Look specifically for `hx_get`, `sse_connect`, and any remaining live shim element.
