"""Eval orchestrator and dataset loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from evalon.evals.checks import (
    check_allowed_tools,
    check_contains_terms,
    check_expected,
    check_forbidden_tools,
    check_latency,
    check_not_contains_terms,
    check_num_tool_calls,
    check_profanity,
    check_turns,
)
from evalon.evals.custom import run_custom_eval
from evalon.evals.dynamic import run_llm_judge
from evalon.evals.models import Dataset, DatasetEntry, EvalConfig, EvalResult


def run_evals(trace: dict[str, Any], config: EvalConfig) -> list[EvalResult]:
    """Run all active evals based on which fields are set in the config."""
    results: list[EvalResult] = []

    if config.min_tool_calls is not None or config.max_tool_calls is not None:
        results.append(
            check_num_tool_calls(trace, min=config.min_tool_calls, max=config.max_tool_calls)
        )

    if config.allowed_tools is not None:
        results.append(check_allowed_tools(trace, allowed=config.allowed_tools))

    if config.forbidden_tools is not None:
        results.append(check_forbidden_tools(trace, forbidden=config.forbidden_tools))

    if config.no_profanity:
        results.append(check_profanity(trace))

    if config.min_latency_ms is not None or config.max_latency_ms is not None:
        results.append(
            check_latency(trace, min_ms=config.min_latency_ms, max_ms=config.max_latency_ms)
        )

    if config.max_turns is not None:
        results.append(check_turns(trace, max=config.max_turns))

    if config.contains_terms is not None:
        results.append(check_contains_terms(trace, terms=config.contains_terms))

    if config.not_contains_terms is not None:
        results.append(check_not_contains_terms(trace, terms=config.not_contains_terms))

    if config.expected is not None:
        results.append(check_expected(trace, expected=config.expected, match=config.expected_match))

    if config.custom_eval is not None:
        results.append(run_custom_eval(trace, config.custom_eval))

    if config.llm_judges:
        for judge in config.llm_judges:
            results.append(run_llm_judge(trace, judge))

    return results


def load_dataset(path: str | Path) -> Dataset:
    """Load a YAML dataset file."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Dataset(**data)
