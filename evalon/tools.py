"""Tool instrumentation helpers."""

from __future__ import annotations

import functools
import inspect
from time import perf_counter
from typing import Any, Callable

from evalon.json import sanitize
from evalon.trace import current_trace


def _call_payload(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
    except Exception:
        arguments = {"args": args, "kwargs": kwargs}
    return sanitize(arguments)


def instrument_tool(func: Callable[..., Any], *, name: str | None = None) -> Callable[..., Any]:
    tool_name = name or func.__name__

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            trace = current_trace()
            started = perf_counter()
            if trace is None:
                return await func(*args, **kwargs)
            arguments = _call_payload(func, args, kwargs)
            with trace.span(tool_name, kind="tool", metadata={"name": tool_name}) as span:
                span.record_input(arguments)
                trace.increment_metric("tool_calls")
                trace.record_event("tool.call", {"name": tool_name, "arguments": arguments})
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    span.record_error(exc)
                    trace.record_error(exc, event_type="tool.error")
                    trace.record_event(
                        "tool.output",
                        {
                            "name": tool_name,
                            "latency_ms": round((perf_counter() - started) * 1000, 3),
                            "error": str(exc),
                        },
                    )
                    raise
                span.record_output(result)
                trace.record_event(
                    "tool.output",
                    {
                        "name": tool_name,
                        "output": result,
                        "latency_ms": round((perf_counter() - started) * 1000, 3),
                    },
                )
                return result

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        trace = current_trace()
        started = perf_counter()
        if trace is None:
            return func(*args, **kwargs)
        arguments = _call_payload(func, args, kwargs)
        with trace.span(tool_name, kind="tool", metadata={"name": tool_name}) as span:
            span.record_input(arguments)
            trace.increment_metric("tool_calls")
            trace.record_event("tool.call", {"name": tool_name, "arguments": arguments})
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                span.record_error(exc)
                trace.record_error(exc, event_type="tool.error")
                trace.record_event(
                    "tool.output",
                    {
                        "name": tool_name,
                        "latency_ms": round((perf_counter() - started) * 1000, 3),
                        "error": str(exc),
                    },
                )
                raise
            span.record_output(result)
            trace.record_event(
                "tool.output",
                {
                    "name": tool_name,
                    "output": result,
                    "latency_ms": round((perf_counter() - started) * 1000, 3),
                },
            )
            return result

    return sync_wrapper
