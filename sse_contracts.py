from __future__ import annotations

"""Small contract helpers for SSE-only route handlers.

Keep this module free of FastHTML imports so MyPy can analyze it in isolation.
The app imports these helpers and applies them only to routes that must
always return `EventSourceResponse`.
"""

import asyncio
import inspect
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, get_args, get_origin, get_type_hints

from sse_starlette.sse import EventSourceResponse

P = ParamSpec("P")
SSEHandler = Callable[P, Awaitable[EventSourceResponse]]

_SSE_ROUTE_CONTRACTS: list[tuple[str, Callable[..., Any]]] = []


def _is_eventsource_annotation(anno: Any) -> bool:
    """Return True when `anno` is or contains `EventSourceResponse`."""

    if anno is inspect.Signature.empty:
        return False
    if anno is EventSourceResponse:
        return True
    origin = get_origin(anno)
    if origin is None:
        return False
    return any(_is_eventsource_annotation(arg) for arg in get_args(anno))


def sse_route_contract(fn: SSEHandler[P]) -> SSEHandler[P]:
    """Mark an endpoint as SSE-only and assert its runtime return type."""

    if not asyncio.iscoroutinefunction(fn):
        raise TypeError(f"SSE route {fn.__qualname__} must be async def")

    _SSE_ROUTE_CONTRACTS.append((fn.__qualname__, fn))

    @wraps(fn)
    async def _wrapped(*args: P.args, **kwargs: P.kwargs) -> EventSourceResponse:
        result = await fn(*args, **kwargs)
        if not isinstance(result, EventSourceResponse):
            raise TypeError(
                f"SSE route {fn.__qualname__} must return EventSourceResponse, got {type(result).__name__}"
            )
        return result

    _wrapped.__sse_route_contract__ = True  # type: ignore[attr-defined]
    _wrapped.__sse_route_contract_name__ = fn.__qualname__  # type: ignore[attr-defined]
    return _wrapped


def validate_sse_route_contracts(contracts: list[tuple[str, Callable[..., Any]]] | None = None) -> None:
    """Fail fast if a marked SSE route is not annotated as SSE-only."""

    for qualname, fn in contracts or _SSE_ROUTE_CONTRACTS:
        try:
            hints = get_type_hints(fn, globalns=globals(), localns=globals())
        except Exception:
            hints = {}
        return_anno = hints.get("return", inspect.signature(fn).return_annotation)
        if not _is_eventsource_annotation(return_anno):
            raise RuntimeError(
                f"SSE route {qualname} must be annotated to return EventSourceResponse, got {return_anno!r}"
            )
