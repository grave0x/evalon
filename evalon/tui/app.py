"""Evalon read-only observability TUI."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from evalon.paths import default_db_path
from evalon.tui.dither import (
    DitherButton,
    DitherLogo,
    gradient_text,
)
from evalon.tui.store import (
    ObservabilityStore,
    ProjectRow,
    ProjectSnapshot,
    Snapshot,
    TraceDetail,
    TraceRow,
)


EVALON_THEME = Theme(
    name="evalon",
    primary="#ff7a18",
    secondary="#d75b0b",
    accent="#ffb04a",
    warning="#ffb04a",
    error="#ff3b18",
    success="#d9741b",
    foreground="#d9a069",
    background="#030201",
    surface="#080401",
    panel="#0b0502",
    variables={
        "block-cursor-background": "#3d1704",
        "block-cursor-foreground": "#fff0dd",
        "block-cursor-text-style": "bold",
        "input-cursor-background": "#ff9d42",
        "input-cursor-foreground": "#030201",
        "input-selection-background": "#7a310f 70%",
        "button-color-foreground": "#fff0dd",
        "footer-key-foreground": "#ff9d42",
    },
)


def _duration(milliseconds: float) -> str:
    if milliseconds <= 0:
        return "—"
    if milliseconds < 1_000:
        return f"{milliseconds:.0f} ms"
    return f"{milliseconds / 1_000:.2f} s"


def _cost(value: float) -> str:
    if value <= 0:
        return "$0"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _compact_number(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def _status(value: str) -> Text:
    normalized = value.lower()
    if normalized == "error":
        return Text("● ERROR", style="bold #ff3b18")
    if normalized == "running":
        return Text("◉ LIVE", style="bold #ffb04a")
    return Text("● OK", style="#d9741b")


def _project_health(project: ProjectRow) -> Text:
    if project.error_count:
        return Text("● DEGRADED", style="bold #ff3b18")
    if project.running_count:
        return Text("◉ LIVE", style="bold #ffb04a")
    return Text("● HEALTHY", style="#d9741b")


def _project_health_name(project: ProjectRow) -> str:
    if project.error_count:
        return "degraded"
    if project.running_count:
        return "live"
    return "healthy"


def _short_id(value: object, width: int = 20) -> str:
    text = str(value or "—")
    if len(text) <= width:
        return text
    edge = max(4, (width - 1) // 2)
    return f"{text[:edge]}…{text[-edge:]}"


def _preview(value: object, limit: int = 100) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        text = " ".join(value.split())
        text = text.replace("**", "").replace("`", "")
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _format_value(value: object) -> str:
    """Format recorded telemetry without discarding nested fields."""
    if value is None:
        return "—"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                value = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                return value
        else:
            return value
    try:
        return json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        return str(value)


def _format_span_input(
    span: dict[str, Any],
    *,
    previous_llm_span: dict[str, Any] | None = None,
    trace_input: object = None,
    show_system_prompt: bool = True,
) -> str:
    value = span.get("input")
    if str(span.get("kind") or "").lower() != "llm" or not isinstance(value, dict):
        return _format_polished_value(value)

    sections: list[str] = []
    if show_system_prompt:
        system = value.get("system")
        if system is not None:
            sections.append(f"SYSTEM PROMPT\n{_format_polished_value(system)}")
        instructions = value.get("instructions")
        if instructions is not None and instructions != system:
            sections.append(f"INSTRUCTIONS\n{_format_polished_value(instructions)}")

    direct_input = value.get("input")
    if direct_input is not None:
        sections.append(f"INPUT\n{_format_polished_value(direct_input)}")

    previous_input = (
        previous_llm_span.get("input")
        if isinstance(previous_llm_span, dict)
        else None
    )
    previous_messages = (
        previous_input.get("messages")
        if isinstance(previous_input, dict)
        else None
    )
    messages = _llm_request_messages(
        value.get("messages"),
        previous_messages,
        trace_input=trace_input,
    )
    if messages:
        sections.append(_format_messages(messages))

    if sections:
        return "\n\n".join(sections)

    fallback = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "provider",
            "operation",
            "model",
            "sampling_params",
            "system",
            "instructions",
            "input",
            "messages",
            "tools",
        }
        and item is not None
    }
    return _format_polished_value(fallback or value.get("messages"))


def _structured_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return value
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return value


def _format_polished_value(value: object, *, depth: int = 0) -> str:
    """Render structured span I/O as readable fields instead of raw JSON."""
    value = _structured_value(value)
    prefix = "  " * depth
    if value is None:
        return f"{prefix}—"
    if isinstance(value, bool):
        return f"{prefix}{'true' if value else 'false'}"
    if isinstance(value, dict):
        if not value:
            return f"{prefix}—"
        lines: list[str] = []
        for key, item in value.items():
            label = str(key).replace("_", " ").upper()
            item = _structured_value(item)
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{label}")
                lines.append(_format_polished_value(item, depth=depth + 1))
            elif isinstance(item, str) and "\n" in item:
                lines.append(f"{prefix}{label}")
                lines.extend(
                    f"{'  ' * (depth + 1)}{line}"
                    for line in item.splitlines()
                )
            else:
                rendered = "—" if item is None else str(item)
                lines.append(f"{prefix}{label}  {rendered}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{prefix}—"
        lines = []
        for index, item in enumerate(value, start=1):
            item = _structured_value(item)
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}[{index}]")
                lines.append(_format_polished_value(item, depth=depth + 1))
            else:
                lines.append(f"{prefix}[{index}] {item}")
        return "\n".join(lines)
    return f"{prefix}{value}"


def _format_tool_calls(tool_calls: object) -> str:
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""

    sections: list[str] = []
    for index, raw_call in enumerate(tool_calls, start=1):
        if not isinstance(raw_call, dict):
            sections.append(
                f"TOOL CALL {index}\n{_format_polished_value(raw_call)}"
            )
            continue
        function = raw_call.get("function")
        function = function if isinstance(function, dict) else raw_call
        name = function.get("name") or raw_call.get("name") or f"tool {index}"
        arguments = function.get("arguments")
        if arguments is None:
            arguments = function.get("input")
        body = _format_polished_value(arguments)
        sections.append(f"TOOL CALL {index} · {name}\n{body}")
    return "\n\n".join(sections)


def _format_messages(messages: list[object]) -> str:
    sections: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            sections.append(f"MESSAGE\n{_format_polished_value(message)}")
            continue

        role = str(message.get("role") or "message").upper()
        content = message.get("content")
        tool_calls = _format_tool_calls(message.get("tool_calls"))
        if role == "TOOL":
            name = message.get("name")
            heading = f"TOOL RESULT · {name}" if name else "TOOL RESULT"
            sections.append(f"{heading}\n{_format_polished_value(content)}")
            continue
        if content not in (None, ""):
            sections.append(f"{role}\n{_format_polished_value(content)}")
        if tool_calls:
            sections.append(tool_calls)
    return "\n\n".join(sections) or "—"


def _llm_request_messages(
    messages: object,
    previous_messages: object,
    *,
    trace_input: object,
) -> list[object]:
    """Return only messages newly supplied to this generation."""
    if not isinstance(messages, list):
        return []

    if isinstance(previous_messages, list):
        common = 0
        for previous, current in zip(previous_messages, messages):
            if previous != current:
                break
            common += 1
        if common == len(previous_messages):
            return messages[common:]

        # A provider or context manager rewrote earlier history. Keep the current
        # tool exchange rather than falling back to the entire conversation.
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("tool_calls")
            ):
                return messages[index:]

    # The first generation in a trace may still receive conversation history
    # from its session. Anchor it to the trace's new user input when possible.
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if trace_input is None or message.get("content") == trace_input:
            return messages[index:]

    non_system = [
        message
        for message in messages
        if not isinstance(message, dict) or message.get("role") != "system"
    ]
    return non_system[-1:]


def _resolved_llm_output(
    span: dict[str, Any],
    following_spans: list[dict[str, Any]],
) -> object:
    value = span.get("output")
    if str(span.get("kind") or "").lower() != "llm" or not isinstance(value, dict):
        return value

    response = value.get("response")
    has_response = (
        bool(response.get("content") or response.get("tool_calls"))
        if isinstance(response, dict)
        else response not in (None, "")
    )
    if has_response or value.get("content") or value.get("tool_calls"):
        return value

    current_input = span.get("input")
    current_messages = (
        current_input.get("messages") if isinstance(current_input, dict) else None
    )
    if not isinstance(current_messages, list):
        return value

    for candidate in following_spans:
        if str(candidate.get("kind") or "").lower() != "llm":
            continue
        candidate_input = candidate.get("input")
        candidate_messages = (
            candidate_input.get("messages")
            if isinstance(candidate_input, dict)
            else None
        )
        if (
            not isinstance(candidate_messages, list)
            or len(candidate_messages) <= len(current_messages)
            or candidate_messages[: len(current_messages)] != current_messages
        ):
            return value
        assistant_message = candidate_messages[len(current_messages)]
        if (
            not isinstance(assistant_message, dict)
            or assistant_message.get("role") != "assistant"
        ):
            return value
        recovered = {
            key: item
            for key, item in assistant_message.items()
            if key != "role"
        }
        if recovered:
            resolved = dict(value)
            resolved["response"] = recovered
            return resolved
        return value
    return value


def _format_span_output(
    span: dict[str, Any],
    following_spans: list[dict[str, Any]] | None = None,
) -> str:
    value = _resolved_llm_output(span, following_spans or [])
    if str(span.get("kind") or "").lower() != "llm" or not isinstance(value, dict):
        return _format_polished_value(value)

    raw_response = value.get("response")
    response = raw_response if isinstance(raw_response, dict) else {}
    tool_calls = value.get("tool_calls") or response.get("tool_calls")
    content = value.get("content")
    if content in (None, ""):
        content = response.get("content")
    if content in (None, "") and raw_response not in (None, "") and not response:
        content = raw_response

    if isinstance(content, list):
        text_blocks: list[object] = []
        block_tool_calls: list[object] = []
        other_blocks: list[object] = []
        for block in content:
            if not isinstance(block, dict):
                text_blocks.append(block)
            elif block.get("type") in {"tool_use", "tool_call"}:
                block_tool_calls.append(block)
            elif block.get("type") == "text" and block.get("text") is not None:
                text_blocks.append(block["text"])
            else:
                other_blocks.append(block)
        if not tool_calls and block_tool_calls:
            tool_calls = block_tool_calls
        content = "\n".join(str(block) for block in text_blocks)
        if not content and other_blocks:
            content = other_blocks

    sections: list[str] = []
    polished_calls = _format_tool_calls(tool_calls)
    if polished_calls:
        sections.append(polished_calls)
    if content not in (None, ""):
        sections.append(f"ASSISTANT\n{_format_polished_value(content)}")
    if sections:
        return "\n\n".join(sections)

    semantic_fallback = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "provider",
            "operation",
            "model",
            "id",
            "finish_reason",
            "latency_ms",
            "usage",
            "response",
            "content",
            "tool_calls",
        }
        and item is not None
    }
    return _format_polished_value(semantic_fallback or response or value)


def _following_tool_messages(
    span: dict[str, Any],
    following_spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_input = span.get("input")
    current_messages = (
        current_input.get("messages")
        if isinstance(current_input, dict)
        else None
    )
    if not isinstance(current_messages, list):
        return []

    for candidate in following_spans:
        if str(candidate.get("kind") or "").lower() != "llm":
            continue
        candidate_input = candidate.get("input")
        candidate_messages = (
            candidate_input.get("messages")
            if isinstance(candidate_input, dict)
            else None
        )
        if (
            not isinstance(candidate_messages, list)
            or candidate_messages[: len(current_messages)] != current_messages
        ):
            return []
        return [
            message
            for message in candidate_messages[len(current_messages) :]
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
    return []


def _following_tool_spans(
    following_spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tool_spans: list[dict[str, Any]] = []
    for candidate in following_spans:
        kind = str(candidate.get("kind") or "").lower()
        if kind == "llm":
            break
        if kind == "tool":
            tool_spans.append(candidate)
    return tool_spans


def _metric(label: str, value: str) -> Text:
    text = Text()
    text.append(f"{label.upper()} ", style="#6f3e1c")
    text.append(value, style="bold #ff9d42")
    return text


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _started_at(value: object) -> str:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return str(value or "—")
    return timestamp.strftime("%m-%d %H:%M:%S")


def _event_style(event_type: str) -> tuple[str, str]:
    if event_type.startswith("llm."):
        return "◆", "bold #ffb04a"
    if event_type.startswith("tool."):
        return "■", "bold #ff7a18"
    if "error" in event_type:
        return "!", "bold #ff3b18"
    return "●", "bold #c76a20"


def _event_detail(event_type: str, raw_payload: object) -> str:
    if not isinstance(raw_payload, dict):
        return _preview(raw_payload, 76)
    payload: dict[str, Any] = raw_payload

    if event_type == "trace.start":
        return f"input  {_preview(payload.get('input'), 42)}"
    if event_type == "trace.output":
        return f"output  {_preview(payload.get('output'), 42)}"
    if event_type == "trace.end":
        return f"status  {payload.get('status', '—')}"
    if event_type == "llm.request":
        provider = payload.get("provider") or "provider"
        model = payload.get("model") or "model"
        messages = payload.get("messages")
        message_count = len(messages) if isinstance(messages, list) else 0
        return f"{provider} · {_short_id(model, 20)} · {message_count} msgs"
    if event_type == "llm.response":
        model = _short_id(payload.get("model") or "model", 20)
        usage = payload.get("usage")
        token_count = 0.0
        if isinstance(usage, dict):
            token_count = float(usage.get("total_tokens") or 0)
        latency = _duration(float(payload.get("latency_ms") or 0))
        return f"{model} · {_compact_number(token_count)} tokens · {latency}"
    if event_type == "tool.call":
        return (
            f"{payload.get('name', 'tool')} · "
            f"{_preview(payload.get('arguments'), 36)}"
        )
    if event_type == "tool.output":
        latency = _duration(float(payload.get("latency_ms") or 0))
        return (
            f"{payload.get('name', 'tool')} · "
            f"{_preview(payload.get('output'), 30)} · {latency}"
        )

    pairs = [
        f"{key}={_preview(value, 24)}"
        for key, value in list(payload.items())[:3]
        if value is not None
    ]
    return " · ".join(pairs) or "—"


def _span_depth(
    span: dict[str, Any],
    spans_by_id: dict[str, dict[str, Any]],
) -> int:
    """Return a bounded nesting depth for a span."""
    depth = 0
    parent_id = str(span.get("parent_span_id") or "")
    visited: set[str] = set()
    while parent_id and parent_id not in visited and depth < 8:
        visited.add(parent_id)
        parent = spans_by_id.get(parent_id)
        if parent is None:
            break
        depth += 1
        parent_id = str(parent.get("parent_span_id") or "")
    return depth


def _compact_path(value: object, *, file_path: bool = False) -> str:
    text = str(value or "—")
    path = Path(text)
    if not path.is_absolute():
        return text
    if not file_path:
        return path.name or str(path)
    return "…/" + "/".join(path.parts[-3:])


def _summarize_names(names: list[str]) -> str:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(
        f"{name} ×{count}" if count > 1 else name for name, count in counts.items()
    )


def _tool_input_summary(value: object) -> str:
    value = _structured_value(value)
    if not isinstance(value, dict):
        return _preview(value, 72)

    parts: list[str] = []
    used: set[str] = set()
    for key in ("pattern", "query", "task", "command", "cmd"):
        if value.get(key) not in (None, ""):
            parts.append(f'{key} "{_preview(value[key], 34)}"')
            used.add(key)
            break

    if value.get("file_path") not in (None, ""):
        parts.append(_compact_path(value["file_path"], file_path=True))
        used.add("file_path")
    elif value.get("path") not in (None, ""):
        path = _compact_path(value["path"])
        parts.append(f"in {path}" if parts else path)
        used.add("path")

    offset = value.get("offset")
    limit = value.get("limit")
    if isinstance(offset, int) and isinstance(limit, int):
        parts.append(f"lines {offset}–{offset + limit - 1}")
        used.update({"offset", "limit"})
    elif isinstance(offset, int):
        parts.append(f"from line {offset}")
        used.add("offset")
    elif isinstance(limit, int):
        parts.append(f"first {limit} lines")
        used.add("limit")

    for key, item in value.items():
        if key in used or item in (None, "") or isinstance(item, (dict, list)):
            continue
        parts.append(f"{key} {_preview(item, 24)}")
        if len(parts) == 3:
            break
    return " · ".join(parts) or "no input"


def _line_range(value: str) -> tuple[int, int] | None:
    numbers = [
        int(prefix)
        for line in value.splitlines()
        if "\t" in line and (prefix := line.split("\t", 1)[0].strip()).isdigit()
    ]
    return (numbers[0], numbers[-1]) if numbers else None


def _tool_output_summary(span: dict[str, Any]) -> str:
    error = _structured_value(span.get("error"))
    if error is not None:
        if isinstance(error, dict):
            error_type = str(error.get("type") or "error")
            message = _preview(error.get("message"), 44)
            return f"{error_type} · {message}"
        return f"error · {_preview(error, 52)}"

    value = _structured_value(span.get("output"))
    if value is None:
        return "no output"
    if not isinstance(value, str):
        if isinstance(value, list):
            return f"{len(value)} items · {_preview(value[:1], 42)}"
        if isinstance(value, dict):
            pairs = [
                f"{key} {_preview(item, 22)}" for key, item in list(value.items())[:3]
            ]
            return " · ".join(pairs) or "empty object"
        return _preview(value, 72)

    name = str(span.get("name") or "")
    lines = [line for line in value.splitlines() if line.strip()]
    if name == "file_reader" or value.startswith("File Content:"):
        line_range = _line_range(value)
        if line_range is not None:
            start, end = line_range
            return f"{end - start + 1} lines read"
        return f"{max(0, len(lines) - 1)} lines read"
    if name == "glob":
        count = len(lines)
        first = _compact_path(lines[0], file_path=True) if lines else "—"
        label = "file" if count == 1 else "files"
        return f"{count} {label} · {first}"
    if name == "grep_search":
        count = len(lines)
        first = lines[0] if lines else "—"
        if ":" in first:
            prefix, remainder = first.split(":", 1)
            if prefix.isdigit():
                first = f"line {prefix} · {_preview(remainder, 30)}"
            else:
                path, line, *_ = first.split(":", 2)
                if line.isdigit():
                    first = f"{_compact_path(path, file_path=True)}:{line}"
        label = "match" if count == 1 else "matches"
        return f"{count} {label} · {first}"
    if name == "ls":
        count = len(lines)
        label = "entry" if count == 1 else "entries"
        return f"{count} {label} · {_preview(lines[0] if lines else None, 42)}"
    return _preview(value, 72)


def _llm_input_summary(value: object) -> str:
    request = value if isinstance(value, dict) else {}
    messages = request.get("messages")
    if not isinstance(messages, list):
        return _preview(value, 72)

    trailing_tools: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            break
        trailing_tools.append(str(message.get("name") or "tool"))
    if trailing_tools:
        trailing_tools.reverse()
        return f"RESULTS · {_summarize_names(trailing_tools)}"

    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            names = []
            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else None
                function = function if isinstance(function, dict) else {}
                names.append(str(function.get("name") or "tool"))
            return f"CALLS · {_summarize_names(names)}"
        role = str(message.get("role") or "input").upper()
        return f"{role} · {_preview(message.get('content'), 60)}"
    return "request recorded"


def _span_request_summary(span: dict[str, Any]) -> str:
    kind = str(span.get("kind") or "").lower()
    value = span.get("input")
    if kind == "llm":
        return _llm_input_summary(value)
    return _tool_input_summary(value)


def _span_result_summary(
    span: dict[str, Any],
    following_spans: list[dict[str, Any]],
) -> str:
    kind = str(span.get("kind") or "").lower()
    if kind != "llm":
        return _tool_output_summary(span)

    resolved = _resolved_llm_output(span, following_spans)
    response: object = None
    tool_calls: object = None
    if isinstance(resolved, dict):
        raw_response = resolved.get("response")
        if isinstance(raw_response, dict):
            response = raw_response.get("content")
            tool_calls = raw_response.get("tool_calls")
        else:
            response = raw_response
        response = resolved.get("content") or response
        tool_calls = resolved.get("tool_calls") or tool_calls
    else:
        response = resolved

    following_tools = _following_tool_spans(following_spans)
    if following_tools:
        names = [str(tool.get("name") or "tool") for tool in following_tools]
        return f"CALLS · {_summarize_names(names)}"
    if isinstance(tool_calls, list) and tool_calls:
        names = []
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            function = function if isinstance(function, dict) else {}
            names.append(str(function.get("name") or "tool"))
        return f"CALLS · {_summarize_names(names)}"
    if response not in (None, "", [], {}):
        return f"ANSWER · {_preview(response, 60)}"
    return "response recorded"


class MetricCard(Static):
    DEFAULT_CSS = """
    MetricCard {
        width: 1fr;
        height: 4;
        margin-right: 1;
        padding: 0 1;
        background: #080401;
        border: tall #2a1609;
    }
    MetricCard .metric-label {
        color: #8d5426;
    }
    """

    def __init__(self, label: str, value: str = "—", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.label = label
        self.value = value

    def set_value(self, value: str) -> None:
        self.value = value
        self.refresh()

    def render(self) -> Text:
        output = Text()
        output.append(self.label.upper(), style="#8d5426")
        output.append("\n")
        output.append_text(gradient_text(self.value, bold=True))
        return output


class ProjectIndexTable(DataTable[object]):
    MIN_COLUMN_WIDTHS = (10, 10, 6, 8, 11, 7, 7, 6, 14)
    COLUMN_WEIGHTS = (1, 2, 1, 1, 1, 1, 1, 1, 2)

    def _on_resize(self, event: events.Resize) -> None:
        super()._on_resize(event)
        self.call_after_refresh(self.fit_columns)

    def fit_columns(self) -> None:
        """Spread project columns across the visible index width."""
        columns = self.ordered_columns
        if len(columns) != len(self.MIN_COLUMN_WIDTHS):
            return

        viewport_width = self.scrollable_content_region.width
        cell_padding = 2 * self.cell_padding * len(columns)
        available_width = max(0, viewport_width - cell_padding)
        preferred_widths = [
            max(column.content_width, minimum)
            for column, minimum in zip(
                columns,
                self.MIN_COLUMN_WIDTHS,
                strict=True,
            )
        ]
        extra_width = max(0, available_width - sum(preferred_widths))
        width_per_weight, remainder = divmod(
            extra_width,
            sum(self.COLUMN_WEIGHTS),
        )
        fitted_widths = [
            preferred + (width_per_weight * weight)
            for preferred, weight in zip(
                preferred_widths,
                self.COLUMN_WEIGHTS,
                strict=True,
            )
        ]
        for index in range(remainder):
            fitted_widths[index % len(fitted_widths)] += 1

        changed = False
        for column, width in zip(columns, fitted_widths, strict=True):
            if column.auto_width or column.width != width:
                column.auto_width = False
                column.width = width
                changed = True
        if changed:
            self._require_update_dimensions = True
            self.refresh(layout=True)


class ExecutionFlowTable(DataTable[object]):
    MIN_COLUMN_WIDTHS = (18, 7, 8, 9, 8, 22, 22)
    COLUMN_WEIGHTS = (3, 1, 1, 1, 1, 5, 5)

    def _on_resize(self, event: events.Resize) -> None:
        super()._on_resize(event)
        self.call_after_refresh(self.fit_columns)

    def fit_columns(self) -> None:
        """Keep the complete request/result flow visible without horizontal scroll."""
        columns = self.ordered_columns
        if len(columns) != len(self.MIN_COLUMN_WIDTHS):
            return

        viewport_width = self.scrollable_content_region.width
        cell_padding = 2 * self.cell_padding * len(columns)
        # DataTable reserves two cells beyond column content for its internal
        # gutter. Account for them so the merged view does not gain a pointless
        # two-cell horizontal scroll range.
        available_width = max(0, viewport_width - cell_padding - 2)
        extra_width = max(0, available_width - sum(self.MIN_COLUMN_WIDTHS))
        width_per_weight, remainder = divmod(
            extra_width,
            sum(self.COLUMN_WEIGHTS),
        )
        fitted_widths = [
            minimum + (width_per_weight * weight)
            for minimum, weight in zip(
                self.MIN_COLUMN_WIDTHS,
                self.COLUMN_WEIGHTS,
                strict=True,
            )
        ]
        for index in range(remainder):
            fitted_widths[index % len(fitted_widths)] += 1

        changed = False
        for column, width in zip(columns, fitted_widths, strict=True):
            if column.auto_width or column.width != width:
                column.auto_width = False
                column.width = width
                changed = True
        if changed:
            self._require_update_dimensions = True
            self.refresh(layout=True)


class EvalonApp(App[None]):
    TITLE = "Evalon"
    CSS = """
    Screen {
        background: #030201;
        color: #d9a069;
    }
    #home, #dashboard, #trace-detail-view, #span-detail-view {
        height: 1fr;
    }
    #home-metric-row {
        height: 4;
        margin: 1 1 0 1;
    }
    #home-body {
        height: 1fr;
        margin: 1;
        background: #050301;
        border: tall #2a1609;
    }
    #project-table {
        height: 1fr;
        background: #050301;
        color: #c78343;
    }
    #project-table > .datatable--header {
        background: #130904;
        color: #ff9d42;
        text-style: bold;
    }
    #project-table > .datatable--cursor {
        background: #3d1704;
        color: #fff0dd;
        text-style: bold;
    }
    #home-actions {
        height: 4;
        padding: 0 1;
        align: left middle;
        background: #030201;
    }
    #home-hint {
        width: 1fr;
        height: 3;
        padding: 1 0 0 1;
        color: #8d5426;
        text-align: right;
    }
    #metric-row {
        height: 4;
        margin: 1 1 0 1;
    }
    #toolbar {
        height: 3;
        margin: 1 1 0 1;
    }
    #search {
        width: 1fr;
        height: 3;
        color: #ffc184;
        background: #080401;
        border: tall #3a1d08;
    }
    #search:focus {
        border: tall #ff7a18;
    }
    #project-label {
        width: auto;
        min-width: 22;
        height: 3;
        padding: 1 1 0 1;
        color: #9f5c25;
    }
    #body {
        height: 1fr;
        margin: 1;
    }
    #trace-list {
        width: 11fr;
        margin-right: 1;
        background: #050301;
        border: tall #2a1609;
    }
    #trace-table {
        height: 1fr;
        background: #050301;
        color: #c78343;
    }
    #trace-table > .datatable--header {
        background: #130904;
        color: #ff9d42;
        text-style: bold;
    }
    #trace-table > .datatable--cursor {
        background: #3d1704;
        color: #fff0dd;
        text-style: bold;
    }
    #inspector {
        width: 9fr;
        background: #050301;
        border: tall #2a1609;
        padding: 0 1;
    }
    #inspector > .section-title {
        height: 1;
    }
    #event-stream-title {
        margin-top: 1;
    }
    .section-title {
        height: 2;
        padding: 0 1;
        color: #ff9d42;
        background: #0b0502;
        text-style: bold;
    }
    #trace-meta {
        height: 8;
        padding: 1 1 0 1;
        color: #c78343;
    }
    #events {
        height: 1fr;
        min-height: 7;
        background: #030201;
        color: #9d6333;
        border: none;
        padding: 0 1;
    }
    #actions {
        height: 4;
        padding: 0 1;
        align: left middle;
        background: #030201;
    }
    #live-status {
        width: 1fr;
        height: 3;
        padding: 1 0 0 1;
        color: #8d5426;
        text-align: right;
    }
    #detail-title {
        height: 2;
        margin: 1 1 0 1;
        padding: 0 1;
        color: #ff9d42;
        background: #0b0502;
        text-style: bold;
    }
    #detail-meta {
        height: 8;
        margin: 0 1;
        padding: 1;
        color: #c78343;
        background: #050301;
        border: tall #2a1609;
    }
    #detail-flow {
        height: 1fr;
        min-height: 6;
        margin: 1 1 0 1;
        background: #050301;
        border: tall #2a1609;
    }
    #execution-flow-table {
        height: 1fr;
        background: #050301;
        color: #c78343;
    }
    #execution-flow-table > .datatable--header {
        background: #130904;
        color: #ff9d42;
        text-style: bold;
    }
    #execution-flow-table > .datatable--cursor {
        background: #3d1704;
        color: #fff0dd;
        text-style: bold;
    }
    #detail-actions {
        height: 4;
        padding: 0 1;
        align: left middle;
        background: #030201;
    }
    #detail-hint {
        width: 1fr;
        height: 3;
        padding: 1 0 0 1;
        color: #8d5426;
        text-align: right;
    }
    #span-detail-title {
        height: 2;
        margin: 1 1 0 1;
        padding: 0 1;
        color: #ff9d42;
        background: #0b0502;
        text-style: bold;
    }
    #span-detail-meta {
        height: 8;
        margin: 0 1 1 1;
        padding: 1;
        color: #c78343;
        background: #050301;
        border: tall #2a1609;
    }
    #span-detail-body {
        height: 1fr;
        margin: 0 1;
        background: #050301;
        border: tall #2a1609;
    }
    #span-detail-body ContentTabs {
        height: 2;
        background: #0b0502;
        color: #8d5426;
        border-bottom: tall #4a1d08;
    }
    #span-detail-body Tab {
        color: #a8642d;
        text-style: bold;
        padding: 0 2;
    }
    #span-detail-body Tab.-active {
        color: #030201;
        background: #ff8a28;
        text-style: bold;
    }
    #span-detail-body ContentTabs:focus Tab.-active {
        color: #030201;
        background: #ffb04a;
    }
    #span-detail-body ContentSwitcher, #span-detail-body TabPane {
        height: 1fr;
        background: #030201;
    }
    #span-call-io {
        height: 1fr;
        padding: 1;
    }
    #span-input-panel {
        width: 2fr;
        margin-right: 1;
        background: #050301;
        border: tall #3a1d08;
    }
    #span-output-panel {
        width: 3fr;
        background: #050301;
        border: tall #6f2b0d;
    }
    #span-input-panel > .section-title {
        color: #d27a31;
    }
    #span-output-panel > .section-title {
        color: #ffb04a;
        background: #1c0b03;
    }
    #span-input, #span-output, #span-metadata, #span-events {
        height: 1fr;
        padding: 0 1;
        color: #c78343;
        background: #030201;
        border: none;
    }
    #span-detail-actions {
        height: 4;
        padding: 0 1;
        align: left middle;
        background: #030201;
    }
    #span-detail-hint {
        width: 1fr;
        height: 3;
        padding: 1 0 0 1;
        color: #8d5426;
        text-align: right;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit", show=False),
        Binding("r", "refresh_data", "refresh", show=False),
        Binding("space", "toggle_live", "live", show=False),
        Binding("/", "focus_search", "search", show=False),
        Binding("escape", "go_back", "back", show=False, priority=True),
        Binding("1", "show_span_input", "span input", show=False),
        Binding("2", "show_span_output", "span output", show=False),
        Binding("3", "show_span_metadata", "span metadata", show=False),
        Binding("4", "show_span_events", "span events", show=False),
    ]

    def __init__(self, store: ObservabilityStore, refresh_seconds: float = 2.0) -> None:
        super().__init__()
        self.register_theme(EVALON_THEME)
        self.theme = EVALON_THEME.name
        self.store = store
        self.refresh_seconds = max(0.5, refresh_seconds)
        self.project_snapshot = ProjectSnapshot()
        self.snapshot = Snapshot()
        self.filtered_traces: list[TraceRow] = []
        self.selected_project: str | None = store.project
        self.selected_trace_id: str | None = None
        self.selected_span_id: str | None = None
        self.active_view = "dashboard" if store.project else "home"
        self.live = True
        self._trace_keys: set[str] = set()
        self._span_keys: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="home"):
            yield DitherLogo(id="home-logo")
            with Horizontal(id="home-metric-row"):
                yield MetricCard("selected project", id="home-metric-projects")
                yield MetricCard("activity", id="home-metric-traces")
                yield MetricCard("reliability", id="home-metric-sessions")
                yield MetricCard("latency", id="home-metric-errors")
                yield MetricCard("usage", id="home-metric-cost")
            with Vertical(id="home-body"):
                yield Label(" PROJECT INDEX", classes="section-title")
                yield ProjectIndexTable(
                    id="project-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            with Horizontal(id="home-actions"):
                yield DitherButton(
                    "OPEN  enter",
                    id="open-project-button",
                    bloom="aura",
                )
                yield DitherButton("REFRESH  r", id="home-refresh-button")
                yield DitherButton("QUIT  q", id="home-quit-button")
                yield Static(
                    "↑↓ select  //  pulse follows selection  //  enter inspect",
                    id="home-hint",
                )
        with Vertical(id="dashboard"):
            with Horizontal(id="metric-row"):
                yield MetricCard("selected trace", id="metric-traces")
                yield MetricCard("timing", id="metric-sessions")
                yield MetricCard("activity", id="metric-errors")
                yield MetricCard("agent work", id="metric-latency")
                yield MetricCard("usage", id="metric-cost")
            with Horizontal(id="toolbar"):
                yield Input(
                    placeholder="/  filter trace, id, session, or status",
                    id="search",
                )
                yield Label("", id="project-label")
            with Horizontal(id="body"):
                with Vertical(id="trace-list"):
                    yield Label(" RECENT TRACES", classes="section-title")
                    yield DataTable(
                        id="trace-table",
                        cursor_type="row",
                        zebra_stripes=True,
                    )
                with Vertical(id="inspector"):
                    yield Label(" TRACE INSPECTOR", classes="section-title")
                    yield Static("select a trace", id="trace-meta")
                    yield Label(
                        " EVENT STREAM",
                        id="event-stream-title",
                        classes="section-title",
                    )
                    yield RichLog(
                        id="events",
                        markup=True,
                        wrap=False,
                        auto_scroll=False,
                    )
            with Horizontal(id="actions"):
                yield DitherButton(
                    "OPEN TRACE  enter",
                    id="open-trace-button",
                    bloom="aura",
                )
                yield DitherButton("PROJECTS  esc", id="projects-button")
                yield DitherButton(
                    "REFRESH  r",
                    id="refresh-button",
                )
                yield DitherButton("LIVE  space", id="live-button")
                yield DitherButton("QUIT  q", id="quit-button")
                yield Static("", id="live-status")
        with Vertical(id="trace-detail-view"):
            yield Static("TRACE DETAIL", id="detail-title")
            yield Static("select a trace", id="detail-meta")
            with Vertical(id="detail-flow"):
                yield Label(
                    " EXECUTION FLOW",
                    classes="section-title",
                )
                yield ExecutionFlowTable(
                    id="execution-flow-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            with Horizontal(id="detail-actions"):
                yield DitherButton(
                    "OPEN SPAN  enter",
                    id="open-span-button",
                    bloom="aura",
                )
                yield DitherButton(
                    "TRACES  esc",
                    id="detail-back-button",
                )
                yield DitherButton("REFRESH  r", id="detail-refresh-button")
                yield DitherButton("QUIT  q", id="detail-quit-button")
                yield Static("trace inspector  //  read-only", id="detail-hint")
        with Vertical(id="span-detail-view"):
            yield Static("SPAN DETAIL", id="span-detail-title")
            yield Static("select a span", id="span-detail-meta")
            with TabbedContent(
                initial="span-call-tab",
                id="span-detail-body",
            ):
                with TabPane("CALL", id="span-call-tab"):
                    with Horizontal(id="span-call-io"):
                        with Vertical(id="span-input-panel"):
                            yield Label(
                                " INPUT",
                                id="span-input-title",
                                classes="section-title",
                            )
                            yield RichLog(
                                id="span-input",
                                markup=False,
                                wrap=True,
                                min_width=1,
                                auto_scroll=False,
                            )
                        with Vertical(id="span-output-panel"):
                            yield Label(
                                " OUTPUT",
                                id="span-output-title",
                                classes="section-title",
                            )
                            yield RichLog(
                                id="span-output",
                                markup=False,
                                wrap=True,
                                min_width=1,
                                auto_scroll=False,
                            )
                with TabPane("METADATA / ERROR", id="span-metadata-tab"):
                    yield RichLog(
                        id="span-metadata",
                        markup=False,
                        wrap=True,
                        min_width=1,
                        auto_scroll=False,
                    )
                with TabPane("RAW", id="span-events-tab"):
                    yield RichLog(
                        id="span-events",
                        markup=False,
                        wrap=True,
                        min_width=1,
                        auto_scroll=False,
                    )
            with Horizontal(id="span-detail-actions"):
                yield DitherButton(
                    "SPAN LIST  esc",
                    id="span-back-button",
                    bloom="aura",
                )
                yield DitherButton("REFRESH  r", id="span-refresh-button")
                yield DitherButton("QUIT  q", id="span-quit-button")
                yield Static(
                    "1 input  2 output  3 metadata  4 events  //  scroll focused pane",
                    id="span-detail-hint",
                )

    def on_mount(self) -> None:
        project_table = self.query_one("#project-table", DataTable)
        project_table.add_columns(
            "HEALTH",
            "PROJECT",
            "TRACES",
            "SESSIONS",
            "ERRORS",
            "P95",
            "COST",
            "TOKENS",
            "LAST ACTIVITY",
        )
        trace_table = self.query_one("#trace-table", DataTable)
        trace_table.add_columns(
            "STATUS",
            "TRACE",
            "PROJECT",
            "SESSION",
            "SPANS",
            "LATENCY",
            "COST",
            "STARTED",
        )
        execution_flow_table = self.query_one(
            "#execution-flow-table",
            ExecutionFlowTable,
        )
        execution_flow_table.add_columns(
            "OPERATION",
            "KIND",
            "OFFSET",
            "DURATION",
            "STATUS",
            "INPUT",
            "OUTPUT",
        )
        self.call_after_refresh(execution_flow_table.fit_columns)
        self.set_interval(self.refresh_seconds, self._live_refresh)
        self._set_view(self.active_view)
        self.action_refresh_data()

    def _live_refresh(self) -> None:
        if self.live:
            self.action_refresh_data(quiet=True)

    def action_refresh_data(self, quiet: bool = False) -> None:
        if self.active_view == "home":
            self._refresh_projects(quiet=quiet)
            return
        if self.active_view == "trace-detail":
            if self.selected_trace_id:
                self._render_trace_detail(self.selected_trace_id)
                if not quiet:
                    self.notify("trace refreshed", timeout=1.0)
            return
        if self.active_view == "span-detail":
            if self.selected_trace_id and self.selected_span_id:
                self._render_span_detail(
                    self.selected_trace_id,
                    self.selected_span_id,
                )
                if not quiet:
                    self.notify("span refreshed", timeout=1.0)
            return
        try:
            self.snapshot = self.store.snapshot()
        except Exception as error:
            self.notify(str(error), title="database read failed", severity="error")
            return
        self._apply_filter(self.query_one("#search", Input).value)
        self._update_live_label()
        if not quiet:
            self.notify("telemetry refreshed", timeout=1.0)

    def action_toggle_live(self) -> None:
        if self.active_view != "dashboard":
            return
        self.live = not self.live
        self._update_live_label()

    def action_focus_search(self) -> None:
        if self.active_view != "dashboard":
            return
        self.query_one("#search", Input).focus()

    def _show_span_panel(self, pane_id: str, log_id: str) -> None:
        if self.active_view != "span-detail":
            return
        self.query_one("#span-detail-body", TabbedContent).active = pane_id
        self.call_after_refresh(self.query_one(log_id, RichLog).focus)

    def action_show_span_input(self) -> None:
        self._show_span_panel("span-call-tab", "#span-input")

    def action_show_span_output(self) -> None:
        self._show_span_panel("span-call-tab", "#span-output")

    def action_show_span_metadata(self) -> None:
        self._show_span_panel("span-metadata-tab", "#span-metadata")

    def action_show_span_events(self) -> None:
        self._show_span_panel("span-events-tab", "#span-events")

    def action_go_back(self) -> None:
        if self.active_view == "span-detail":
            self.active_view = "trace-detail"
            self._set_view("trace-detail")
            return
        if self.active_view == "trace-detail":
            self.active_view = "dashboard"
            self._set_view("dashboard")
            return
        self.action_go_home()

    def action_go_home(self) -> None:
        if self.active_view == "home":
            return
        self.store.project = None
        self.selected_trace_id = None
        self.selected_span_id = None
        self.active_view = "home"
        self._set_view("home")
        self._refresh_projects()

    @on(Input.Changed, "#search")
    def filter_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(DataTable.RowHighlighted, "#project-table")
    def project_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.selected_project = str(event.row_key.value)
        self._update_home_metrics()

    @on(DataTable.RowSelected, "#project-table")
    def project_selected(self, event: DataTable.RowSelected) -> None:
        self._open_project(str(event.row_key.value))

    @on(DataTable.RowHighlighted, "#trace-table")
    def trace_highlighted(self, event: DataTable.RowHighlighted) -> None:
        trace_id = str(event.row_key.value)
        if trace_id and trace_id != self.selected_trace_id:
            self.selected_trace_id = trace_id
            self._render_trace(trace_id)

    @on(DataTable.RowSelected, "#trace-table")
    def trace_selected(self, event: DataTable.RowSelected) -> None:
        self._open_trace(str(event.row_key.value))

    @on(DataTable.RowHighlighted, "#execution-flow-table")
    def span_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.selected_span_id = str(event.row_key.value)

    @on(DataTable.RowSelected, "#execution-flow-table")
    def span_selected(self, event: DataTable.RowSelected) -> None:
        self._open_span(str(event.row_key.value))

    @on(DitherButton.Pressed, "#refresh-button")
    def refresh_pressed(self) -> None:
        self.action_refresh_data()

    @on(DitherButton.Pressed, "#home-refresh-button")
    def home_refresh_pressed(self) -> None:
        self.action_refresh_data()

    @on(DitherButton.Pressed, "#open-project-button")
    def open_project_pressed(self) -> None:
        if self.selected_project:
            self._open_project(self.selected_project)

    @on(DitherButton.Pressed, "#open-trace-button")
    def open_trace_pressed(self) -> None:
        if self.selected_trace_id:
            self._open_trace(self.selected_trace_id)

    @on(DitherButton.Pressed, "#live-button")
    def live_pressed(self) -> None:
        self.action_toggle_live()

    @on(DitherButton.Pressed, "#quit-button")
    def quit_pressed(self) -> None:
        self.exit()

    @on(DitherButton.Pressed, "#home-quit-button")
    def home_quit_pressed(self) -> None:
        self.exit()

    @on(DitherButton.Pressed, "#projects-button")
    def projects_pressed(self) -> None:
        self.action_go_home()

    @on(DitherButton.Pressed, "#detail-back-button")
    def detail_back_pressed(self) -> None:
        self.action_go_back()

    @on(DitherButton.Pressed, "#open-span-button")
    def open_span_pressed(self) -> None:
        if self.selected_span_id:
            self._open_span(self.selected_span_id)

    @on(DitherButton.Pressed, "#detail-refresh-button")
    def detail_refresh_pressed(self) -> None:
        self.action_refresh_data()

    @on(DitherButton.Pressed, "#detail-quit-button")
    def detail_quit_pressed(self) -> None:
        self.exit()

    @on(DitherButton.Pressed, "#span-back-button")
    def span_back_pressed(self) -> None:
        self.action_go_back()

    @on(DitherButton.Pressed, "#span-refresh-button")
    def span_refresh_pressed(self) -> None:
        self.action_refresh_data()

    @on(DitherButton.Pressed, "#span-quit-button")
    def span_quit_pressed(self) -> None:
        self.exit()

    def _refresh_projects(self, quiet: bool = False) -> None:
        try:
            self.project_snapshot = self.store.projects()
        except Exception as error:
            self.notify(str(error), title="database read failed", severity="error")
            return
        self._render_project_table()
        self._update_home_metrics()
        if not quiet:
            self.notify("project index refreshed", timeout=1.0)

    def _update_home_metrics(self) -> None:
        project = next(
            (
                row
                for row in self.project_snapshot.projects
                if row.name == self.selected_project
            ),
            None,
        )
        if project is None:
            for card_id in (
                "#home-metric-projects",
                "#home-metric-traces",
                "#home-metric-sessions",
                "#home-metric-errors",
                "#home-metric-cost",
            ):
                self.query_one(card_id, MetricCard).set_value("—")
            return

        self.query_one("#home-metric-projects", MetricCard).set_value(
            f"{project.name} / {_project_health_name(project)}"
        )
        self.query_one("#home-metric-traces", MetricCard).set_value(
            f"{project.trace_count} traces / {project.session_count} sessions"
        )
        self.query_one("#home-metric-sessions", MetricCard).set_value(
            f"{(1.0 - project.error_rate) * 100:.1f}% / "
            f"{project.error_count} errors"
        )
        self.query_one("#home-metric-errors", MetricCard).set_value(
            f"p50 {_duration(project.p50_latency_ms)} / "
            f"p95 {_duration(project.p95_latency_ms)}"
        )
        self.query_one("#home-metric-cost", MetricCard).set_value(
            f"{_cost(project.total_cost_usd)} / "
            f"{_compact_number(project.total_tokens)} tok"
        )

    def _render_project_table(self) -> None:
        table = self.query_one("#project-table", DataTable)
        previous = self.selected_project
        previous_scroll = table.scroll_offset
        table.clear(columns=False)
        project_names = {project.name for project in self.project_snapshot.projects}
        for project in self.project_snapshot.projects:
            table.add_row(
                _project_health(project),
                gradient_text(project.name, bold=True),
                str(project.trace_count),
                str(project.session_count),
                f"{project.error_count}  /  {project.error_rate * 100:.1f}%",
                _duration(project.p95_latency_ms),
                _cost(project.total_cost_usd),
                _compact_number(project.total_tokens),
                _started_at(project.last_activity),
                key=project.name,
            )
        if previous in project_names:
            self.selected_project = previous
            table.move_cursor(
                row=table.get_row_index(previous),
                animate=False,
                scroll=False,
            )
            self.call_after_refresh(
                self._restore_scroll,
                table,
                previous_scroll.x,
                previous_scroll.y,
            )
        elif self.project_snapshot.projects:
            self.selected_project = self.project_snapshot.projects[0].name
        else:
            self.selected_project = None
        table = self.query_one("#project-table", ProjectIndexTable)
        self.call_after_refresh(table.fit_columns)

    def _open_project(self, project: str) -> None:
        if project not in {row.name for row in self.project_snapshot.projects}:
            return
        self.selected_project = project
        self.store.project = project
        self.selected_trace_id = None
        self.selected_span_id = None
        search = self.query_one("#search", Input)
        search.value = ""
        self.active_view = "dashboard"
        self.query_one("#project-label", Label).update(f"project // {project}")
        self._set_view("dashboard")
        self.action_refresh_data()

    def _open_trace(self, trace_id: str) -> None:
        if trace_id not in self._trace_keys:
            return
        self.selected_trace_id = trace_id
        self.selected_span_id = None
        self.active_view = "trace-detail"
        self._set_view("trace-detail")
        self.call_after_refresh(self._render_visible_trace_detail, trace_id)

    def _open_span(self, span_id: str) -> None:
        if span_id not in self._span_keys or not self.selected_trace_id:
            return
        self.selected_span_id = span_id
        self.active_view = "span-detail"
        self._set_view("span-detail")
        self.call_after_refresh(
            self._render_visible_span_detail,
            self.selected_trace_id,
            span_id,
        )

    def _render_visible_trace_detail(self, trace_id: str) -> None:
        """Populate the trace view after its panels have their final width."""
        if self.active_view == "trace-detail" and self.selected_trace_id == trace_id:
            self._render_trace_detail(trace_id)

    def _render_visible_span_detail(self, trace_id: str, span_id: str) -> None:
        """Populate the span view after its panels have their final width."""
        if (
            self.active_view == "span-detail"
            and self.selected_trace_id == trace_id
            and self.selected_span_id == span_id
        ):
            self._render_span_detail(trace_id, span_id)

    def _set_view(self, view: str) -> None:
        home = self.query_one("#home", Vertical)
        dashboard = self.query_one("#dashboard", Vertical)
        trace_detail = self.query_one("#trace-detail-view", Vertical)
        span_detail = self.query_one("#span-detail-view", Vertical)
        home.display = view == "home"
        dashboard.display = view == "dashboard"
        trace_detail.display = view == "trace-detail"
        span_detail.display = view == "span-detail"
        if view == "home":
            self.query_one("#project-table", DataTable).focus()
        elif view == "dashboard":
            project = self.store.project or "all projects"
            self.query_one("#project-label", Label).update(f"project // {project}")
            self.query_one("#trace-table", DataTable).focus()
        elif view == "trace-detail":
            self.query_one("#execution-flow-table", DataTable).focus()
        else:
            tabs = self.query_one("#span-detail-body", TabbedContent)
            tabs.active = "span-call-tab"
            self.query_one("#span-output", RichLog).focus()

    def _update_trace_metrics(self, detail: TraceDetail | None) -> None:
        if detail is None:
            for card_id in (
                "#metric-traces",
                "#metric-sessions",
                "#metric-errors",
                "#metric-latency",
                "#metric-cost",
            ):
                self.query_one(card_id, MetricCard).set_value("—")
            return

        trace = detail.trace
        metrics = detail.metrics
        status = str(trace.get("status") or "unknown").lower()
        started = _started_at(trace.get("started_at"))
        if len(started) >= 14 and started[-3] == ":":
            started = started[:-3]
        self.query_one("#metric-traces", MetricCard).set_value(
            f"{_short_id(trace.get('name') or 'trace', 18)} / {status}"
        )
        self.query_one("#metric-sessions", MetricCard).set_value(
            f"{started} / {_duration(float(metrics.get('latency_ms', 0)))}"
        )
        self.query_one("#metric-errors", MetricCard).set_value(
            f"{len(detail.spans)} spans / {len(detail.events)} events"
        )
        self.query_one("#metric-latency", MetricCard).set_value(
            f"{_compact_number(metrics.get('llm_calls', 0))} llm / "
            f"{_compact_number(metrics.get('tool_calls', 0))} tools"
        )
        tokens = float(metrics.get("input_tokens", 0)) + float(
            metrics.get("output_tokens", 0)
        )
        self.query_one("#metric-cost", MetricCard).set_value(
            f"{_cost(float(metrics.get('cost_usd', 0)))} / "
            f"{_compact_number(tokens)} tok"
        )

    def _apply_filter(self, value: str) -> None:
        query = value.strip().lower()
        if not query:
            self.filtered_traces = self.snapshot.traces
        else:
            self.filtered_traces = [
                trace
                for trace in self.snapshot.traces
                if query
                in " ".join(
                    (
                        trace.id,
                        trace.name,
                        trace.project,
                        trace.status,
                        trace.session_id or "",
                    )
                ).lower()
            ]
        self._render_trace_table()

    def _render_trace_table(self) -> None:
        table = self.query_one("#trace-table", DataTable)
        previous = self.selected_trace_id
        previous_scroll = table.scroll_offset
        table.clear(columns=False)
        self._trace_keys.clear()
        for trace in self.filtered_traces:
            table.add_row(
                _status(trace.status),
                trace.name,
                trace.project,
                _short_id(trace.session_id, 18),
                str(trace.span_count),
                _duration(trace.latency_ms),
                _cost(trace.cost_usd),
                _started_at(trace.started_at),
                key=trace.id,
            )
            self._trace_keys.add(trace.id)
        if previous in self._trace_keys:
            # DataTable.clear() resets its cursor and both scroll axes. Restore the
            # selection without pulling it into view, then restore the viewport so
            # a live refresh cannot move a list the user is actively reading.
            table.move_cursor(
                row=table.get_row_index(previous),
                animate=False,
                scroll=False,
            )
            self.call_after_refresh(
                self._restore_scroll,
                table,
                previous_scroll.x,
                previous_scroll.y,
            )
            self._render_trace(previous)
        elif self.filtered_traces:
            self.selected_trace_id = self.filtered_traces[0].id
            self._render_trace(self.selected_trace_id)
        else:
            self.selected_trace_id = None
            self._update_trace_metrics(None)
            self.query_one("#trace-meta", Static).update(
                Text("no matching traces", style="#8d5426")
            )
            self.query_one("#events", RichLog).clear()

    def _render_trace(self, trace_id: str) -> None:
        try:
            detail = self.store.trace_detail(trace_id)
        except Exception as error:
            self.notify(str(error), severity="error")
            return
        if detail is None:
            self._update_trace_metrics(None)
            return
        self._update_trace_metrics(detail)
        self.query_one("#trace-meta", Static).update(
            self._trace_meta_content(detail)
        )
        self._render_events_to(detail, self.query_one("#events", RichLog))

    def _render_trace_detail(self, trace_id: str) -> None:
        try:
            detail = self.store.trace_detail(trace_id)
        except Exception as error:
            self.notify(str(error), severity="error")
            return
        if detail is None:
            self.notify("trace no longer exists", severity="warning")
            self.action_go_back()
            return

        trace = detail.trace
        title = Text()
        title.append(" TRACE // ", style="#8d5426")
        title.append(str(trace.get("name") or trace_id), style="bold #ffb04a")
        title.append("  ")
        title.append(_short_id(trace_id, 42), style="#a8642d")
        self.query_one("#detail-title", Static).update(title)
        self.query_one("#detail-meta", Static).update(
            self._trace_meta_content(detail, include_io=False)
        )
        self._render_execution_flow(detail)

    def _render_execution_flow(self, detail: TraceDetail) -> None:
        table = self.query_one("#execution-flow-table", ExecutionFlowTable)
        previous = self.selected_span_id
        previous_scroll = table.scroll_offset
        table.clear(columns=False)
        self._span_keys.clear()
        origin = _parse_timestamp(detail.trace.get("started_at"))
        spans_by_id = {
            str(span.get("id")): span
            for span in detail.spans
            if span.get("id")
        }
        for index, span in enumerate(detail.spans):
            span_id = str(span.get("id") or "")
            started = _parse_timestamp(span.get("started_at"))
            elapsed = 0.0
            if origin is not None and started is not None:
                elapsed = max(0.0, (started - origin).total_seconds())
            kind = str(span.get("kind") or "custom").upper()
            symbol, kind_style = (
                ("◆", "bold #ffb04a")
                if kind == "LLM"
                else ("■", "bold #ff7a18")
                if kind == "TOOL"
                else ("●", "bold #c76a20")
            )
            kind_cell = Text(f"{symbol} {kind}", style=kind_style)
            depth = _span_depth(span, spans_by_id)
            operation = Text()
            if depth:
                operation.append(
                    f"{'  ' * (depth - 1)}↳ ",
                    style="#6f3e1c",
                )
            operation.append(
                str(span.get("name") or span_id or "span"),
                style="#d28a48",
            )
            table.add_row(
                operation,
                kind_cell,
                f"+{elapsed:7.3f}s",
                _duration(float(span.get("latency_ms") or 0)),
                _status(str(span.get("status") or "")),
                _span_request_summary(span),
                _span_result_summary(span, detail.spans[index + 1 :]),
                key=span_id,
            )
            self._span_keys.add(span_id)
        table.fit_columns()
        if previous in self._span_keys:
            self.selected_span_id = previous
            table.move_cursor(
                row=table.get_row_index(previous),
                animate=False,
                scroll=False,
            )
            self.call_after_refresh(
                self._restore_scroll,
                table,
                previous_scroll.x,
                previous_scroll.y,
            )
        elif detail.spans:
            self.selected_span_id = str(detail.spans[0].get("id") or "")
        else:
            self.selected_span_id = None

    def _render_span_detail(self, trace_id: str, span_id: str) -> None:
        try:
            detail = self.store.trace_detail(trace_id)
        except Exception as error:
            self.notify(str(error), severity="error")
            return
        if detail is None:
            self.notify("trace no longer exists", severity="warning")
            self.action_go_back()
            return
        span = next(
            (
                candidate
                for candidate in detail.spans
                if str(candidate.get("id") or "") == span_id
            ),
            None,
        )
        if span is None:
            self.notify("span no longer exists", severity="warning")
            self.action_go_back()
            return
        span_index = detail.spans.index(span)

        kind = str(span.get("kind") or "custom").upper()
        name = str(span.get("name") or span_id)
        title = Text()
        title.append(f" {kind} SPAN // ", style="#8d5426")
        title.append(name, style="bold #ffb04a")
        title.append("  ")
        title.append(_short_id(span_id, 42), style="#a8642d")
        self.query_one("#span-detail-title", Static).update(title)
        tabs = self.query_one("#span-detail-body", TabbedContent)
        tabs.get_tab("span-call-tab").label = "I/O"
        self.query_one("#span-input-title", Label).update(" INPUT")
        self.query_one("#span-output-title", Label).update(" OUTPUT")
        self.query_one("#span-detail-meta", Static).update(
            self._span_meta_content(span)
        )
        previous_llm_span = next(
            (
                candidate
                for candidate in reversed(detail.spans[:span_index])
                if str(candidate.get("kind") or "").lower() == "llm"
            ),
            None,
        )
        self._write_value(
            self.query_one("#span-input", RichLog),
            _format_span_input(
                span,
                previous_llm_span=previous_llm_span,
                trace_input=detail.trace.get("input"),
                show_system_prompt=span_id
                == next(
                    (
                        str(candidate.get("id") or "")
                        for candidate in detail.spans
                        if str(candidate.get("kind") or "").lower() == "llm"
                    ),
                    None,
                ),
            ),
            style="#d28a48",
        )
        self._write_value(
            self.query_one("#span-output", RichLog),
            _format_span_output(span, detail.spans[span_index + 1 :]),
            style="#b96f34",
        )
        context: dict[str, object] = {
            "metadata": span.get("metadata") or {},
        }
        if span.get("error") is not None:
            context["error"] = span.get("error")
        self._write_value(
            self.query_one("#span-metadata", RichLog),
            context,
            style="#c78343",
        )
        span_events = [
            event
            for event in detail.events
            if str(event.get("span_id") or "") == span_id
        ]
        self._render_span_events(
            span,
            span_events,
            detail.spans[span_index + 1 :],
        )

    def _span_meta_content(self, span: dict[str, Any]) -> Group:
        heading = Text()
        heading.append(
            str(span.get("name") or span.get("id") or "span"),
            style="bold #ffb04a",
        )
        heading.append("  ")
        heading.append_text(_status(str(span.get("status") or "")))

        identity = Text()
        identity.append(_short_id(span.get("id"), 38), style="#8d5426")
        identity.append("  trace ", style="#6f3e1c")
        identity.append(_short_id(span.get("trace_id"), 22), style="#a8642d")
        identity.append("  parent ", style="#6f3e1c")
        identity.append(_short_id(span.get("parent_span_id"), 18), style="#a8642d")

        facts = Table.grid(expand=True, padding=(0, 1))
        facts.add_column(ratio=1)
        facts.add_column(ratio=1)
        facts.add_column(ratio=1)
        facts.add_row(
            _metric(
                "duration",
                _duration(float(span.get("latency_ms") or 0)),
            ),
            _metric("started", _started_at(span.get("started_at"))),
            _metric("ended", _started_at(span.get("ended_at"))),
        )

        metadata = span.get("metadata")
        provider = model = operation = "—"
        if isinstance(metadata, dict):
            provider = str(metadata.get("provider") or "—")
            model = _short_id(metadata.get("model"), 34)
            operation = str(metadata.get("operation") or span.get("name") or "—")
        context = Text()
        context.append("PROVIDER ", style="#6f3e1c")
        context.append(provider, style="#c78343")
        context.append("  MODEL ", style="#6f3e1c")
        context.append(model, style="#c78343")
        context.append("  OPERATION ", style="#6f3e1c")
        context.append(operation, style="#c78343")
        return Group(heading, identity, facts, context)

    def _render_span_events(
        self,
        span: dict[str, Any],
        events: list[dict[str, Any]],
        following_spans: list[dict[str, Any]],
    ) -> None:
        log = self.query_one("#span-events", RichLog)
        previous_scroll = log.scroll_offset
        log.clear()
        for label, value in (
            ("RAW INPUT", span.get("input")),
            ("RAW OUTPUT", _resolved_llm_output(span, following_spans)),
        ):
            log.write(Text(label, style="bold #ffb04a"))
            log.write(Text(_format_value(value), style="#a8642d"))
            log.write(Text(""))

        log.write(Text("RECORDED EVENTS", style="bold #ffb04a"))
        log.write(Text(""))
        origin = _parse_timestamp(span.get("started_at"))
        for event in events:
            timestamp = _parse_timestamp(event.get("timestamp"))
            elapsed = 0.0
            if origin is not None and timestamp is not None:
                elapsed = max(0.0, (timestamp - origin).total_seconds())
            event_type = str(event.get("type") or "event")
            symbol, style = _event_style(event_type)
            heading = Text()
            heading.append(f"+{elapsed:7.3f}s  ", style="#6f3e1c")
            heading.append(f"{symbol} {event_type}", style=style)
            log.write(heading)
            log.write(
                Text(
                    _format_value(event.get("payload")),
                    style="#a8642d",
                )
            )
            log.write(Text(""))
        if not events:
            log.write(Text("no events recorded for this span", style="#6f3e1c"))
        self.call_after_refresh(
            self._restore_scroll,
            log,
            previous_scroll.x,
            previous_scroll.y,
        )

    def _write_value(self, log: RichLog, value: object, *, style: str) -> None:
        previous_scroll = log.scroll_offset
        log.clear()
        log.write(Text(_format_value(value), style=style))
        self.call_after_refresh(
            self._restore_scroll,
            log,
            previous_scroll.x,
            previous_scroll.y,
        )

    @staticmethod
    def _restore_scroll(
        widget: RichLog | DataTable[object],
        x: int,
        y: int,
    ) -> None:
        """Restore a viewport after live refresh rebuilt its content."""
        widget.scroll_to(x=x, y=y, animate=False, force=True)

    def _trace_meta_content(
        self,
        detail: TraceDetail,
        *,
        preview_limit: int = 100,
        include_io: bool = True,
    ) -> Group:
        trace = detail.trace
        metrics = detail.metrics
        heading = Text()
        heading.append(
            str(trace.get("name") or trace["id"]),
            style="bold #ffb04a",
        )
        heading.append("  ")
        heading.append_text(_status(str(trace.get("status", ""))))

        identity = Text()
        identity.append(_short_id(trace.get("id"), 36), style="#8d5426")
        identity.append("  ")
        identity.append(str(trace.get("project") or "—"), style="#c78343")
        identity.append("  session ", style="#6f3e1c")
        identity.append(_short_id(trace.get("session_id"), 18), style="#a8642d")

        tokens = float(metrics.get("input_tokens", 0)) + float(
            metrics.get("output_tokens", 0)
        )
        facts = Table.grid(expand=True, padding=(0, 1))
        facts.add_column(ratio=1)
        facts.add_column(ratio=1)
        facts.add_column(ratio=1)
        facts.add_row(
            _metric("duration", _duration(float(metrics.get("latency_ms", 0)))),
            _metric("spans", str(len(detail.spans))),
            _metric("events", str(len(detail.events))),
        )
        facts.add_row(
            _metric("cost", _cost(float(metrics.get("cost_usd", 0)))),
            _metric("tokens", _compact_number(tokens)),
            _metric(
                "llm/tools",
                f"{_compact_number(metrics.get('llm_calls', 0))}/"
                f"{_compact_number(metrics.get('tool_calls', 0))}",
            ),
        )

        trace_input = Text()
        trace_input.append("IN   ", style="bold #6f3e1c")
        trace_input.append(
            _preview(trace.get("input"), preview_limit),
            style="#d28a48",
        )
        trace_output = Text()
        trace_output.append("OUT  ", style="bold #6f3e1c")
        trace_output.append(
            _preview(trace.get("output"), preview_limit),
            style="#b96f34",
        )
        if include_io:
            return Group(heading, identity, facts, trace_input, trace_output)
        return Group(heading, identity, facts)

    def _render_events_to(
        self,
        detail: TraceDetail,
        log: RichLog,
        *,
        limit: int = 80,
    ) -> None:
        previous_scroll = log.scroll_offset
        log.clear()
        origin = _parse_timestamp(detail.trace.get("started_at"))
        for event in detail.events[-limit:]:
            event_type = str(event.get("type") or "event")
            timestamp = _parse_timestamp(event.get("timestamp"))
            elapsed = 0.0
            if origin is not None and timestamp is not None:
                elapsed = max(0.0, (timestamp - origin).total_seconds())
            symbol, style = _event_style(event_type)
            row = Text()
            row.append(f"+{elapsed:7.3f}s  ", style="#6f3e1c")
            row.append(f"{symbol} {event_type:<14}", style=style)
            row.append(_event_detail(event_type, event.get("payload")), style="#a8642d")
            log.write(row)
        if not detail.events:
            log.write(Text("no events recorded", style="#6f3e1c"))
        self.call_after_refresh(
            self._restore_scroll,
            log,
            previous_scroll.x,
            previous_scroll.y,
        )

    def _update_live_label(self) -> None:
        state = "◉ LIVE  auto-refresh" if self.live else "○ PAUSED"
        color = "#ff9d42" if self.live else "#6f3e1c"
        self.query_one("#live-status", Static).update(Text(state, style=color))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evalon",
        description="Read-only terminal observability for local Evalon traces.",
    )
    parser.add_argument(
        "database",
        nargs="?",
        default=default_db_path(),
        help="Evalon SQLite database (default: EVALON_DB or ~/.evalon/evalon-runs.sqlite)",
    )
    parser.add_argument("--project", help="show one project only")
    parser.add_argument(
        "--refresh",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="live refresh interval (default: 2)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        store = ObservabilityStore(Path(args.database), project=args.project)
    except (FileNotFoundError, ValueError, OSError) as error:
        raise SystemExit(f"evalon: {error}") from error
    EvalonApp(store, refresh_seconds=args.refresh).run()
