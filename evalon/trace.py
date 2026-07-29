"""Trace lifecycle and event recording."""

from __future__ import annotations

import contextvars
import traceback
import uuid
from dataclasses import dataclass, field
from time import perf_counter
from types import TracebackType
from typing import Any

from evalon.json import sanitize, utc_now_iso


_active_trace: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar(
    "evalon_active_trace",
    default=None,
)
_active_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "evalon_active_span",
    default=None,
)
_active_session: contextvars.ContextVar["Session | None"] = contextvars.ContextVar(
    "evalon_active_session",
    default=None,
)


def current_trace() -> "Trace | None":
    return _active_trace.get()


def current_span() -> "Span | None":
    return _active_span.get()


def current_session() -> "Session | None":
    return _active_session.get()


def _storage_call(storage: Any, method: str, *args: Any, **kwargs: Any) -> None:
    """Call an optional storage method if present."""
    if storage is None:
        return
    fn = getattr(storage, method, None)
    if fn is None:
        return
    fn(*args, **kwargs)


def _exception_payload(exc: BaseException) -> dict[str, Any]:
    return sanitize(
        {
            "type": exc.__class__.__name__,
            "message": str(exc),
            "traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
        }
    )


@dataclass
class Span:
    trace: "Trace"
    name: str
    kind: str = "custom"
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_span_id: str | None = None
    id: str = field(default_factory=lambda: f"span_{uuid.uuid4().hex}")
    status: str = "running"
    input: Any = None
    output: Any = None
    error: dict[str, Any] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    latency_ms: float | None = None
    _start_time: float | None = None
    _token: contextvars.Token["Span | None"] | None = field(default=None, init=False, repr=False)
    _flushed_start: bool = field(default=False, init=False, repr=False)

    @property
    def trace_id(self) -> str:
        return self.trace.id

    def start(self) -> None:
        self.started_at = utc_now_iso()
        self._start_time = perf_counter()
        self._flush_span()

    def finish(self, *, status: str = "success", exc: BaseException | None = None) -> None:
        self.status = status
        self.ended_at = utc_now_iso()
        if self._start_time is not None:
            self.latency_ms = round((perf_counter() - self._start_time) * 1000, 3)
        if exc is not None:
            self.error = _exception_payload(exc)
        self._flush_span()

    def record_input(self, value: Any) -> None:
        self.input = sanitize(value)
        self._flush_span()

    def record_output(self, value: Any) -> None:
        self.output = sanitize(value)
        self._flush_span()

    def record_error(self, exc: BaseException) -> None:
        self.status = "error"
        self.error = _exception_payload(exc)
        self._flush_span()

    def _flush_span(self) -> None:
        storage = self.trace.storage
        if storage is None:
            return
        # Ensure parent trace row exists before span FK insert
        if not self.trace._header_flushed:
            self.trace._flush_header()
        _storage_call(storage, "upsert_span", self.to_dict())

    def __enter__(self) -> "Span":
        self.start()
        self._token = _active_span.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        del exc_type, tb
        self.finish(status="error" if exc is not None else "success", exc=exc)
        if self._token is not None:
            _active_span.reset(self._token)
        return False

    async def __aenter__(self) -> "Span":
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)

    def to_dict(self) -> dict[str, Any]:
        return sanitize(
            {
                "id": self.id,
                "trace_id": self.trace_id,
                "parent_span_id": self.parent_span_id,
                "name": self.name,
                "kind": self.kind,
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "latency_ms": self.latency_ms,
                "metadata": self.metadata,
                "input": self.input,
                "output": self.output,
                "error": self.error,
            }
        )


@dataclass
class Trace:
    project: str
    name: str
    input: Any = None
    expected: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    environment: str | None = None
    storage: Any = None
    session_id: str | None = None
    id: str = field(default_factory=lambda: f"trace_{uuid.uuid4().hex}")
    status: str = "running"
    output: Any = None
    spans: list[Span] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int | float] = field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    _start_time: float | None = None
    _flushed: bool = False
    _header_flushed: bool = False
    _recorded_exception_ids: set[int] = field(default_factory=set)
    # Events already appended live; finish rewrites full set via write()
    _live_event_count: int = 0

    def start(self) -> None:
        self.started_at = utc_now_iso()
        self._start_time = perf_counter()
        self.status = "running"
        self._flush_header()
        self.record_event("trace.start", {"name": self.name, "input": self.input})

    def _header_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "environment": self.environment,
            "name": self.name,
            "input": self.input,
            "output": self.output,
            "expected": self.expected,
            "metadata": self.metadata,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
        }

    def _flush_header(self) -> None:
        if self.storage is None:
            return
        _storage_call(self.storage, "upsert_trace_header", self._header_dict())
        self._header_flushed = True

    def finish(self, *, status: str = "success") -> None:
        if self._flushed:
            return
        self.status = status
        self.ended_at = utc_now_iso()
        if self._start_time is not None:
            self.metrics["latency_ms"] = round((perf_counter() - self._start_time) * 1000, 3)
        self.metrics.setdefault("llm_calls", 0)
        self.metrics.setdefault("tool_calls", 0)
        self.metrics.setdefault("errors", 0)
        self.metrics.setdefault("input_tokens", 0)
        self.metrics.setdefault("output_tokens", 0)
        self.metrics.setdefault("cost_usd", 0.0)
        self.record_event("trace.end", {"status": self.status})
        if self.storage is not None:
            # Full snapshot: replaces live-appended events/spans for a consistent final row
            finalize = getattr(self.storage, "finalize_trace", None) or getattr(
                self.storage, "write", None
            )
            if finalize is not None:
                finalize(self.to_dict())
        self._flushed = True

    def record_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        event = {
            "type": event_type,
            "timestamp": utc_now_iso(),
            "payload": sanitize(payload or {}),
        }
        span = current_span()
        if span is not None and span.trace_id == self.id:
            event["span_id"] = span.id
        self.events.append(event)
        if self.storage is not None and not self._flushed:
            if not self._header_flushed:
                self._flush_header()
            _storage_call(self.storage, "append_event", self.id, event)
            self._live_event_count += 1

    def span(
        self,
        name: str,
        *,
        kind: str = "custom",
        metadata: dict[str, Any] | None = None,
    ) -> Span:
        parent = current_span()
        parent_span_id = parent.id if parent is not None and parent.trace_id == self.id else None
        span = Span(
            trace=self,
            name=name,
            kind=kind,
            metadata=sanitize(metadata or {}),
            parent_span_id=parent_span_id,
        )
        self.spans.append(span)
        return span

    def record_metric(self, name: str, value: int | float) -> None:
        self.metrics[name] = value
        if self.storage is not None and not self._flushed:
            if not self._header_flushed:
                self._flush_header()
            _storage_call(self.storage, "upsert_metrics", self.id, {name: value})

    def record_output(self, output: Any) -> None:
        self.output = sanitize(output)
        self.record_event("trace.output", {"output": output})
        if not self._flushed:
            self._flush_header()

    def increment_metric(self, name: str, amount: int | float = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount
        if self.storage is not None and not self._flushed:
            if not self._header_flushed:
                self._flush_header()
            _storage_call(self.storage, "upsert_metrics", self.id, {name: self.metrics[name]})

    def record_error(self, exc: BaseException, *, event_type: str = "error") -> bool:
        exception_id = id(exc)
        if exception_id in self._recorded_exception_ids:
            return False
        self._recorded_exception_ids.add(exception_id)
        self.increment_metric("errors")
        self.record_event(
            event_type,
            {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
            },
        )
        return True

    def add_token_usage(self, usage: dict[str, Any]) -> None:
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        self.increment_metric("input_tokens", input_tokens)
        self.increment_metric("output_tokens", output_tokens)
        # Cache token tracking
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache_read = cache_read or details.get("cached_tokens", 0) or 0
        cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
        if cache_read:
            self.increment_metric("cache_read_tokens", cache_read)
        if cache_creation:
            self.increment_metric("cache_creation_tokens", cache_creation)

    def to_dict(self) -> dict[str, Any]:
        return sanitize(
            {
                "id": self.id,
                "project": self.project,
                "environment": self.environment,
                "name": self.name,
                "input": self.input,
                "output": self.output,
                "expected": self.expected,
                "metadata": self.metadata,
                "session_id": self.session_id,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "status": self.status,
                "spans": [span.to_dict() for span in self.spans],
                "events": self.events,
                "metrics": self.metrics,
            }
        )


class TraceContext:
    def __init__(self, trace: Trace) -> None:
        self.trace = trace
        self._token: contextvars.Token[Trace | None] | None = None

    def __enter__(self) -> Trace:
        session = current_session()
        if session is not None and self.trace.session_id is None:
            self.trace.session_id = session.id
            session.traces.append(self.trace)
        self.trace.start()
        self._token = _active_trace.set(self.trace)
        return self.trace

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        try:
            if exc is not None:
                self.trace.record_error(exc)
                try:
                    self.trace.finish(status="error")
                except Exception:
                    return False
            else:
                self.trace.finish(status="success")
        finally:
            active_span = current_span()
            if active_span is not None and active_span.trace_id == self.trace.id:
                _active_span.set(None)
            if self._token is not None:
                _active_trace.reset(self._token)
        return False

    async def __aenter__(self) -> Trace:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)


@dataclass
class Session:
    project: str
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    storage: Any = None
    id: str = field(default_factory=lambda: f"session_{uuid.uuid4().hex}")
    status: str = "running"
    traces: list[Trace] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int | float] = field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    _start_time: float | None = None
    _flushed: bool = False

    def start(self) -> None:
        self.started_at = utc_now_iso()
        self._start_time = perf_counter()
        self.status = "running"
        self.record_event("session.start", {"name": self.name})
        self._flush_header()

    def _header_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "name": self.name,
            "metadata": self.metadata,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    def _flush_header(self) -> None:
        _storage_call(self.storage, "upsert_session", self._header_dict())

    def finish(self, *, status: str = "success") -> None:
        if self._flushed:
            return
        self.status = status
        self.ended_at = utc_now_iso()
        if self._start_time is not None:
            self.metrics["latency_ms"] = round((perf_counter() - self._start_time) * 1000, 3)
        # Roll up child trace cost when available
        total_cost = 0.0
        for t in self.traces:
            cost = t.metrics.get("cost_usd")
            if cost is not None:
                total_cost += float(cost)
        if total_cost:
            self.metrics["cost_usd"] = total_cost
        self.record_event("session.end", {"status": self.status})
        if self.storage is not None:
            finalize = getattr(self.storage, "finalize_session", None) or getattr(
                self.storage, "write_session", None
            )
            if finalize is not None:
                finalize(self.to_dict())
        self._flushed = True

    def record_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append(
            {
                "type": event_type,
                "timestamp": utc_now_iso(),
                "payload": sanitize(payload or {}),
            }
        )

    def record_metric(self, name: str, value: int | float) -> None:
        self.metrics[name] = value

    def increment_metric(self, name: str, amount: int | float = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def to_dict(self) -> dict[str, Any]:
        return sanitize(
            {
                "id": self.id,
                "project": self.project,
                "name": self.name,
                "metadata": self.metadata,
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "traces": [t.to_dict() for t in self.traces],
                "events": self.events,
                "metrics": self.metrics,
            }
        )


class SessionContext:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._token: contextvars.Token[Session | None] | None = None

    def __enter__(self) -> Session:
        self.session.start()
        self._token = _active_session.set(self.session)
        return self.session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        try:
            if exc is not None:
                try:
                    self.session.finish(status="error")
                except Exception:
                    return False
            else:
                self.session.finish(status="success")
        finally:
            if self._token is not None:
                _active_session.reset(self._token)
        return False

    async def __aenter__(self) -> Session:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)
