# kogwistar-htmxchat
This repo works with kogwistar repo https://github.com/humblemat810/kogwistar
<p align="center">
    <img src="assets/kogwistar_256.png" width="200"/>
</p>




`htmxchat` is a FastHTML + HTMX chat application that demonstrates a full Python stack with live assistant streaming and a client-side Pyodide worker for running arbitrary Python scripts.

This repo serves two goals:

1. Show that the substrate works. The app is built to prove the server-side rendering, HTMX partial updates, and SSE transport can support real interactive workflows.
2. Experiment with agent-style execution. The browser includes a Pyodide worker so Python snippets can be queued, approved, cancelled, and executed on the client side.
![screenshot](assets\screenshot.png)
## What This Project Does

- Renders the entire UI from Python using FastHTML.
- Uses HTMX for partial updates and event-driven interactions.
- Streams assistant progress over SSE.
- Exposes a debug/event panel for inspecting live and historical run events.
- Provides a fourth-panel script queue that can collect Python snippets and send them to a Pyodide worker.

## Architecture

The app is intentionally split into three layers:

- `main.py` handles routes, session state, and SSE relay logic.
- `components.py` builds the FastHTML fragments that HTMX swaps into the page.
- `static/app.js` handles browser-side behavior, including SSE event handling, debug tracing, and the Pyodide worker integration.

The important idea is that the server emits HTML fragments and the browser swaps them in. For live runs, the backend stream is relayed through the app and then presented in the browser as HTMX-friendly SSE updates.

## Project Layout

- `main.py` - application entrypoint and route handlers
- `components.py` - FastHTML component builders
- `static/app.js` - browser glue, SSE hooks, and Pyodide worker control
- `static/style.css` - styling for the shell and panels
- `docs/` - deeper notes and protocol guides
- `tests/` - transport and integration tests

## Requirements

- Python 3.11+
- A backend service that provides the GraphRAG chat API
- A browser with JavaScript enabled

The app expects the backend API to be available locally or through the configured base URL.

## Running The App

Typical local development flow:

```bash
python -m pip install -r requirements.txt
python main.py
```

If your environment uses a different entrypoint or dev server command, use the project-specific script you already have configured.

## Streaming Modes

The assistant stream supports two modes:

- `poll` - the browser refreshes the assistant bubble on a timer
- `sse` - the browser receives live events through SSE

Set `CHAT_STREAM_MODE=sse` to use the live streaming path.

## Pyodide Worker

The browser includes a Pyodide worker so scripts can be run client-side after approval.

Current behavior:

- code blocks received through SSE can get a `Run in Browser` button
- the fourth panel can queue scripts for approval
- approved scripts are sent to the worker
- the worker can be restarted automatically on timeout or crash

## Important Sandbox Note

The sandbox in this project is **not** the Pyodide worker.

The Pyodide worker runs inside the browser and can access the browser app state and DOM-driven UI flow. That makes the browser application the practical boundary for this experiment.

So when we say "sandbox" here, we mean the browser app plus the worker, not a server-side isolation boundary.

## SSE And Code Delivery

The current client can queue Python code when the SSE message contains rendered HTML with `<pre><code>...</code></pre>` blocks.

That means:

- raw JSON SSE events are not enough by themselves
- the relay should emit HTML fragments that HTMX can swap into the UI
- the browser will then parse the rendered DOM and queue the code block

See:

- `docs/server_side_sse_code_run_guide.md`
- `docs/pyodide_worker_communication.md`

## Tests

The repo includes transport and UI regression tests under `tests/`.

Useful examples:

- SSE relay coverage
- debug panel live-stream coverage
- Pyodide worker timeout and restart coverage

If you have the test dependencies installed, run the relevant test module directly with `pytest`.

## Notes For Contributors

- Keep the session payload small. Cookie-backed session state is easy to overflow.
- Prefer HTMX-friendly HTML fragments over custom front-end state when possible.
- Treat the client-side Pyodide worker as a browser capability, not a trusted server sandbox.
- When changing SSE behavior, verify both the backend relay logs and the browser-side HTMX event traces.

## Related Docs

- [FastHTML HTMX Chat API Guide](./fasthtml_htmx_chat_api_guide.md)
- [Server-Side SSE Code Run Guide](./docs/server_side_sse_code_run_guide.md)
- [Pyodide Worker Communication](./docs/pyodide_worker_communication.md)

