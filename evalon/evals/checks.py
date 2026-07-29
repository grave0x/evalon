from __future__ import annotations

import json
import re

from evalon.evals.models import EvalResult

PROFANITY_PATTERNS = re.compile(
    r"f+u*c*k+|f+u*c*k+i*n*g+|f+c+k+|f\s+u\s+c\s+k",
    re.IGNORECASE,
)


def _get_tool_spans(trace: dict) -> list[dict]:
    return [s for s in trace.get("spans", []) if s.get("kind") == "tool"]


def _format_tool_call(span: dict) -> str:
    name = span.get("name", "?")
    inp = span.get("input")
    out = span.get("output")
    parts = [f"{name}("]
    if inp is not None:
        inp_str = json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp)
        if len(inp_str) > 80:
            inp_str = inp_str[:77] + "..."
        parts.append(inp_str)
    parts.append(")")
    if out is not None:
        out_str = json.dumps(out, ensure_ascii=False) if isinstance(out, (dict, list)) else str(out)
        if len(out_str) > 80:
            out_str = out_str[:77] + "..."
        parts.append(f" -> {out_str}")
    return "".join(parts)


def check_num_tool_calls(trace: dict, min: int | None = None, max: int | None = None) -> EvalResult:
    tool_spans = _get_tool_spans(trace)
    actual = len(tool_spans)
    bounds = []
    if min is not None:
        bounds.append(f"min={min}")
    if max is not None:
        bounds.append(f"max={max}")
    bounds_str = ", ".join(bounds) if bounds else "no bounds"

    passed = True
    if min is not None and actual < min:
        passed = False
    if max is not None and actual > max:
        passed = False

    tool_calls = [_format_tool_call(s) for s in tool_spans]
    details: dict = {"actual": actual, "tool_calls": tool_calls}
    if min is not None:
        details["min"] = min
    if max is not None:
        details["max"] = max

    return EvalResult(
        name="check_num_tool_calls",
        passed=passed,
        message=f"Tool calls: {actual} ({bounds_str})",
        details=details,
    )


def check_allowed_tools(trace: dict, allowed: list[str]) -> EvalResult:
    tool_spans = _get_tool_spans(trace)
    names = [s["name"] for s in tool_spans if "name" in s]
    disallowed = [n for n in names if n not in allowed]
    passed = len(disallowed) == 0
    tool_calls = [_format_tool_call(s) for s in tool_spans]
    return EvalResult(
        name="check_allowed_tools",
        passed=passed,
        message=(
            f"All tool names are allowed"
            if passed
            else f"Disallowed tools found: {disallowed}"
        ),
        details={"tool_calls": tool_calls, "allowed": allowed, "disallowed": disallowed},
    )


def check_forbidden_tools(trace: dict, forbidden: list[str]) -> EvalResult:
    tool_spans = _get_tool_spans(trace)
    names = [s["name"] for s in tool_spans if "name" in s]
    found = [n for n in names if n in forbidden]
    passed = len(found) == 0
    tool_calls = [_format_tool_call(s) for s in tool_spans]
    return EvalResult(
        name="check_forbidden_tools",
        passed=passed,
        message=(
            f"No forbidden tools used"
            if passed
            else f"Forbidden tools found: {found}"
        ),
        details={"tool_calls": tool_calls, "forbidden": forbidden, "found": found},
    )


def check_profanity(trace: dict) -> EvalResult:
    output = trace.get("output")
    if output is None:
        return EvalResult(
            name="check_profanity",
            passed=True,
            message="No output to check",
            details={"output": None},
        )
    text = str(output)
    match = PROFANITY_PATTERNS.search(text)
    passed = match is None
    return EvalResult(
        name="check_profanity",
        passed=passed,
        message=(
            "No profanity detected"
            if passed
            else f"Profanity detected: '{match.group()}'"
        ),
        details={"output_excerpt": text[:200], "match": match.group() if match else None},
    )


def check_latency(trace: dict, min_ms: float | None = None, max_ms: float | None = None) -> EvalResult:
    metrics = trace.get("metrics", {})
    actual = metrics.get("latency_ms")
    if actual is None:
        return EvalResult(
            name="check_latency",
            passed=False,
            message="No latency_ms in metrics",
            details={"actual": None},
        )

    passed = True
    if min_ms is not None and actual < min_ms:
        passed = False
    if max_ms is not None and actual > max_ms:
        passed = False

    details: dict = {"actual": actual}
    if min_ms is not None:
        details["min_ms"] = min_ms
    if max_ms is not None:
        details["max_ms"] = max_ms

    parts = [f"{actual:.1f}ms"]
    if min_ms is not None:
        parts.append(f"min={min_ms}ms")
    if max_ms is not None:
        parts.append(f"max={max_ms}ms")

    return EvalResult(
        name="check_latency",
        passed=passed,
        message=f"Latency: {' vs '.join(parts)}",
        details=details,
    )


def check_turns(trace: dict, max: int) -> EvalResult:
    metrics = trace.get("metrics", {})
    actual = metrics.get("llm_calls")
    if actual is None:
        return EvalResult(
            name="check_turns",
            passed=False,
            message="No llm_calls in metrics",
            details={"actual": None, "max": max},
        )

    passed = actual <= max
    return EvalResult(
        name="check_turns",
        passed=passed,
        message=f"Turns: {actual} (max={max})",
        details={"actual": actual, "max": max},
    )


def check_contains_terms(trace: dict, terms: list[str]) -> EvalResult:
    output = trace.get("output")
    if output is None:
        return EvalResult(
            name="check_contains_terms",
            passed=False,
            message="No output to check",
            details={"missing": terms},
        )
    text = str(output).lower()
    missing = [t for t in terms if t.lower() not in text]
    passed = len(missing) == 0
    return EvalResult(
        name="check_contains_terms",
        passed=passed,
        message=(
            f"All terms found"
            if passed
            else f"Missing terms: {missing}"
        ),
        details={"terms": terms, "missing": missing},
    )


def check_not_contains_terms(trace: dict, terms: list[str]) -> EvalResult:
    output = trace.get("output")
    if output is None:
        return EvalResult(
            name="check_not_contains_terms",
            passed=True,
            message="No output to check",
            details={"found": []},
        )
    text = str(output).lower()
    found = [t for t in terms if t.lower() in text]
    passed = len(found) == 0
    return EvalResult(
        name="check_not_contains_terms",
        passed=passed,
        message=(
            "None of the forbidden terms found"
            if passed
            else f"Forbidden terms found: {found}"
        ),
        details={"terms": terms, "found": found},
    )


def check_expected(trace: dict, expected: str, match: str = "exact") -> EvalResult:
    output = trace.get("output")
    if output is None:
        return EvalResult(
            name="check_expected",
            passed=False,
            message="No output to check",
            details={"expected": expected, "actual": None, "match": match},
        )

    actual = str(output)
    if match == "contains":
        passed = expected.lower() in actual.lower()
        msg = (
            f"Output contains expected substring"
            if passed
            else f"Output does not contain expected substring"
        )
    else:
        passed = actual == expected
        msg = (
            "Output matches expected"
            if passed
            else "Output does not match expected"
        )

    return EvalResult(
        name="check_expected",
        passed=passed,
        message=msg,
        details={"expected": expected, "actual": actual, "match": match},
    )
