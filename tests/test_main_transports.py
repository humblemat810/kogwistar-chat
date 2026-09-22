from __future__ import annotations

import ast
import asyncio
import copy
import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from sse_starlette.sse import EventSourceResponse

# Keep the repo root importable when pytest executes from the tests directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import sse_contracts

pytestmark = pytest.mark.ci


class Session(dict):
    """Tiny dict-backed session stub for route tests."""


async def _first_sse_event(response: EventSourceResponse):
    async for item in response.body_iterator:
        return item
    return None


async def _collect_timed_sse_events(response: EventSourceResponse, *, limit: int) -> list[tuple[float, dict]]:
    items: list[tuple[float, dict]] = []
    loop = asyncio.get_running_loop()
    async for item in response.body_iterator:
        items.append((loop.time(), item))
        if len(items) >= limit:
            break
    return items


async def _fake_streamed_run_events(events: list[dict], *, delay_s: float = 0.03):
    for idx, event in enumerate(events):
        if idx:
            await asyncio.sleep(delay_s)
        yield event


def _make_terminal_run(session: Session, run_id: str = "run-1") -> None:
    main._init_run_state(session, run_id=run_id, conv_id="conv-1", user_text="hello")
    state = main._load_run_state(session, run_id)
    state["terminal"] = True
    state["stage"] = "completed"
    state["text"] = "final answer"
    main._save_run_state(session, state)


def test_session_helpers_seed_expected_defaults():
    session = Session()

    run_state = main._load_run_state(session, "run-defaults")
    panel_state = main._load_panel_state(session)
    debug_state = main._load_debug_state(session, "run-defaults")

    assert run_state["run_id"] == "run-defaults"
    assert run_state["stage"] == "queued"
    assert run_state["terminal"] is False
    assert panel_state["mode"] == "history"
    assert panel_state["paused"] is False
    assert debug_state["paused"] is False
    assert debug_state["resume_seq"] == 0
    assert debug_state["cleared"] is False


def test_run_state_save_strips_event_history_from_session():
    session = Session()
    run_id = "run-session-slim"
    main._init_run_state(session, run_id=run_id, conv_id="conv-slim", user_text="hello")
    state = main._load_run_state(session, run_id)
    main._append_run_event(
        state,
        {"seq": 1, "event_type": "output.delta", "delta": "x" * 1000},
        seq=1,
        event_type="output.delta",
        payload={"delta": "x" * 1000},
        label="Text",
        detail="x" * 1000,
    )

    main._save_run_state(session, state)

    assert "events" not in session["run_state"][run_id]
    reloaded = main._load_run_state(session, run_id)
    assert reloaded["events"]
    assert reloaded["events"][0]["event_type"] == "output.delta"


def test_output_completed_event_uses_data_payload_for_final_text():
    session = Session()
    run_id = "run-output-completed"
    main._init_run_state(session, run_id=run_id, conv_id="conv-output", user_text="hello")
    state = main._load_run_state(session, run_id)

    seq, event_type, payload, label, detail = main._ingest_run_event(
        state,
        {"seq": 1, "event_type": "output.completed", "data": "final answer from data"},
    )

    assert seq == 1
    assert event_type == "output.completed"
    assert payload["data"] == "final answer from data"
    assert label == "Output.completed"
    assert "final answer" in detail
    assert state["text"] == "final answer from data"
    assert state["stage"] == "completed"


def test_terminal_assistant_body_does_not_show_thinking_placeholder():
    body = main.render_assistant_body(run_id="run-terminal", terminal=True)

    rendered = repr(body)
    assert "Completed" in rendered
    assert "Thinking" not in rendered


def test_debug_live_mode_falls_back_for_terminal_runs(monkeypatch):
    session = Session(token="token-1")
    run_id = "run-terminal"
    _make_terminal_run(session, run_id=run_id)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("terminal debug runs must not reopen the live SSE stream")

    monkeypatch.setattr(main.graph_api, "get_run_events", fail_if_called)

    async def run():
        return await main.get_debug_run_events(run_id=run_id, mode="live", after_seq=0, session=session)

    window, controls = asyncio.run(run())

    assert session["debug_panel"]["mode"] == "history"
    assert session["debug_panel"]["paused"] is False
    window_html = repr(window)
    controls_html = repr(controls)
    assert "live unavailable: terminal" not in window_html
    assert "sse_connect=" not in window_html
    assert "stream-transport-shim" not in window_html
    assert "debug-panel-controls" in controls_html
    assert "Mode: History" in controls_html


def test_terminal_poll_disables_future_polling(monkeypatch):
    session = Session(token="token-2")
    run_id = "run-terminal-poll"
    _make_terminal_run(session, run_id=run_id)

    calls = {"poll": 0, "run": 0}

    async def fake_get_run_events(*_args, **_kwargs):
        calls["poll"] += 1
        return []

    async def fake_get_run(*_args, **_kwargs):
        calls["run"] += 1
        return {"terminal": True, "status": "completed"}

    monkeypatch.setattr(main.graph_api, "get_run_events", fake_get_run_events)
    monkeypatch.setattr(main.graph_api, "get_run", fake_get_run)

    async def run():
        return await main.get_run_poll(run_id, after_seq=0, session=session)

    first = asyncio.run(run())
    second = asyncio.run(run())

    assistant, *oob_logs = first
    assistant_2, *oob_logs_2 = second

    assert session["run_state"][run_id]["terminal"] is True
    assert session["run_state"][run_id]["stage"] == "completed"
    assert calls == {"poll": 0, "run": 0}
    assert assistant.attrs["class"] == "assistant-transport-row"
    assert "STATIC | done" in repr(assistant)
    assert len(oob_logs) == 1
    assert "final answer" in repr(oob_logs[0])
    assert assistant_2.attrs["class"] == "assistant-transport-row"
    assert "STATIC | done" in repr(assistant_2)
    assert len(oob_logs_2) == 1
    assert "final answer" in repr(oob_logs_2[0])


def test_debug_stream_falls_back_for_terminal_runs(monkeypatch):
    session = Session(token="token-3")
    run_id = "run-terminal-stream"
    _make_terminal_run(session, run_id=run_id)

    calls = {"stream": 0}

    async def fail_if_called(*_args, **_kwargs):
        calls["stream"] += 1
        raise AssertionError("terminal debug runs must not open the live SSE stream")

    monkeypatch.setattr(main.graph_api, "stream_events", fail_if_called)

    async def run():
        return await main.get_debug_run_events_stream(run_id=run_id, session=session, after_seq=0)

    first = asyncio.run(run())
    second = asyncio.run(run())

    assert session["debug_panel"]["mode"] == "history"
    assert session["debug_panel"]["paused"] is False
    assert calls["stream"] == 0
    assert isinstance(first, EventSourceResponse)
    assert isinstance(second, EventSourceResponse)
    first_event = asyncio.run(_first_sse_event(first))
    second_event = asyncio.run(_first_sse_event(second))
    assert "source: terminal" in first_event["data"]
    assert "debug-panel-controls" in first_event["data"]
    assert "stream-transport-shim" not in first_event["data"]
    assert "source: terminal" in second_event["data"]


def test_untracked_terminal_run_live_click_hydrates_once(monkeypatch):
    session = Session(token="token-4")
    run_id = "historic-terminal-run"
    calls = {"poll": 0, "run": 0}

    async def fake_get_run_events(*_args, **_kwargs):
        calls["poll"] += 1
        return []

    async def fake_get_run(*_args, **_kwargs):
        calls["run"] += 1
        return {"terminal": True, "status": "completed"}

    monkeypatch.setattr(main.graph_api, "get_run_events", fake_get_run_events)
    monkeypatch.setattr(main.graph_api, "get_run", fake_get_run)

    async def run():
        return await main.get_debug_run_events(run_id=run_id, mode="live", after_seq=0, session=session)

    window, controls = asyncio.run(run())

    assert calls == {"poll": 1, "run": 1}
    assert session["run_state"][run_id]["terminal"] is True
    assert session["debug_panel"]["mode"] == "history"
    assert "sse_connect" not in repr(window)
    assert "completed" in repr(controls)


def test_page_load_hydrates_saved_panel_run_once(monkeypatch):
    session = Session(
        token="token-5",
        user_id="user-1",
        debug_panel={"run_id": "historic-page-run", "mode": "history", "paused": False},
    )
    run_id = "historic-page-run"
    conv_id = "conv-5"
    calls = {"poll": 0, "run": 0}

    async def fake_list_conversations(*_args, **_kwargs):
        return [{"id": conv_id, "turn_count": 1}]

    async def fake_get_transcript(*_args, **_kwargs):
        return []

    async def fake_get_run_events(*_args, **_kwargs):
        calls["poll"] += 1
        return []

    async def fake_get_run(*_args, **_kwargs):
        calls["run"] += 1
        return {"terminal": True, "status": "completed"}

    monkeypatch.setattr(main.graph_api, "list_conversations", fake_list_conversations)
    monkeypatch.setattr(main.graph_api, "get_transcript", fake_get_transcript)
    monkeypatch.setattr(main.graph_api, "get_run_events", fake_get_run_events)
    monkeypatch.setattr(main.graph_api, "get_run", fake_get_run)

    async def run():
        return await main.get_conv(conv_id, session=session, request=SimpleNamespace(headers={}))

    response = asyncio.run(run())

    assert calls == {"poll": 1, "run": 1}
    assert session["run_state"][run_id]["terminal"] is True
    assert "completed" in repr(response)


def test_terminal_run_forces_saved_live_panel_back_to_history(monkeypatch):
    session = Session(
        token="token-6",
        user_id="user-1",
        debug_panel={"run_id": "historic-live-panel", "mode": "live", "paused": False},
    )
    run_id = "historic-live-panel"
    conv_id = "conv-6"

    async def fake_list_conversations(*_args, **_kwargs):
        return [{"id": conv_id, "turn_count": 1}]

    async def fake_get_transcript(*_args, **_kwargs):
        return []

    async def fake_get_run_events(*_args, **_kwargs):
        return []

    async def fake_get_run(*_args, **_kwargs):
        return {"terminal": True, "status": "completed"}

    monkeypatch.setattr(main.graph_api, "list_conversations", fake_list_conversations)
    monkeypatch.setattr(main.graph_api, "get_transcript", fake_get_transcript)
    monkeypatch.setattr(main.graph_api, "get_run_events", fake_get_run_events)
    monkeypatch.setattr(main.graph_api, "get_run", fake_get_run)

    async def run():
        return await main.get_conv(conv_id, session=session, request=SimpleNamespace(headers={}))

    response = asyncio.run(run())

    assert session["debug_panel"]["mode"] == "history"
    assert session["run_state"][run_id]["terminal"] is True
    assert "stream-transport-shim" not in repr(response)


def test_get_conv_returns_four_panels(monkeypatch):
    session = Session(
        token="token-8",
        user_id="user-1",
    )
    conv_id = "conv-8"

    async def fake_list_conversations(*_args, **_kwargs):
        return [{"id": conv_id, "turn_count": 1}]

    async def fake_get_transcript(*_args, **_kwargs):
        return []

    monkeypatch.setattr(main.graph_api, "list_conversations", fake_list_conversations)
    monkeypatch.setattr(main.graph_api, "get_transcript", fake_get_transcript)

    async def run():
        return await main.get_conv(conv_id, session=session, request=SimpleNamespace(headers={"hx-request": "true"}))

    response = asyncio.run(run())

    assert len(response) == 4
    assert "script-queue-list" in repr(response[3])
    assert "script-queue-approve-btn" in repr(response[3])


def test_debug_live_mode_backend_terminal_forces_history(monkeypatch):
    session = Session(
        token="token-7",
        user_id="user-1",
        debug_panel={"run_id": "backend-terminal-run", "mode": "live", "paused": False},
    )
    run_id = "backend-terminal-run"
    calls = {"run": 0}

    async def fake_get_run(*_args, **_kwargs):
        calls["run"] += 1
        return {"terminal": True, "status": "completed"}

    monkeypatch.setattr(main.graph_api, "get_run", fake_get_run)

    async def run():
        return await main.get_debug_run_events_stream(run_id=run_id, session=session, after_seq=0)

    response = asyncio.run(run())
    first_event = asyncio.run(_first_sse_event(response))

    assert calls["run"] == 1
    assert session["run_state"][run_id]["terminal"] is True
    assert session["debug_panel"]["mode"] == "history"
    assert isinstance(response, EventSourceResponse)
    assert "stream-transport-shim" not in first_event["data"]
    assert "source: terminal" in first_event["data"]
    assert "completed" in first_event["data"]


def test_debug_live_click_renders_stream_shim(monkeypatch):
    session = Session(token="token-live-click")
    run_id = "run-live-click"
    main._init_run_state(session, run_id=run_id, conv_id="conv-live", user_text="hello")

    async def run():
        return await main.get_debug_run_events(run_id=run_id, mode="live", after_seq=0, session=session)

    window, controls = asyncio.run(run())

    assert session["debug_panel"]["mode"] == "live"
    assert "stream-transport-shim" in repr(window)
    assert "debug-stream-shim" in repr(window)
    assert f"/debug/run-events/{run_id}/stream?after_seq=0" in repr(window)
    assert "Mode: Live SSE" in repr(controls)


def test_assistant_sse_relay_yields_multiple_frames_over_time(monkeypatch):
    session = Session(token="token-assistant-stream")
    run_id = "run-assistant-stream"
    main._init_run_state(session, run_id=run_id, conv_id="conv-assistant", user_text="hello")

    async def fake_stream_events(*_args, **_kwargs):
        async for item in _fake_streamed_run_events(
            [
                {"seq": 1, "event_type": "run.stage", "stage": "prepare"},
                {"seq": 2, "event_type": "output.delta", "delta": "Hello"},
                {"seq": 3, "event_type": "run.completed"},
            ],
            delay_s=0.03,
        ):
            yield item

    monkeypatch.setattr(main.graph_api, "stream_events", fake_stream_events)

    async def run():
        response = await main.get_events(run_id=run_id, session=session)
        assert isinstance(response, EventSourceResponse)
        return await _collect_timed_sse_events(response, limit=3)

    items = asyncio.run(run())

    assert len(items) == 3
    assert f"evt-{run_id}-1" in items[0][1]["data"]
    assert session["run_state"][run_id]["text"] == "Hello"
    assert f"evt-{run_id}-3" in items[2][1]["data"]
    assert session["run_state"][run_id]["terminal"] is True
    assert items[1][0] - items[0][0] >= 0.02
    assert items[2][0] - items[1][0] >= 0.02


def test_assistant_sse_relay_advances_state_on_first_event(monkeypatch):
    session = Session(token="token-assistant-first-event")
    run_id = "run-assistant-first-event"
    main._init_run_state(session, run_id=run_id, conv_id="conv-assistant", user_text="hello")
    main._save_panel_state(session, {"run_id": run_id, "mode": "live", "paused": False})

    async def fake_stream_events(*_args, **_kwargs):
        async for item in _fake_streamed_run_events(
            [{"seq": 1, "event_type": "run.stage", "stage": "prepare"}],
            delay_s=0.0,
        ):
            yield item

    monkeypatch.setattr(main.graph_api, "stream_events", fake_stream_events)

    async def run():
        response = await main.get_events(run_id=run_id, session=session)
        assert isinstance(response, EventSourceResponse)
        return await _first_sse_event(response)

    first = asyncio.run(run())

    assert session["run_state"][run_id]["last_seq"] == 1
    assert f"evt-{run_id}-1" in first["data"]


def test_upstream_failure_event_surfaces_nested_error_message():
    session = Session(token="token-failure-event")
    main._init_run_state(
        session,
        run_id="run-failure-event",
        conv_id="conv-failure",
        user_text="hello",
    )
    state = main._load_run_state(session, "run-failure-event")

    _seq, event_type, _payload, label, detail = main._ingest_run_event(
        state,
        {
            "seq": 1,
            "event_type": "run.failed",
            "error": {"message": "workflow worker stopped"},
        },
    )

    assert event_type == "run.failed"
    assert label == "Failed"
    assert detail == "workflow worker stopped"
    assert state["terminal"] is True
    assert state["error"] == "workflow worker stopped"


def test_debug_live_stream_yields_multiple_frames_over_time(monkeypatch):
    session = Session(token="token-debug-stream")
    run_id = "run-debug-stream"
    main._init_run_state(session, run_id=run_id, conv_id="conv-debug", user_text="hello")

    async def fake_get_run(*_args, **_kwargs):
        return {"terminal": False, "status": "running"}

    async def fake_stream_events(*_args, **_kwargs):
        async for item in _fake_streamed_run_events(
            [
                {"seq": 1, "event_type": "run.stage", "stage": "prepare"},
                {"seq": 2, "event_type": "reasoning.summary", "summary": "Preparing the answer run."},
                {"seq": 3, "event_type": "run.completed"},
            ],
            delay_s=0.03,
        ):
            yield item

    monkeypatch.setattr(main.graph_api, "get_run", fake_get_run)
    monkeypatch.setattr(main.graph_api, "stream_events", fake_stream_events)

    async def run():
        response = await main.get_debug_run_events_stream(run_id=run_id, session=session, after_seq=0)
        assert isinstance(response, EventSourceResponse)
        return await _collect_timed_sse_events(response, limit=3)

    items = asyncio.run(run())

    assert len(items) == 3
    assert f"evt-{run_id}-1" in items[0][1]["data"]
    assert f"evt-{run_id}-2" in items[1][1]["data"]
    assert f"evt-{run_id}-3" in items[2][1]["data"]
    assert items[1][0] - items[0][0] >= 0.02
    assert items[2][0] - items[1][0] >= 0.02
    assert session["debug_panel"]["mode"] == "history"


def test_sse_contract_runtime_guard_rejects_wrong_return_type():
    @main.sse_route_contract
    async def bad_stream() -> EventSourceResponse:
        return {"not": "an EventSourceResponse"}  # type: ignore[return-value]

    with pytest.raises(TypeError, match="must return EventSourceResponse"):
        asyncio.run(bad_stream())


def test_sse_contract_validator_rejects_missing_annotation():
    async def _empty_stream():
        if False:
            yield "never"

    async def bad_stream():
        return EventSourceResponse(_empty_stream())

    with pytest.raises(RuntimeError, match="annotated to return EventSourceResponse"):
        sse_contracts.validate_sse_route_contracts([("bad_stream", bad_stream)])


def test_mypy_checks_real_sse_routes_from_main():
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    source = main_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(main_path))
    selected = []
    referenced_names: set[str] = set()
    builtin_names = set(dir(__import__("builtins")))

    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not any(
            isinstance(dec, ast.Name) and dec.id == "sse_route_contract"
            for dec in node.decorator_list
        ):
            continue
        selected.append(copy.deepcopy(node))
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                referenced_names.add(sub.id)

    assert selected, "expected at least one SSE route decorated with sse_route_contract"

    helper_names = sorted(
        name
        for name in referenced_names
        if name not in builtin_names
        and name not in {"EventSourceResponse", "Any", "bool", "dict", "list", "str", "int", "float", "set", "tuple", "None"}
    )

    probe = Path.cwd() / "_mypy_sse_contract_probe.py"
    probe_source = "\n".join(
        [
            "from __future__ import annotations",
            "",
            "from typing import Any",
            "from sse_starlette.sse import EventSourceResponse",
            "",
            *[f"{name}: Any" for name in helper_names],
            "",
            *[
                ast.unparse(
                    ast.fix_missing_locations(
                        ast.AsyncFunctionDef(
                            name=node.name,
                            args=node.args,
                            body=node.body,
                            decorator_list=[],
                            returns=node.returns,
                            type_comment=node.type_comment,
                        )
                    )
                )
                for node in selected
            ],
            "",
        ]
    )
    probe.write_text(probe_source, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", str(probe)],
            capture_output=True,
            text=True,
        )
    finally:
        with suppress(FileNotFoundError):
            probe.unlink()
    if proc.returncode != 0 and "No module named mypy" in (proc.stderr or proc.stdout):
        pytest.skip("mypy is not installed in this environment")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_pyodide_worker_timeout_reboots_and_marks_failure():
    project_root = Path(__file__).resolve().parent.parent
    script = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('static/app.js', 'utf8');
const workerInstances = [];
const resultBox = {
  id: 'exec-result-run-1',
  style: { display: 'none' },
  className: 'py-res-box',
  innerHTML: '',
  previousElementSibling: null,
};
const button = {
  innerText: 'Run in Browser',
  disabled: false,
  classList: {
    contains(name) {
      return name === 'run-py-btn';
    },
  },
  previousElementSibling: {
    innerText: 'print("hello")',
  },
};
resultBox.previousElementSibling = button;

function Worker(url) {
  this.url = url;
  this.messages = [];
  this.terminated = false;
  this.postMessage = (msg) => this.messages.push(msg);
  this.terminate = () => {
    this.terminated = true;
  };
  workerInstances.push(this);
}

const sandbox = {
  console,
  Worker,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (fn) => fn(),
  CustomEvent: function CustomEvent(type, init) {
    this.type = type;
    this.detail = init?.detail;
  },
  document: {
    getElementById(id) {
      if (id === 'py-worker-timeout-ms') {
        return { value: '500' };
      }
      if (id === 'exec-result-run-1') {
        return resultBox;
      }
      return null;
    },
    addEventListener() {},
    dispatchEvent() {},
    querySelectorAll() { return []; },
  },
  window: null,
};
sandbox.window = sandbox;

vm.runInNewContext(source, sandbox, { filename: 'static/app.js' });

if (typeof sandbox.sendCodeToWorker !== 'function') {
  throw new Error('sendCodeToWorker was not exposed');
}

const started = sandbox.sendCodeToWorker('run-1', 'while True:\n    pass', button, 'exec-result-run-1');
if (!started) {
  throw new Error('sendCodeToWorker refused to start the job');
}

setTimeout(() => {
  try {
    if (workerInstances.length < 2) {
      throw new Error(`expected worker reboot, saw ${workerInstances.length} worker instance(s)`);
    }
    if (!workerInstances[0].terminated) {
      throw new Error('expected the original worker to be terminated on timeout');
    }
    if (!/timed out after 500 ms/.test(resultBox.innerHTML)) {
      throw new Error(`expected timeout error in result box, got: ${resultBox.innerHTML}`);
    }
    if (button.disabled) {
      throw new Error('expected inline run button to be re-enabled after timeout');
    }
    if (button.innerText !== 'Run in Browser') {
      throw new Error(`expected button label to reset, got: ${button.innerText}`);
    }
    process.stdout.write(JSON.stringify({
      workers: workerInstances.length,
      terminated: workerInstances[0].terminated,
      result: resultBox.innerHTML,
      buttonLabel: button.innerText,
    }));
  } catch (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
  }
}, 700);
"""

    completed = subprocess.run(
        ["node", "-e", script],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )

    payload = json.loads(completed.stdout.strip())
    assert payload["workers"] >= 2
    assert payload["terminated"] is True
    assert "timed out after 500 ms" in payload["result"]
    assert payload["buttonLabel"] == "Run in Browser"


def test_pyodide_worker_error_path_does_not_block_next_run():
    project_root = Path(__file__).resolve().parent.parent
    script = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('static/app.js', 'utf8');
const workerInstances = [];
const resultBoxes = {
  'exec-result-run-1': {
    id: 'exec-result-run-1',
    style: { display: 'none' },
    className: 'py-res-box',
    innerHTML: '',
    previousElementSibling: null,
  },
  'exec-result-run-2': {
    id: 'exec-result-run-2',
    style: { display: 'none' },
    className: 'py-res-box',
    innerHTML: '',
    previousElementSibling: null,
  },
};
const button = {
  innerText: 'Run in Browser',
  disabled: false,
  classList: {
    contains(name) {
      return name === 'run-py-btn';
    },
  },
  previousElementSibling: {
    innerText: 'print("first")',
  },
};
resultBoxes['exec-result-run-1'].previousElementSibling = button;
resultBoxes['exec-result-run-2'].previousElementSibling = button;

function Worker(url) {
  this.url = url;
  this.messages = [];
  this.terminated = false;
  this.postMessage = (msg) => this.messages.push(msg);
  this.terminate = () => {
    this.terminated = true;
  };
  workerInstances.push(this);
}

const sandbox = {
  console,
  Worker,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (fn) => fn(),
  CustomEvent: function CustomEvent(type, init) {
    this.type = type;
    this.detail = init?.detail;
  },
  document: {
    getElementById(id) {
      if (id === 'py-worker-timeout-ms') {
        return { value: '500' };
      }
      return resultBoxes[id] || null;
    },
    addEventListener() {},
    dispatchEvent() {},
    querySelectorAll() { return []; },
  },
  window: null,
};
sandbox.window = sandbox;

vm.runInNewContext(source, sandbox, { filename: 'static/app.js' });

const started = sandbox.sendCodeToWorker('run-1', 'print("first")', button, 'exec-result-run-1');
if (!started) {
  throw new Error('first run did not start');
}

workerInstances[0].onmessage({
  data: {
    type: 'result',
    id: 'run-1',
    success: false,
    error: 'boom',
  },
});

if (button.disabled) {
  throw new Error('button stayed disabled after worker error');
}
if (button.innerText !== 'Run in Browser') {
  throw new Error(`button label did not reset after worker error: ${button.innerText}`);
}
if (!/boom/.test(resultBoxes['exec-result-run-1'].innerHTML)) {
  throw new Error('expected error output for first run');
}

button.previousElementSibling.innerText = 'print("second")';
const restarted = sandbox.sendCodeToWorker('run-2', 'print("second")', button, 'exec-result-run-2');
if (!restarted) {
  throw new Error('second run was blocked after a failure');
}

const secondWorker = workerInstances[workerInstances.length - 1];
if (!secondWorker.messages.some((msg) => msg.type === 'execute' && msg.id === 'run-2')) {
  throw new Error('second run was not forwarded to the worker');
}

process.stdout.write(JSON.stringify({
  workers: workerInstances.length,
  firstWorkerTerminated: workerInstances[0].terminated,
  firstResult: resultBoxes['exec-result-run-1'].innerHTML,
  secondRunPosted: true,
}));
"""

    completed = subprocess.run(
        ["node", "-e", script],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )

    payload = json.loads(completed.stdout.strip())
    assert payload["workers"] >= 1
    assert payload["firstWorkerTerminated"] is False
    assert "boom" in payload["firstResult"]
    assert payload["secondRunPosted"] is True
