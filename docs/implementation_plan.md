# FastHTML HTMX Chat Application with Pyodide Worker

This project implements a modern, premium chat application using FastHTML and HTMX, integrated with an existing GraphRAG server. It features OAuth2-style login and a client-side Pyodide worker to offload server processing for Python script execution.

## Proposed Changes

### Core Chat Application
- **FastHTML & HTMX**: Use FastHTML for the server-side logic and HTMX for dynamic UI updates without page reloads.
- **SSE Integration**: Connect to the GraphRAG server's SSE endpoint (`/api/conversations/{conversation_id}/runs/{run_id}/events`) to receive real-time updates.
- **Event Handling**:
  - `run.stage`: Updates the high-level status of the agent (e.g., "Preparing", "Retrieving").
  - `reasoning.summary`: Displays granular "thinking" messages in a designated UI area.
  - `output.delta`: Streams the assistant's response tokens into the chat interface.
  - `run.completed`/`failed`: Manages the lifecycle and final state of the chat turn.
- **Premium Design**: Vanilla CSS with a glassmorphic aesthetic, smooth animations, and a modern color palette.

- **Chat Features**:
    - SSE output
    - run trace
    - source/reference panel
    - “replay from here” button on old turns as shown in the diagram:

```
Turn 1 -> Turn 2 -> Turn 3 -> Turn 4
                  \
                   -> Turn 3b -> Turn 4b
```

### Authentication
- **OAuth2 Login**: Implement a login page that uses the existing `/auth/dev-token` endpoint on the GraphRAG server for username/password authentication.
- **Session Management**: Store JWT tokens in the session for authenticated requests.

### Pyodide Worker Integration
- **Client-Side Execution**: Offload LLM-generated Python code to a background Web Worker using Pyodide.
- **Worker Logic**:
  - `PyodideWorker`: Initializes Pyodide, loads necessary packages (e.g., NumPy), and executes code sent from the main thread.
  - **Secure Execution**: Using Web Workers provides a layer of isolation for client-side script running.

## Project Structure

### [Component] HTMX Chat App

#### [NEW] [main.py](file:///c:/Users/chanh/Documents/htmxchat/main.py)
The entry point for the FastHTML application, handling routing, session management, and HTMX partials.

#### [NEW] [graph_api.py](file:///c:/Users/chanh/Documents/htmxchat/graph_api.py)
A utility for interacting with the GraphRAG server, including JWT authentication and SSE client logic.

#### [NEW] [static/style.css](file:///c:/Users/chanh/Documents/htmxchat/static/style.css)
Premium Vanilla CSS for the application interface.

#### [NEW] [static/worker.js](file:///c:/Users/chanh/Documents/htmxchat/static/worker.js)
The Pyodide Web Worker implementation for client-side Python execution.

#### [NEW] [requirements.txt](file:///c:/Users/chanh/Documents/htmxchat/requirements.txt)
Python dependencies: `python-fasthtml`, `httpx`, `python-jose`, `pydantic`.

## Verification Plan

### Automated Tests
- **SSE Flow**: Verify that all specific SSE events (`run.stage`, `reasoning.summary`, `output.delta`) correctly update the UI.
- **Auth Flow**: Test login, session persistence, and logout with the `/auth/dev-token` endpoint.
- **Pyodide Worker**: Run a suite of Python scripts in the worker and verify the outputs.
- **UI Responsiveness**: Verify the layout behaves correctly across different resolutions.

## Future Vision (Out of Scope)
- **Client-Side GraphRAG**: Explore running a sandboxed version of the GraphRAG engine within the Pyodide Web Worker.
- **Privacy-First Memory**: Allow users to store personal Knowledge Graph data and memory retrieval locally on their device, ensuring highest privacy while leveraging LLM capabilities.
- **Local KG Indexing**: Investigate indexing personal documents directly in the browser using Pyodide's filesystem and local storage.

### Manual Verification
- **User Experience**: Walk through the chat flow, checking for smooth animations and consistent style.
- **Thinking State**: Ensure the "agent thinking" UI provides clear feedback during long-running tasks.
