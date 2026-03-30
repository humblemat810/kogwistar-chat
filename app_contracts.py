from __future__ import annotations

"""Shared typing contracts for session state and Graph API payloads.

This module stays intentionally small: it gives the app a few concrete shapes
for the most important dict-like data and a couple of lightweight runtime
guards for boundary validation.
"""

from typing import Any, TypedDict


class ConversationSummary(TypedDict):
    id: str
    turn_count: int


class RunEventRecord(TypedDict):
    seq: int
    event_type: str
    payload: dict[str, Any]
    label: str
    detail: str


class RunState(TypedDict):
    run_id: str
    conversation_id: str
    last_seq: int
    events: list[RunEventRecord]
    text: str
    stage: str
    terminal: bool
    error: str | None
    user_text: str
    mode: str


class DebugPanelState(TypedDict):
    run_id: str
    mode: str
    paused: bool


class DebugRunState(TypedDict):
    paused: bool
    resume_seq: int
    cleared: bool


class SessionData(TypedDict, total=False):
    auth_key: str
    token: str
    username: str
    user_id: str
    conversation_id: str
    conversations: list[ConversationSummary]
    last_run_id: str
    run_state: dict[str, RunState]
    debug_panel: DebugPanelState
    debug_state: dict[str, DebugRunState]


class AuthTokenResponse(TypedDict, total=False):
    token: str
    access_token: str


class UserProfileResponse(TypedDict, total=False):
    user_id: str
    display_name: str
    email: str


class CreateConversationResponse(TypedDict):
    conversation_id: str


class ConversationTurn(TypedDict):
    role: str
    content: str


class ConversationListResponse(TypedDict):
    conversations: list[ConversationSummary]


class TranscriptResponse(TypedDict):
    turns: list[ConversationTurn]


class SubmitTurnResponse(TypedDict):
    run_id: str


class RunResponse(TypedDict, total=False):
    terminal: bool
    status: str


class RunEventsResponse(TypedDict):
    events: list[dict[str, Any]]


class StreamEventPayload(TypedDict, total=False):
    seq: int | str
    id: int | str
    sse_id: int | str
    event: str
    event_type: str
    type: str
    stage: str
    summary: str
    delta: str
    message: str
    status: str
    data: Any
    payload: dict[str, Any]
    run_id: str
    created_at_ms: int
    step_seq: int
    workflow_node_id: str
    assistant_text: str
    assistant_turn_node_id: str


def require_mapping(value: Any, *, context: str) -> dict[str, Any]:
    """Ensure `value` is a plain dict-like JSON object."""

    if not isinstance(value, dict):
        raise TypeError(f"{context} must be a JSON object, got {type(value).__name__}")
    return dict(value)


def require_list(value: Any, *, context: str) -> list[Any]:
    """Ensure `value` is a JSON array."""

    if not isinstance(value, list):
        raise TypeError(f"{context} must be a JSON array, got {type(value).__name__}")
    return list(value)


def require_str(value: Any, *, context: str) -> str:
    """Ensure `value` is a string and normalize it."""

    if not isinstance(value, str):
        raise TypeError(f"{context} must be a string, got {type(value).__name__}")
    return value


def require_bool(value: Any, *, context: str) -> bool:
    """Ensure `value` is a boolean."""

    if not isinstance(value, bool):
        raise TypeError(f"{context} must be a boolean, got {type(value).__name__}")
    return value


def normalize_conversation_summary(value: Any, *, context: str) -> ConversationSummary:
    """Validate and normalize a conversation summary record."""

    payload = require_mapping(value, context=context)
    return {
        "id": require_str(payload.get("id"), context=f"{context}.id"),
        "turn_count": int(payload.get("turn_count") or 0),
    }


def normalize_conversation_turn(value: Any, *, context: str) -> ConversationTurn:
    """Validate and normalize one transcript turn."""

    payload = require_mapping(value, context=context)
    return {
        "role": require_str(payload.get("role"), context=f"{context}.role"),
        "content": require_str(payload.get("content"), context=f"{context}.content"),
    }


def normalize_run_event(value: Any, *, context: str) -> dict[str, Any]:
    """Validate that one run event is a JSON object."""

    return require_mapping(value, context=context)
