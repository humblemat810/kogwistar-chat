# Chat Client Debugging Checklist

This note captures the failure modes we hit while wiring the FastHTML/HTMX chat client against the server.

## Common Pitfalls
- fresh browser should use api/auth/login endpoint to get redirected to 5173 port, and this htmx server should be configured using 5173 port
- `localhost` is not always the same backend as `127.0.0.1` on Windows.
- Port `28110` is shared by multiple possible listeners in this repo setup, including Docker and the local VS Code-launched server.
- Port `5173` is the frontend dev server URL. The backend expects the UI to exist there, but compose does not start it.
- `DEV_AUTH_NS` controls which namespaces the dev JWT can access. If it is `docs`, the dev user will not reach conversation/workflow routes.
- `/auth/dev-token` expects a JSON body. Empty or form-encoded requests fail before namespace validation.
- `AUTH_MODE=dev` changes auth behavior. If you are debugging login, confirm whether the server was started in `dev` or `oidc`.
- `/api/document.upsert_tree` requires the document to already exist.
- `persist_document_graph_extraction(...)` validates node and edge spans against the stored document content.
- `persist_document_graph_extraction(...)` also resolves edge endpoints, so endpoint IDs must be structurally valid.
- `curl` in PowerShell may not be the `curl.exe` binary you think it is.
- `curl http://localhost:28110/...` and `curl.exe http://127.0.0.1:28110/...` can hit different listeners.
- The request logger is separate from the app logic. A silent console does not mean the request failed.
- HTMX poll loops stop only when the replacement HTML no longer contains `hx_get`. If the server still returns a polling node, the browser will keep polling.
- HTMX SSE reconnects automatically when the client sees a broken or ended `EventSource`. If an SSE route returns plain HTML instead of a valid SSE message, the browser will reconnect forever.
- A route mounted by `hx-ext="sse"` must keep returning an `EventSourceResponse`, even for terminal/fallback branches. Returning a normal HTML fragment from an SSE route is a protocol bug.
- Session run state can be stale for old conversations. A historical run may already be finished even when the current browser session thinks `terminal=False`.
- Historical runs need a one-shot backend hydrate on first inspection so the frontend can persist `terminal`, `stage`, `last_seq`, and event history.
- The right-panel mode can also be stale. If the session remembers `mode=live` for a run that is already terminal, the UI can keep trying to remount live SSE until the server forces it back to `history`.
- A terminal run must disable assistant polling by returning `poll_url=None`.
- A terminal run must disable debug live mode by omitting `live_url` and removing any SSE shim.
- Testing only a single handler call is not enough for transport bugs. Repeated-call tests are needed to prove the app does not re-open polling or SSE after terminal state is persisted.
- Browser console logs alone are not enough. Add route-entry logs with `run_id`, `after_seq`, `terminal`, `panel_mode`, and whether the run came from stored session state.

## Q&A

- Q: Why do I get `Not Found` but no log line?
  - A: You may be hitting a different listener on `localhost`, or the request may be going through a path handled outside the logger you are watching. Use `curl.exe` and prefer `127.0.0.1`.

- Q: Why does the dev account only see `docs`?
  - A: The VS Code dev-auth profile was previously set to `DEV_AUTH_NS=docs`. Set it to `docs,conversation,workflow,wisdom` if you want full access.

- Q: Why does `/auth/dev-token` fail before it even checks `ns`?
  - A: The endpoint parses JSON first. If the client sends no body or non-JSON form data, you get a JSON decode failure instead of an auth validation error.

- Q: Why does `/api/document.upsert_tree` fail on my seed payload?
  - A: The document must exist first, and the spans must match the document content. A mismatched excerpt or missing document will fail validation.

- Q: Why does a debug workflow appear to work in tests but not in the live server?
  - A: Test fixtures often use fake backends and direct engine setup. The live server also needs the correct auth namespace, the document row, and a valid backend route.

- Q: Why does the conversation client get `401` on chat routes?
  - A: The token probably lacks the required namespace or role. Check `/api/auth/me` and verify the `ns` claim.

- Q: Why do SSE events sometimes disappear?
  - A: The stream may be connected to the wrong port or the wrong process, or the client may be using `localhost` while the server is bound on `127.0.0.1`.

- Q: Why does `/debug/run-events/{run_id}/stream` keep reconnecting every few seconds even after the run is complete?
  - A: That usually means the route was mounted by HTMX SSE, but the terminal branch returned plain HTML instead of a one-shot SSE response, or the panel still believed the run was live because terminal state was stale in session.

- Q: Why does a finished run still look live after reopening the page?
  - A: The session may remember `debug_panel.mode=live` while the run itself is already terminal. The app must hydrate the run once from the backend and then overwrite the panel mode back to `history`.

- Q: Why does a past run from another session start scanning again when I click Live SSE?
  - A: The current browser session may not have stored run state for it yet. Without one backend hydrate, the app will default to `terminal=False`, `stage=queued`, and try to open live SSE incorrectly.

## Setup Checklist

- Start the backend on a known port and verify it is the process you intend to test.
- Start or point the frontend at `http://localhost:5173/` if you are using the default UI URL.
- Confirm the VS Code launch profile or compose profile sets the intended auth mode.
- Confirm `DEV_AUTH_NS` contains the namespaces the client needs.
- Confirm `/auth/dev-token` is being called with `Content-Type: application/json`.
- Confirm `UI_URL` matches the actual browser app URL.
- Confirm `OIDC_REDIRECT_URI` points back to the backend when using OIDC.
- Mint a token and inspect `/api/auth/me` before testing chat routes.
- Verify `/api/conversations` works before testing answer submission.
- If a run keeps polling or reconnecting, inspect whether the returned fragment still contains `hx_get`, `sse_connect`, or a live shim node.
- If a historical run behaves as active, verify the backend status with `GET /api/runs/{run_id}` and confirm the frontend persisted `terminal=True` in session.
- If an SSE route is terminal, confirm it still returns a valid SSE response shape, not a raw HTML fragment.
- Create the document before calling `/api/document.upsert_tree`.
- Keep span excerpts and offsets aligned with the document content.
- Use `curl.exe -v http://127.0.0.1:28110/...` when checking the local debug server.

## Recommended Dev Auth Settings

Use these values for the VS Code debug profile when you want full local access:

```json
{
  "AUTH_MODE": "dev",
  "DEV_AUTH_EMAIL": "dev@example.com",
  "DEV_AUTH_SUBJECT": "dev",
  "DEV_AUTH_NAME": "Dev User",
  "DEV_AUTH_ROLE": "rw",
  "DEV_AUTH_NS": "docs,conversation,workflow,wisdom",
  "UI_URL": "http://127.0.0.1:5173/"
}
```

## Fast Checks

- `GET /api/auth/me` should show the expected `role` and `ns`.
- `GET /health` should return the server you think you started.
- `GET /api/conversations` should return data once you have a valid token.
- `POST /api/document` should succeed before any tree upsert.
- `POST /api/document.upsert_tree` should only use spans that match the document text.
