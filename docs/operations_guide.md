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
