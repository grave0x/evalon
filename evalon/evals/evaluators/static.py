"""Built-in deterministic evaluators over canonical evaluation context data."""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any

from jsonschema import Draft202012Validator

from evalon.core.json import sanitize
from evalon.evals.contracts import (
    EvaluationContext,
    EvaluationResult,
    EvaluatorDefinition,
    EvaluatorType,
)
from evalon.evals.evaluators.registry import (
    StaticEvaluator,
    get_static_evaluator,
    register_static_evaluator,
)


def _config(config: dict[str, Any], key: str) -> Any:
    if key not in config:
        raise ValueError(f"Static evaluator configuration requires {key!r}")
    return config[key]


def _validate_keys(
    config: dict[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    optional = optional or set()
    unknown = set(config) - required - optional - {"type"}
    if unknown:
        raise ValueError(
            f"Unknown static evaluator configuration field(s): {', '.join(sorted(unknown))}"
        )
    missing = required - set(config)
    if missing:
        raise ValueError(
            f"Missing static evaluator configuration field(s): {', '.join(sorted(missing))}"
        )


def _bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"Static evaluator configuration {key!r} must be boolean")
    return value


def _number(config: dict[str, Any], key: str) -> float:
    value = _config(config, key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"Static evaluator configuration {key!r} must be numeric")
    return float(value)


def _normalise(value: Any, *, strip: bool, case_sensitive: bool) -> Any:
    if not isinstance(value, str):
        return value
    if strip:
        value = value.strip()
    return value if case_sensitive else value.casefold()


def _expected(config: dict[str, Any], context: EvaluationContext) -> Any:
    return (
        context.expected_output
        if config.get("expected", "context") == "context"
        else config["expected"]
    )


def _result(
    definition: EvaluatorDefinition,
    *,
    passed: bool | None,
    label: str,
    reason: str,
    evidence: list[Any],
    score: float | None = None,
    error: dict[str, Any] | None = None,
    duration_ms: float | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        evaluator_id=definition.id,
        evaluator_name=definition.name,
        evaluator_version=definition.version,
        evaluator_type=EvaluatorType.STATIC,
        required=definition.required,
        passed=passed,
        score=(
            (definition.score_max if passed else definition.score_min)
            if score is None and passed is not None
            else score
        ),
        label=label,
        reason=reason,
        evidence=sanitize(evidence),
        evaluator_error=error,
        duration_ms=duration_ms,
    )


def _tool_calls(context: EvaluationContext) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = [
        dict(item) for item in context.tool_calls if isinstance(item, dict)
    ]
    span_ids = {call.get("span_id") for call in calls if call.get("span_id")}
    for span in context.spans:
        if not isinstance(span, dict) or span.get("kind") != "tool":
            continue
        if span.get("id") in span_ids:
            continue
        metadata = (
            span.get("metadata") if isinstance(span.get("metadata"), dict) else {}
        )
        calls.append(
            {
                "span_id": span.get("id"),
                "name": metadata.get("name") or span.get("name"),
                "arguments": span.get("input"),
                "output": span.get("output"),
                "status": span.get("status"),
                "latency_ms": span.get("latency_ms"),
            }
        )
    return calls


def _match_tools(context: EvaluationContext, name: str) -> list[dict[str, Any]]:
    return [call for call in _tool_calls(context) if call.get("name") == name]


def _json_path(value: Any, path: str) -> Any:
    if not isinstance(path, str) or not path.startswith("$"):
        raise ValueError("JSON path must start with '$'")
    tokens = re.findall(
        r"(?:^\$)|(?:\.([A-Za-z_][A-Za-z0-9_]*))|(?:\[([0-9]+)\])", path
    )
    reconstructed = "$" + "".join(
        f".{name}" if name else f"[{index}]" for name, index in tokens[1:]
    )
    if reconstructed != path:
        raise ValueError(f"Unsupported JSON path: {path!r}")
    current = value
    for name, index in tokens[1:]:
        if name:
            if not isinstance(current, dict) or name not in current:
                return _MISSING
            current = current[name]
        else:
            if not isinstance(current, list) or int(index) >= len(current):
                return _MISSING
            current = current[int(index)]
    return current


_MISSING = object()


def _validate_exact(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type"}, {"expected", "strip", "case_sensitive"})
    _bool(config, "strip")
    _bool(config, "case_sensitive", True)


def _exact(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    c = definition.configuration
    expected = _expected(c, context)
    actual = _normalise(
        context.candidate_output,
        strip=_bool(c, "strip"),
        case_sensitive=_bool(c, "case_sensitive", True),
    )
    wanted = _normalise(
        expected,
        strip=_bool(c, "strip"),
        case_sensitive=_bool(c, "case_sensitive", True),
    )
    ok = actual == wanted
    return _result(
        definition,
        passed=ok,
        label="exact_match",
        reason="Candidate output exactly matched expected value"
        if ok
        else "Candidate output did not exactly match expected value",
        evidence=[{"actual": context.candidate_output, "expected": expected}],
    )


def _validate_contains(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type"}, {"expected", "case_sensitive"})
    _bool(config, "case_sensitive", True)


def _contains(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    c = definition.configuration
    expected = _expected(c, context)
    if not isinstance(context.candidate_output, str) or not isinstance(expected, str):
        return _result(
            definition,
            passed=False,
            label="contains",
            reason="Contains requires string candidate and expected values",
            evidence=[{"actual": context.candidate_output, "expected": expected}],
        )
    actual = _normalise(
        context.candidate_output,
        strip=False,
        case_sensitive=_bool(c, "case_sensitive", True),
    )
    wanted = _normalise(
        expected, strip=False, case_sensitive=_bool(c, "case_sensitive", True)
    )
    ok = wanted in actual
    return _result(
        definition,
        passed=ok,
        label="contains",
        reason="Candidate output contained expected text"
        if ok
        else "Candidate output did not contain expected text",
        evidence=[{"actual": context.candidate_output, "expected": expected}],
    )


def _excludes(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    expected = _expected(definition.configuration, context)
    if not isinstance(context.candidate_output, str) or not isinstance(expected, str):
        return _result(
            definition,
            passed=False,
            label="excludes",
            reason="Excludes requires string candidate and forbidden values",
            evidence=[{"actual": context.candidate_output, "expected": expected}],
        )
    result = _contains(definition, context)
    result.passed = not bool(result.passed)
    result.score = float(result.passed)
    result.label = "excludes"
    result.reason = (
        "Candidate output excluded forbidden text"
        if result.passed
        else "Candidate output contained forbidden text"
    )
    return result


def _validate_regex(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type", "pattern"}, {"flags"})
    if not isinstance(config["pattern"], str):
        raise TypeError("Regex pattern must be a string")
    flags = config.get("flags", "")
    if not isinstance(flags, str) or set(flags) - {"i", "m", "s"}:
        raise ValueError("Regex flags may only contain i, m, and s")
    re.compile(
        config["pattern"],
        sum(
            {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}[flag]
            for flag in flags
        ),
    )


def _regex(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    c = definition.configuration
    flags = sum(
        {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}[f]
        for f in c.get("flags", "")
    )
    actual = context.candidate_output
    match = re.search(c["pattern"], actual, flags) if isinstance(actual, str) else None
    return _result(
        definition,
        passed=match is not None,
        label="regex_match",
        reason="Candidate output matched regex"
        if match
        else "Candidate output did not match regex",
        evidence=[
            {
                "pattern": c["pattern"],
                "actual": actual,
                "match": match.group(0) if match else None,
            }
        ],
    )


def _validate_json_schema(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type", "schema"})
    if not isinstance(config["schema"], dict):
        raise TypeError("JSON schema must be an object")
    Draft202012Validator.check_schema(config["schema"])


def _json_schema(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    value = context.candidate_output
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            return _result(
                definition,
                passed=False,
                label="json_schema",
                reason="Candidate output was not valid JSON",
                evidence=[{"actual": context.candidate_output, "error": str(exc)}],
            )
    errors = [
        {
            "path": "$"
            + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}"
                for part in error.absolute_path
            ),
            "message": error.message,
            "validator": error.validator,
        }
        for error in sorted(
            Draft202012Validator(definition.configuration["schema"]).iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
    ]
    return _result(
        definition,
        passed=not errors,
        label="json_schema",
        reason="Candidate JSON matched schema"
        if not errors
        else "Candidate JSON failed schema validation",
        evidence=[{"actual": value, "errors": errors}],
    )


def _validate_json_path(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type", "path"}, {"expected"})
    _json_path({}, config["path"])


def _json_path_equal(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    value = context.candidate_output
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = _MISSING
    actual = (
        _json_path(value, definition.configuration["path"])
        if value is not _MISSING
        else _MISSING
    )
    expected = _expected(definition.configuration, context)
    ok = actual is not _MISSING and actual == expected
    return _result(
        definition,
        passed=ok,
        label="json_path_equal",
        reason="JSON path matched expected value"
        if ok
        else "JSON path did not match expected value",
        evidence=[
            {
                "path": definition.configuration["path"],
                "actual": None if actual is _MISSING else actual,
                "expected": expected,
                "found": actual is not _MISSING,
            }
        ],
    )


def _validate_tool_name(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type", "tool"})
    if not isinstance(config["tool"], str) or not config["tool"].strip():
        raise ValueError("Tool name must be a non-empty string")


def _required_tool(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    tool = definition.configuration["tool"]
    calls = _match_tools(context, tool)
    return _result(
        definition,
        passed=bool(calls),
        label="required_tool_call",
        reason="Required tool was called" if calls else "Required tool was not called",
        evidence=calls or [{"tool": tool, "calls": []}],
    )


def _forbidden_tool(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    tool = definition.configuration["tool"]
    calls = _match_tools(context, tool)
    return _result(
        definition,
        passed=not calls,
        label="forbidden_tool_call",
        reason="Forbidden tool was not called"
        if not calls
        else "Forbidden tool was called",
        evidence=calls or [{"tool": tool, "calls": []}],
    )


def _validate_tool_order(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type", "tools"})
    if (
        not isinstance(config["tools"], list)
        or not config["tools"]
        or any(not isinstance(item, str) or not item for item in config["tools"])
    ):
        raise ValueError("Tool order requires a non-empty list of tool names")


def _tool_order(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    expected = definition.configuration["tools"]
    calls = _tool_calls(context)
    names = [call.get("name") for call in calls]
    position = 0
    for name in names:
        if position < len(expected) and name == expected[position]:
            position += 1
    ok = position == len(expected)
    return _result(
        definition,
        passed=ok,
        label="tool_call_order",
        reason="Tools were called in required order"
        if ok
        else "Tools were not called in required order",
        evidence=[{"expected_order": expected, "actual_calls": calls}],
    )


def _validate_tool_args(config: dict[str, Any]) -> None:
    _validate_keys(config, {"type", "tool", "arguments"})
    if not isinstance(config["tool"], str) or not isinstance(config["arguments"], dict):
        raise TypeError(
            "Tool argument validation requires a tool name and object arguments"
        )


def _tool_arguments(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    c = definition.configuration
    calls = _match_tools(context, c["tool"])
    expected = c["arguments"]
    matches = [
        call
        for call in calls
        if isinstance(call.get("arguments"), dict)
        and all(
            call["arguments"].get(key, _MISSING) == value
            for key, value in expected.items()
        )
    ]
    return _result(
        definition,
        passed=bool(matches),
        label="tool_argument_validation",
        reason="A tool call matched required arguments"
        if matches
        else "No tool call matched required arguments",
        evidence=matches
        or [{"tool": c["tool"], "expected_arguments": expected, "actual_calls": calls}],
    )


def _validate_max(config: dict[str, Any], field: str) -> None:
    _validate_keys(config, {"type", "max"})
    if _number(config, "max") < 0:
        raise ValueError(f"Maximum {field} must be non-negative")


def _maximum_tool_calls(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    calls = _tool_calls(context)
    maximum = _number(definition.configuration, "max")
    return _result(
        definition,
        passed=len(calls) <= maximum,
        label="maximum_tool_calls",
        reason="Tool call count was within maximum"
        if len(calls) <= maximum
        else "Tool call count exceeded maximum",
        evidence=[{"actual": len(calls), "maximum": maximum, "calls": calls}],
    )


def _metric_max(label: str, metric: str):
    def evaluate(
        definition: EvaluatorDefinition, context: EvaluationContext
    ) -> EvaluationResult:
        maximum = _number(definition.configuration, "max")
        actual = context.metrics.get(metric)
        valid = isinstance(actual, int | float) and not isinstance(actual, bool)
        ok = valid and float(actual) <= maximum
        return _result(
            definition,
            passed=ok,
            label=label,
            reason=f"{metric} was within maximum"
            if ok
            else f"{metric} exceeded maximum or was not recorded",
            evidence=[{"metric": metric, "actual": actual, "maximum": maximum}],
        )

    return evaluate


def _execution_without_errors(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    trace_status = (
        (context.candidate_trace or {}).get("status")
        if context.candidate_trace
        else None
    )
    errors = [
        event
        for event in context.events
        if isinstance(event, dict) and str(event.get("type", "")).endswith("error")
    ]
    error_spans = [
        span
        for span in context.spans
        if isinstance(span, dict) and span.get("status") == "error"
    ]
    metric_errors = context.metrics.get("errors", 0)
    ok = (
        trace_status not in {"error", "execution_error"}
        and not errors
        and not error_spans
        and metric_errors == 0
    )
    return _result(
        definition,
        passed=ok,
        label="candidate_execution_without_errors",
        reason="Candidate execution had no recorded errors"
        if ok
        else "Candidate execution recorded errors",
        evidence=[
            {
                "trace_status": trace_status,
                "error_events": errors,
                "error_spans": error_spans,
                "metric_errors": metric_errors,
            }
        ],
    )


def evaluate_static(
    definition: EvaluatorDefinition, context: EvaluationContext
) -> EvaluationResult:
    """Evaluate one validated deterministic evaluator without network access."""
    if definition.evaluator_type is not EvaluatorType.STATIC:
        raise ValueError("evaluate_static requires a static evaluator definition")
    evaluator = get_static_evaluator(definition.configuration.get("type"))
    evaluator.validate(definition.configuration)
    started = perf_counter()
    try:
        result = evaluator.evaluate(definition, context)
    except Exception as exc:  # noqa: BLE001 - evaluator failures become results
        result = _result(
            definition,
            passed=None,
            label="evaluator_error",
            reason="Static evaluator failed",
            evidence=[],
            error={"type": type(exc).__name__, "message": str(exc)},
        )
    result.duration_ms = round((perf_counter() - started) * 1000, 3)
    return result


def _register(name: str, validate, evaluate, description: str) -> None:
    register_static_evaluator(StaticEvaluator(name, evaluate, validate, description))


_register("exact_match", _validate_exact, _exact, "Exact candidate-output match")
_register(
    "contains", _validate_contains, _contains, "Required candidate-output substring"
)
_register(
    "excludes", _validate_contains, _excludes, "Forbidden candidate-output substring"
)
_register(
    "regex_match", _validate_regex, _regex, "Regular-expression candidate-output match"
)
_register(
    "json_schema", _validate_json_schema, _json_schema, "JSON Schema subset validation"
)
_register(
    "json_path_equal", _validate_json_path, _json_path_equal, "JSON path equality"
)
_register(
    "required_tool_call",
    _validate_tool_name,
    _required_tool,
    "Required canonical tool span",
)
_register(
    "forbidden_tool_call",
    _validate_tool_name,
    _forbidden_tool,
    "Forbidden canonical tool span",
)
_register(
    "tool_call_order", _validate_tool_order, _tool_order, "Required tool-call order"
)
_register(
    "tool_argument_validation",
    _validate_tool_args,
    _tool_arguments,
    "Required tool arguments",
)
_register(
    "maximum_tool_calls",
    lambda c: _validate_max(c, "tool calls"),
    _maximum_tool_calls,
    "Maximum tool calls",
)
_register(
    "maximum_latency",
    lambda c: _validate_max(c, "latency"),
    _metric_max("maximum_latency", "latency_ms"),
    "Maximum candidate latency",
)
_register(
    "maximum_input_tokens",
    lambda c: _validate_max(c, "input tokens"),
    _metric_max("maximum_input_tokens", "input_tokens"),
    "Maximum candidate input tokens",
)
_register(
    "maximum_output_tokens",
    lambda c: _validate_max(c, "output tokens"),
    _metric_max("maximum_output_tokens", "output_tokens"),
    "Maximum candidate output tokens",
)
_register(
    "maximum_candidate_cost",
    lambda c: _validate_max(c, "candidate cost"),
    _metric_max("maximum_candidate_cost", "cost_usd"),
    "Maximum candidate cost",
)
_register(
    "candidate_execution_without_errors",
    lambda c: _validate_keys(c, {"type"}),
    _execution_without_errors,
    "Candidate execution has no errors",
)
