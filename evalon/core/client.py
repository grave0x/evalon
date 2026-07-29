"""Evalon client and module-level convenience API."""

from __future__ import annotations

import inspect
from contextlib import nullcontext
from functools import wraps
from pathlib import Path
from typing import Any, ContextManager

from evalon.core.providers import AnthropicWrapper, OpenAIWrapper
from evalon.core.paths import default_db_path
from evalon.storage import JsonlStorage, SqliteStorage
from evalon.core.tools import instrument_tool
from evalon.core.trace import (
    Session,
    SessionContext,
    Span,
    Trace,
    TraceContext,
    current_session as _current_session,
    current_span as _current_span,
    current_trace as _current_trace,
)


_client: "EvalonClient | None" = None

Storage = JsonlStorage | SqliteStorage


def _create_storage(output: Path) -> Storage:
    """Create the appropriate local storage backend based on file extension."""
    suffix = output.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        return JsonlStorage(output)
    return SqliteStorage(output)


class EvalonClient:
    def __init__(
        self,
        *,
        project: str,
        output: str | Path | None = None,
        environment: str | None = None,
        metadata: dict[str, Any] | None = None,
        storage: Storage | None = None,
    ) -> None:
        self.project = project
        self.output = Path(output).expanduser() if output is not None else default_db_path()
        self.environment = environment
        self.metadata = metadata or {}
        self.storage = storage or _create_storage(self.output)

    def trace(
        self,
        name: str,
        *,
        input: Any = None,
        expected: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceContext:
        trace_metadata = dict(self.metadata)
        if metadata:
            trace_metadata.update(metadata)
        trace = Trace(
            project=self.project,
            name=name,
            input=input,
            expected=expected,
            metadata=trace_metadata,
            environment=self.environment,
            storage=self.storage,
        )
        return TraceContext(trace)

    def session(
        self,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SessionContext:
        session_metadata = dict(self.metadata)
        if metadata:
            session_metadata.update(metadata)
        session = Session(
            project=self.project,
            name=name,
            metadata=session_metadata,
            storage=self.storage,
        )
        return SessionContext(session)

    def tool(self, func: Any | None = None, *, name: str | None = None) -> Any:
        def decorate(inner: Any) -> Any:
            return instrument_tool(inner, name=name)

        if func is None:
            return decorate
        return decorate(func)

    def openai(self, client: Any | None = None, *, async_client: bool = False, **kwargs: Any) -> OpenAIWrapper:
        if client is None:
            try:
                from openai import AsyncOpenAI, OpenAI
            except ImportError as exc:
                raise ImportError("Install the `openai` package or pass an OpenAI client instance.") from exc
            client = AsyncOpenAI(**kwargs) if async_client else OpenAI(**kwargs)
        return OpenAIWrapper(client, provider="openai")

    def openrouter(self, client: Any | None = None, *, async_client: bool = False, **kwargs: Any) -> OpenAIWrapper:
        if client is None:
            try:
                from openai import AsyncOpenAI, OpenAI
            except ImportError as exc:
                raise ImportError("Install the `openai` package or pass an OpenAI-compatible client.") from exc
            kwargs.setdefault("base_url", "https://openrouter.ai/api/v1")
            client = AsyncOpenAI(**kwargs) if async_client else OpenAI(**kwargs)
        return OpenAIWrapper(client, provider="openrouter")

    def anthropic(self, client: Any | None = None, *, async_client: bool = False, **kwargs: Any) -> AnthropicWrapper:
        if client is None:
            try:
                from anthropic import Anthropic, AsyncAnthropic
            except ImportError as exc:
                raise ImportError("Install the `anthropic` package or pass an Anthropic client instance.") from exc
            client = AsyncAnthropic(**kwargs) if async_client else Anthropic(**kwargs)
        return AnthropicWrapper(client)


def init(
    project: str,
    *,
    output: str | Path | None = None,
    environment: str | None = None,
    metadata: dict[str, Any] | None = None,
    storage: Storage | None = None,
) -> EvalonClient:
    global _client
    _client = EvalonClient(
        project=project,
        output=output,
        environment=environment,
        metadata=metadata,
        storage=storage,
    )
    return _client


def get_client() -> EvalonClient:
    if _client is None:
        raise RuntimeError("Evalon is not initialized. Call evalon.init(project=...) first.")
    return _client


def trace(
    name: str,
    *,
    input: Any = None,
    expected: Any = None,
    metadata: dict[str, Any] | None = None,
) -> TraceContext:
    return get_client().trace(name, input=input, expected=expected, metadata=metadata)


def session(
    name: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> SessionContext:
    return get_client().session(name, metadata=metadata)


def tool(func: Any | None = None, *, name: str | None = None) -> Any:
    if _client is not None:
        return get_client().tool(func, name=name)

    def decorate(inner: Any) -> Any:
        return instrument_tool(inner, name=name)

    if func is None:
        return decorate
    return decorate(func)


def _observed_input(func: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Return a stable, named input payload without capturing bound instances."""
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
        values = dict(bound.arguments)
    except (TypeError, ValueError):
        values = {
            "args": list(args),
            "kwargs": kwargs,
        }
    values.pop("self", None)
    values.pop("cls", None)
    return values or None


def observe(func: Any | None = None, *, name: str | None = None) -> Any:
    """Trace a function call, or add a span when called inside an active trace."""

    def decorate(inner: Any) -> Any:
        observed_name = name or inner.__name__

        if inspect.iscoroutinefunction(inner):

            @wraps(inner)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                input_payload = _observed_input(inner, args, kwargs)
                active = current_trace()
                if active is None:
                    async with get_client().trace(observed_name, input=input_payload) as root:
                        result = await inner(*args, **kwargs)
                        root.record_output(result)
                        return result

                async with active.span(
                    observed_name,
                    kind="custom",
                    metadata={"evalon.observe": True},
                ) as observed_span:
                    observed_span.record_input(input_payload)
                    try:
                        result = await inner(*args, **kwargs)
                    except Exception as exc:
                        active.record_error(exc, event_type="observe.error")
                        raise
                    observed_span.record_output(result)
                    return result

            return async_wrapper

        @wraps(inner)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            input_payload = _observed_input(inner, args, kwargs)
            active = current_trace()
            if active is None:
                with get_client().trace(observed_name, input=input_payload) as root:
                    result = inner(*args, **kwargs)
                    root.record_output(result)
                    return result

            with active.span(
                observed_name,
                kind="custom",
                metadata={"evalon.observe": True},
            ) as observed_span:
                observed_span.record_input(input_payload)
                try:
                    result = inner(*args, **kwargs)
                except Exception as exc:
                    active.record_error(exc, event_type="observe.error")
                    raise
                observed_span.record_output(result)
                return result

        return sync_wrapper

    if func is None:
        return decorate
    return decorate(func)


def openai(client: Any | None = None, *, async_client: bool = False, **kwargs: Any) -> OpenAIWrapper:
    return get_client().openai(client, async_client=async_client, **kwargs)


def openrouter(client: Any | None = None, *, async_client: bool = False, **kwargs: Any) -> OpenAIWrapper:
    return get_client().openrouter(client, async_client=async_client, **kwargs)


def anthropic(client: Any | None = None, *, async_client: bool = False, **kwargs: Any) -> AnthropicWrapper:
    return get_client().anthropic(client, async_client=async_client, **kwargs)


def current_trace() -> Trace | None:
    return _current_trace()


def current_span() -> Span | None:
    return _current_span()


def current_session() -> Session | None:
    return _current_session()


def span(
    name: str,
    *,
    kind: str = "custom",
    metadata: dict[str, Any] | None = None,
) -> ContextManager[Span | None]:
    active = current_trace()
    if active is None:
        return nullcontext(None)
    return active.span(name, kind=kind, metadata=metadata)


def record_event(event_type: str, **payload: Any) -> None:
    active = current_trace()
    if active is not None:
        active.record_event(event_type, payload)


def record_metric(name: str, value: int | float) -> None:
    active = current_trace()
    if active is not None:
        active.record_metric(name, value)


def record_output(output: Any) -> None:
    active = current_trace()
    if active is not None:
        active.record_output(output)
