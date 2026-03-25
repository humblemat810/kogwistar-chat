from __future__ import annotations

import ast
import asyncio
import copy
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


def _make_terminal_run(session: Session, run_id: str = "run-1") -> None:
    main._init_run_state(session, run_id=run_id, conv_id="conv-1", user_text="hello")
    state = main._load_run_state(session, run_id)
    state["terminal"] = True
    state["stage"] = "completed"
    state["text"] = "final answer"
    main._save_run_state(session, state)


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
