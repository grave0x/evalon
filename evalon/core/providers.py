"""Provider SDK wrappers."""

from __future__ import annotations

import inspect
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Callable

from evalon.core.json import sanitize
from evalon.core.trace import Span, Trace, current_trace


def _get_attr_or_key(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _extract_usage(response: Any) -> dict[str, Any]:
    usage = _get_attr_or_key(response, "usage")
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    output: dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = getattr(usage, key, None)
        if value is not None:
            output[key] = value
    # OpenAI: extract cache info from nested prompt_tokens_details
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is not None:
        cached = _get_attr_or_key(details, "cached_tokens")
        if cached is not None:
            output["prompt_tokens_details"] = {"cached_tokens": cached}
    return output


def _serialize_tool_calls(tool_calls: Any) -> list[dict[str, Any]] | None:
    if not tool_calls:
        return None
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = _get_attr_or_key(tc, "function")
        entry: dict[str, Any] = {
            "id": _get_attr_or_key(tc, "id"),
            "type": _get_attr_or_key(tc, "type", "function"),
        }
        if fn is not None:
            args = _get_attr_or_key(fn, "arguments")
            entry["function"] = {
                "name": _get_attr_or_key(fn, "name"),
                "arguments": args if isinstance(args, (str, dict, list)) else str(args) if args is not None else None,
            }
        else:
            name = _get_attr_or_key(tc, "name")
            if name is not None:
                entry["name"] = name
        out.append(entry)
    return out or None


def _extract_choice_message(response: Any) -> dict[str, Any] | None:
    """OpenAI-style chat.completions: choices[0].message."""
    choices = _get_attr_or_key(response, "choices")
    if not choices:
        return None
    first = choices[0]
    message = _get_attr_or_key(first, "message")
    if message is None:
        # Some SDKs put text on the choice itself
        text = _get_attr_or_key(first, "text")
        if text is not None:
            return {"content": text}
        return None
    content = _get_attr_or_key(message, "content")
    tool_calls = _serialize_tool_calls(_get_attr_or_key(message, "tool_calls"))
    result: dict[str, Any] = {}
    if content is not None:
        result["content"] = content
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result or None


def _extract_anthropic_content(response: Any) -> str | list[Any] | None:
    """Anthropic messages: content blocks → text or structured list."""
    content = _get_attr_or_key(response, "content")
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
                continue
            btype = _get_attr_or_key(block, "type")
            if btype == "text" or _get_attr_or_key(block, "text") is not None:
                text = _get_attr_or_key(block, "text")
                if text is not None:
                    texts.append(str(text))
        if texts:
            return "\n".join(texts) if len(texts) > 1 else texts[0]
        # Fall back to sanitized block list (tool use, etc.)
        return sanitize(content)
    return sanitize(content)


def _extract_response_text(response: Any) -> str | None:
    """Best-effort assistant text for preview (chat, responses, anthropic)."""
    # OpenAI Responses API
    for key in ("output_text", "text"):
        value = _get_attr_or_key(response, key)
        if isinstance(value, str) and value:
            return value

    choice_msg = _extract_choice_message(response)
    if choice_msg and isinstance(choice_msg.get("content"), str):
        return choice_msg["content"]

    anthropic = _extract_anthropic_content(response)
    if isinstance(anthropic, str):
        return anthropic

    return None


def _extract_finish_reason(response: Any) -> Any:
    reason = _get_attr_or_key(response, "finish_reason")
    if reason is not None:
        return reason
    # OpenAI chat: choices[0].finish_reason
    choices = _get_attr_or_key(response, "choices")
    if choices:
        return _get_attr_or_key(choices[0], "finish_reason")
    # Anthropic
    return _get_attr_or_key(response, "stop_reason")


def _response_payload(response: Any) -> dict[str, Any]:
    """Build a JSON-serializable llm.response payload (never store raw SDK objects)."""
    choice_msg = _extract_choice_message(response)
    content = _extract_response_text(response)
    tool_calls = None
    if choice_msg and choice_msg.get("tool_calls"):
        tool_calls = choice_msg["tool_calls"]
    else:
        tool_calls = _serialize_tool_calls(_get_attr_or_key(response, "tool_calls"))

    payload: dict[str, Any] = {
        "id": _get_attr_or_key(response, "id"),
        "model": _get_attr_or_key(response, "model"),
        "finish_reason": _extract_finish_reason(response),
        "usage": _extract_usage(response),
    }
    if content is not None:
        payload["content"] = content
    if tool_calls:
        payload["tool_calls"] = tool_calls

    # Keep a small structured snapshot under "response" for compatibility,
    # but only plain data — never the raw SDK object (which becomes namespace(...)).
    snapshot: dict[str, Any] = {}
    if content is not None:
        snapshot["content"] = content
    if tool_calls:
        snapshot["tool_calls"] = tool_calls
    if snapshot:
        payload["response"] = snapshot

    return sanitize(payload)


_SAMPLING_PARAMS = frozenset({
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_output_tokens",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "stop",
})


def _request_payload(provider: str, operation: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    params = {k: kwargs[k] for k in _SAMPLING_PARAMS if k in kwargs}
    system = kwargs.get("system")
    messages = kwargs.get("messages")
    if system is None and isinstance(messages, list):
        system_messages = [
            message.get("content")
            for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        if system_messages:
            system = system_messages[0] if len(system_messages) == 1 else system_messages
    return {
        "provider": provider,
        "operation": operation,
        "model": kwargs.get("model"),
        "sampling_params": params or None,
        "input": kwargs.get("input"),
        "instructions": kwargs.get("instructions"),
        "messages": messages,
        "system": system,
        "tools": kwargs.get("tools"),
    }


def _record_request(trace: Trace, payload: dict[str, Any]) -> None:
    trace.increment_metric("llm_calls")
    trace.record_event("llm.request", payload)


def _extract_cache_tokens(usage: dict[str, Any]) -> tuple[int, int]:
    """Extract cache read and creation tokens from usage dict.

    Returns (cache_read_tokens, cache_creation_tokens).
    Handles both OpenAI format (prompt_tokens_details.cached_tokens)
    and Anthropic format (cache_read_input_tokens, cache_creation_input_tokens).
    """
    # Anthropic format: top-level fields
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

    # OpenAI format: nested prompt_tokens_details.cached_tokens
    if not cache_read:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache_read = details.get("cached_tokens", 0) or 0

    return (cache_read, cache_creation)


def _record_response(
    trace: Trace,
    span: Span,
    provider: str,
    operation: str,
    response: Any,
    started: float,
) -> dict[str, Any]:
    usage = _extract_usage(response)
    if usage:
        trace.add_token_usage(usage)
        span.metadata["usage"] = sanitize(usage)

    # Cost calculation
    model = _get_attr_or_key(response, "model")
    if model:
        span.metadata["model"] = model
    if usage and model:
        from evalon.core.cost import calculate_cost

        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        cache_read, cache_creation = _extract_cache_tokens(usage)

        # OpenAI includes cached tokens in prompt_tokens (via prompt_tokens_details).
        # Anthropic keeps cache tokens separate from input_tokens.
        has_openai_cache = "prompt_tokens_details" in usage

        cost = calculate_cost(
            model,
            prompt_tokens,
            completion_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            prompt_tokens_includes_cache=has_openai_cache,
        )
        if cost is not None:
            trace.increment_metric("cost_usd", cost.total_cost)
            trace.record_metric("input_cost_usd", cost.input_cost)
            trace.record_metric("output_cost_usd", cost.output_cost)
            span.metadata["cost_usd"] = cost.total_cost
            span.metadata["input_cost_usd"] = cost.input_cost
            span.metadata["output_cost_usd"] = cost.output_cost
            if cost.cache_read_cost:
                trace.record_metric("cache_read_cost_usd", cost.cache_read_cost)
                span.metadata["cache_read_cost_usd"] = cost.cache_read_cost
            if cost.cache_creation_cost:
                trace.record_metric("cache_creation_cost_usd", cost.cache_creation_cost)
                span.metadata["cache_creation_cost_usd"] = cost.cache_creation_cost

    latency_ms = round((perf_counter() - started) * 1000, 3)
    payload = sanitize(
        {
            "provider": provider,
            "operation": operation,
            "latency_ms": latency_ms,
            **_response_payload(response),
        }
    )
    span.metadata["latency_ms"] = latency_ms
    span.record_output(payload)
    trace.record_event(
        "llm.response",
        payload,
    )
    return payload


def _record_error(trace: Trace, span: Span, provider: str, operation: str, exc: BaseException, started: float) -> None:
    span.record_error(exc)
    trace.record_error(exc, event_type="llm.error")
    trace.record_event(
        "llm.response",
        {
            "provider": provider,
            "operation": operation,
            "latency_ms": round((perf_counter() - started) * 1000, 3),
            "error": str(exc),
        },
    )


def _is_stream_request(kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("stream"))


def _chunk_delta_content(chunk: Any) -> str | None:
    """Extract text delta from OpenAI-style or Anthropic-style stream chunks."""
    # OpenAI chat.completions stream: choices[0].delta.content
    choices = _get_attr_or_key(chunk, "choices")
    if choices:
        delta = _get_attr_or_key(choices[0], "delta")
        if delta is not None:
            content = _get_attr_or_key(delta, "content")
            if isinstance(content, str) and content:
                return content
            # Some SDKs put text on delta.text
            text = _get_attr_or_key(delta, "text")
            if isinstance(text, str) and text:
                return text
        # Rare: choice.text
        text = _get_attr_or_key(choices[0], "text")
        if isinstance(text, str) and text:
            return text

    # Anthropic: type=content_block_delta, delta.text
    ctype = _get_attr_or_key(chunk, "type")
    if ctype == "content_block_delta":
        delta = _get_attr_or_key(chunk, "delta")
        if delta is not None:
            text = _get_attr_or_key(delta, "text")
            if isinstance(text, str) and text:
                return text

    # OpenAI responses stream: output_text.delta style
    if ctype == "response.output_text.delta":
        delta = _get_attr_or_key(chunk, "delta")
        if isinstance(delta, str) and delta:
            return delta

    return None


def _chunk_usage(chunk: Any) -> dict[str, Any]:
    usage = _extract_usage(chunk)
    if usage:
        return usage
    return {}


def _chunk_model(chunk: Any, fallback: str | None) -> str | None:
    model = _get_attr_or_key(chunk, "model")
    if isinstance(model, str) and model:
        return model
    return fallback


def _chunk_finish_reason(chunk: Any) -> Any:
    choices = _get_attr_or_key(chunk, "choices")
    if choices:
        reason = _get_attr_or_key(choices[0], "finish_reason")
        if reason is not None:
            return reason
    # Anthropic
    delta = _get_attr_or_key(chunk, "delta")
    if delta is not None:
        return _get_attr_or_key(delta, "stop_reason")
    return _get_attr_or_key(chunk, "stop_reason")


class _StreamRecorder:
    """Accumulate stream chunks and finalize the LLM span once."""

    def __init__(
        self,
        *,
        trace: Trace,
        span: Span,
        span_cm: Any,
        provider: str,
        operation: str,
        model: str | None,
        started: float,
        auto_trace_cm: Any | None,
    ) -> None:
        self.trace = trace
        self.span = span
        self.span_cm = span_cm
        self.provider = provider
        self.operation = operation
        self.model = model
        self.started = started
        self.auto_trace_cm = auto_trace_cm
        self.content_parts: list[str] = []
        self.usage: dict[str, Any] = {}
        self.finish_reason: Any = None
        self.first_token_at: float | None = None
        self._done = False
        self.response_id: Any = None

    def on_chunk(self, chunk: Any) -> None:
        if self._done:
            return
        content = _chunk_delta_content(chunk)
        if content:
            if self.first_token_at is None:
                self.first_token_at = perf_counter()
                ttft_ms = round((self.first_token_at - self.started) * 1000, 3)
                self.span.metadata["ttft_ms"] = ttft_ms
                self.trace.record_event(
                    "llm.first_token",
                    {
                        "provider": self.provider,
                        "operation": self.operation,
                        "ttft_ms": ttft_ms,
                    },
                )
            self.content_parts.append(content)

        usage = _chunk_usage(chunk)
        if usage:
            self.usage.update(usage)

        model = _chunk_model(chunk, self.model)
        if model:
            self.model = model

        rid = _get_attr_or_key(chunk, "id")
        if rid is not None:
            self.response_id = rid

        reason = _chunk_finish_reason(chunk)
        if reason is not None:
            self.finish_reason = reason

    def finish_ok(self) -> None:
        if self._done:
            return
        self._done = True
        content = "".join(self.content_parts)
        # Build a response-like object for shared cost/token recording
        response = SimpleNamespace(
            id=self.response_id,
            model=self.model,
            usage=self.usage or None,
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish_reason,
                    message=SimpleNamespace(content=content or None, tool_calls=None),
                )
            ]
            if content or self.finish_reason is not None
            else None,
            content=content or None,
            output_text=content or None,
        )
        if self.first_token_at is not None:
            self.span.metadata["ttft_ms"] = round((self.first_token_at - self.started) * 1000, 3)
        self.span.metadata["stream"] = True
        try:
            output = _record_response(
                self.trace,
                self.span,
                self.provider,
                self.operation,
                response,
                self.started,
            )
        except BaseException as exc:
            self.span_cm.__exit__(type(exc), exc, exc.__traceback__)
            if self.auto_trace_cm is not None:
                self.auto_trace_cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            self.span_cm.__exit__(None, None, None)
            if self.auto_trace_cm is not None:
                self.trace.record_output(output)
                self.auto_trace_cm.__exit__(None, None, None)

    def finish_error(self, exc: BaseException) -> None:
        if self._done:
            return
        self._done = True
        partial = "".join(self.content_parts)
        if partial:
            output = {
                "provider": self.provider,
                "operation": self.operation,
                "stream": True,
                "partial_content": partial,
                "error": str(exc),
            }
            self.span.record_output(output)
            if self.auto_trace_cm is not None:
                self.trace.record_output(output)
        self.span.metadata["stream"] = True
        try:
            _record_error(self.trace, self.span, self.provider, self.operation, exc, self.started)
        finally:
            try:
                self.span_cm.__exit__(type(exc), exc, exc.__traceback__)
            finally:
                if self.auto_trace_cm is not None:
                    self.auto_trace_cm.__exit__(type(exc), exc, exc.__traceback__)


class _InstrumentedStream:
    """Sync iterator proxy that records stream lifecycle."""

    def __init__(self, stream: Any, recorder: _StreamRecorder) -> None:
        self._stream = stream
        self._recorder = recorder

    def __iter__(self) -> "_InstrumentedStream":
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._stream)
        except StopIteration:
            self._recorder.finish_ok()
            raise
        except Exception as exc:
            self._recorder.finish_error(exc)
            raise
        self._recorder.on_chunk(chunk)
        return chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def __enter__(self) -> "_InstrumentedStream":
        enter = getattr(self._stream, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        exit_method = getattr(self._stream, "__exit__", None)
        try:
            suppressed = bool(exit_method(exc_type, exc, tb)) if exit_method is not None else False
        except Exception as exit_exc:
            self._recorder.finish_error(exit_exc)
            raise
        if exc is not None:
            self._recorder.finish_error(exc)
        else:
            self._recorder.finish_ok()
        return suppressed

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        try:
            if close is not None:
                close()
        except Exception as exc:
            self._recorder.finish_error(exc)
            raise
        else:
            self._recorder.finish_ok()


class _InstrumentedAsyncStream:
    """Async iterator proxy that records stream lifecycle."""

    def __init__(self, stream: Any, recorder: _StreamRecorder) -> None:
        self._stream = stream
        self._recorder = recorder

    def __aiter__(self) -> "_InstrumentedAsyncStream":
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._stream.__anext__()
        except StopAsyncIteration:
            self._recorder.finish_ok()
            raise
        except Exception as exc:
            self._recorder.finish_error(exc)
            raise
        self._recorder.on_chunk(chunk)
        return chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    async def __aenter__(self) -> "_InstrumentedAsyncStream":
        enter = getattr(self._stream, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        exit_method = getattr(self._stream, "__aexit__", None)
        try:
            suppressed = bool(await exit_method(exc_type, exc, tb)) if exit_method is not None else False
        except Exception as exit_exc:
            self._recorder.finish_error(exit_exc)
            raise
        if exc is not None:
            self._recorder.finish_error(exc)
        else:
            self._recorder.finish_ok()
        return suppressed

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        try:
            if close is not None:
                await close()
        except Exception as exc:
            self._recorder.finish_error(exc)
            raise
        else:
            self._recorder.finish_ok()


def _looks_like_stream(result: Any) -> bool:
    if result is None:
        return False
    if isinstance(result, (str, bytes, dict, list)):
        return False
    # OpenAI Stream objects are iterable; avoid treating coroutine as stream
    if inspect.isawaitable(result) and not hasattr(result, "__aiter__"):
        return False
    return hasattr(result, "__iter__") or hasattr(result, "__aiter__")


def _wrap_stream_result(
    result: Any,
    *,
    trace: Trace,
    span: Span,
    span_cm: Any,
    provider: str,
    operation: str,
    model: str | None,
    started: float,
    auto_trace_cm: Any | None = None,
) -> Any:
    recorder = _StreamRecorder(
        trace=trace,
        span=span,
        span_cm=span_cm,
        provider=provider,
        operation=operation,
        model=model,
        started=started,
        auto_trace_cm=auto_trace_cm,
    )
    if hasattr(result, "__aiter__"):
        return _InstrumentedAsyncStream(result, recorder)
    return _InstrumentedStream(result, recorder)


def _start_auto_trace(
    provider: str,
    operation: str,
    request_payload: dict[str, Any],
) -> tuple[Trace, Any]:
    # Import lazily to avoid the client/providers import cycle.
    from evalon.core.client import get_client

    trace_cm = get_client().trace(
        f"{provider}.{operation}",
        input=request_payload,
        metadata={
            "evalon.auto_trace": True,
            "provider": provider,
            "operation": operation,
        },
    )
    return trace_cm.__enter__(), trace_cm


def _active_or_auto_trace(
    provider: str,
    operation: str,
    request_payload: dict[str, Any],
) -> tuple[Trace, Any | None]:
    trace = current_trace()
    if trace is not None:
        return trace, None
    return _start_auto_trace(provider, operation, request_payload)


def _finish_auto_success(
    trace: Trace,
    auto_trace_cm: Any | None,
    output: Any,
) -> None:
    if auto_trace_cm is None:
        return
    trace.record_output(output)
    auto_trace_cm.__exit__(None, None, None)


def _finish_auto_error(auto_trace_cm: Any | None, exc: BaseException) -> None:
    if auto_trace_cm is not None:
        auto_trace_cm.__exit__(type(exc), exc, exc.__traceback__)


def instrument_llm_call(provider: str, operation: str, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    request_payload = _request_payload(provider, operation, kwargs)
    if args:
        request_payload["args"] = sanitize(args)
    stream_requested = _is_stream_request(kwargs)

    def span_metadata() -> dict[str, Any]:
        params = {k: kwargs[k] for k in _SAMPLING_PARAMS if k in kwargs}
        meta = {
            "provider": provider,
            "operation": operation,
            "model": kwargs.get("model"),
            "sampling_params": params or None,
        }
        if stream_requested:
            meta["stream"] = True
        return meta

    if inspect.iscoroutinefunction(call):

        async def call_and_record() -> Any:
            trace, auto_trace_cm = _active_or_auto_trace(
                provider,
                operation,
                request_payload,
            )
            span_cm = trace.span(operation, kind="llm", metadata=span_metadata())
            span = span_cm.__enter__()
            span.record_input(request_payload)
            _record_request(trace, request_payload)
            started = perf_counter()
            try:
                response = await call(*args, **kwargs)
            except Exception as exc:
                _record_error(trace, span, provider, operation, exc, started)
                span_cm.__exit__(type(exc), exc, exc.__traceback__)
                _finish_auto_error(auto_trace_cm, exc)
                raise
            if stream_requested:
                return _wrap_stream_result(
                    response,
                    trace=trace,
                    span=span,
                    span_cm=span_cm,
                    provider=provider,
                    operation=operation,
                    model=kwargs.get("model"),
                    started=started,
                    auto_trace_cm=auto_trace_cm,
                )
            try:
                output = _record_response(trace, span, provider, operation, response, started)
                return response
            except BaseException as exc:
                _finish_auto_error(auto_trace_cm, exc)
                raise
            finally:
                span_cm.__exit__(None, None, None)
                if "output" in locals():
                    _finish_auto_success(trace, auto_trace_cm, output)

        return call_and_record()

    trace, auto_trace_cm = _active_or_auto_trace(
        provider,
        operation,
        request_payload,
    )
    span_cm = trace.span(operation, kind="llm", metadata=span_metadata())
    span = span_cm.__enter__()
    span.record_input(request_payload)
    _record_request(trace, request_payload)
    started = perf_counter()
    try:
        result = call(*args, **kwargs)
    except Exception as exc:
        _record_error(trace, span, provider, operation, exc, started)
        span_cm.__exit__(type(exc), exc, exc.__traceback__)
        _finish_auto_error(auto_trace_cm, exc)
        raise

    if inspect.isawaitable(result) and not hasattr(result, "__aiter__"):

        async def await_and_record() -> Any:
            try:
                response = await result
            except Exception as exc:
                _record_error(trace, span, provider, operation, exc, started)
                span_cm.__exit__(type(exc), exc, exc.__traceback__)
                _finish_auto_error(auto_trace_cm, exc)
                raise
            if stream_requested:
                return _wrap_stream_result(
                    response,
                    trace=trace,
                    span=span,
                    span_cm=span_cm,
                    provider=provider,
                    operation=operation,
                    model=kwargs.get("model"),
                    started=started,
                    auto_trace_cm=auto_trace_cm,
                )
            try:
                output = _record_response(trace, span, provider, operation, response, started)
                return response
            except BaseException as exc:
                _finish_auto_error(auto_trace_cm, exc)
                raise
            finally:
                span_cm.__exit__(None, None, None)
                if "output" in locals():
                    _finish_auto_success(trace, auto_trace_cm, output)

        return await_and_record()

    if stream_requested:
        return _wrap_stream_result(
            result,
            trace=trace,
            span=span,
            span_cm=span_cm,
            provider=provider,
            operation=operation,
            model=kwargs.get("model"),
            started=started,
            auto_trace_cm=auto_trace_cm,
        )

    try:
        output = _record_response(trace, span, provider, operation, result, started)
        return result
    except BaseException as exc:
        _finish_auto_error(auto_trace_cm, exc)
        raise
    finally:
        span_cm.__exit__(None, None, None)
        if "output" in locals():
            _finish_auto_success(trace, auto_trace_cm, output)


class _Proxy:
    def __init__(self, target: Any) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class OpenAIWrapper(_Proxy):
    def __init__(self, target: Any, *, provider: str) -> None:
        super().__init__(target)
        self._provider = provider

    @property
    def chat(self) -> "OpenAIChatWrapper":
        return OpenAIChatWrapper(self._target.chat, provider=self._provider)

    @property
    def responses(self) -> "OpenAIResponsesWrapper":
        return OpenAIResponsesWrapper(self._target.responses, provider=self._provider)


class OpenAIChatWrapper(_Proxy):
    def __init__(self, target: Any, *, provider: str) -> None:
        super().__init__(target)
        self._provider = provider

    @property
    def completions(self) -> "OpenAICompletionsWrapper":
        return OpenAICompletionsWrapper(self._target.completions, provider=self._provider)


class OpenAICompletionsWrapper(_Proxy):
    def __init__(self, target: Any, *, provider: str) -> None:
        super().__init__(target)
        self._provider = provider

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return instrument_llm_call(self._provider, "chat.completions.create", self._target.create, *args, **kwargs)


class OpenAIResponsesWrapper(_Proxy):
    def __init__(self, target: Any, *, provider: str) -> None:
        super().__init__(target)
        self._provider = provider

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return instrument_llm_call(self._provider, "responses.create", self._target.create, *args, **kwargs)


class AnthropicWrapper(_Proxy):
    @property
    def messages(self) -> "AnthropicMessagesWrapper":
        return AnthropicMessagesWrapper(self._target.messages)


class AnthropicMessagesWrapper(_Proxy):
    def create(self, *args: Any, **kwargs: Any) -> Any:
        return instrument_llm_call("anthropic", "messages.create", self._target.create, *args, **kwargs)
