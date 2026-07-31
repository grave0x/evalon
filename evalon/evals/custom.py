"""Explicitly configured project Python evaluators."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from time import perf_counter
from typing import Any

from evalon.core.json import sanitize
from evalon.evals.contracts import (
    EvaluationContext,
    EvaluationResult,
    EvaluatorDefinition,
    EvaluatorType,
)


def evaluator(
    func: Callable[[EvaluationContext], Any] | None = None,
    *,
    name: str | None = None,
    version: str = "1",
) -> (
    Callable[[Callable[[EvaluationContext], Any]], Callable[[EvaluationContext], Any]]
    | Callable[[EvaluationContext], Any]
):
    """Mark a callable as an evaluator without implicitly discovering it.

    The decorator is metadata only. A caller must still configure the callable
    explicitly through a ``module:attribute`` reference before it can execute.
    """

    if not isinstance(version, str) or not version.strip():
        raise ValueError("Evaluator version must be a non-empty string")

    def decorate(
        target: Callable[[EvaluationContext], Any],
    ) -> Callable[[EvaluationContext], Any]:
        if not callable(target):
            raise TypeError("@evaluator can only decorate a callable")
        evaluator_name = name or target.__name__
        if not isinstance(evaluator_name, str) or not evaluator_name.strip():
            raise ValueError("Evaluator name must be a non-empty string")
        target.__evalon_evaluator__ = {"name": evaluator_name, "version": version}
        return target

    return decorate if func is None else decorate(func)


def load_evaluator(reference: str) -> Callable[[EvaluationContext], Any]:
    """Load only an explicit ``module:attribute`` evaluator reference."""
    if not isinstance(reference, str) or reference.count(":") != 1:
        raise ValueError("Evaluator reference must use 'module:callable' syntax")
    module_name, attribute_path = reference.split(":", 1)
    if not module_name or not attribute_path:
        raise ValueError("Evaluator reference must use 'module:callable' syntax")
    module = importlib.import_module(module_name)
    target: Any = module
    for attribute in attribute_path.split("."):
        if not attribute:
            raise ValueError("Evaluator reference contains an empty attribute")
        target = getattr(target, attribute)
    if not callable(target):
        raise TypeError(f"Configured evaluator {reference!r} is not callable")
    return target


def _error_result(
    definition: EvaluatorDefinition,
    reference: str,
    exc: BaseException,
    duration_ms: float,
) -> EvaluationResult:
    return EvaluationResult(
        evaluator_id=definition.id,
        evaluator_name=definition.name,
        evaluator_version=definition.version,
        evaluator_type=EvaluatorType.PYTHON,
        required=definition.required,
        label="evaluator_error",
        reason="Custom evaluator raised an exception",
        evaluator_error={"type": type(exc).__name__, "message": str(exc)},
        metadata={"callable": reference},
        duration_ms=duration_ms,
    )


def _normalise_result(
    value: Any,
    definition: EvaluatorDefinition,
    reference: str,
    duration_ms: float,
) -> EvaluationResult:
    if isinstance(value, EvaluationResult):
        result = value
        if result.evaluator_id != definition.id:
            raise ValueError(
                "Custom evaluator returned a result for another evaluator ID"
            )
    elif isinstance(value, dict):
        payload = dict(value)
        if "passed" not in payload and "evaluator_error" not in payload:
            raise ValueError(
                "Custom evaluator dictionary must include passed or evaluator_error"
            )
        supplied_id = payload.pop("evaluator_id", definition.id)
        if supplied_id != definition.id:
            raise ValueError(
                "Custom evaluator result evaluator_id does not match definition"
            )
        payload.update(
            {
                "evaluator_id": definition.id,
                "evaluator_name": definition.name,
                "evaluator_version": definition.version,
                "evaluator_type": EvaluatorType.PYTHON.value,
                "required": definition.required,
            }
        )
        result = EvaluationResult.from_dict(payload)
    else:
        raise TypeError("Custom evaluator must return EvaluationResult or a dictionary")

    if result.evaluator_type not in {None, EvaluatorType.PYTHON}:
        raise ValueError("Custom evaluator result must have evaluator_type 'python'")
    result.evaluator_name = definition.name
    result.evaluator_version = definition.version
    result.evaluator_type = EvaluatorType.PYTHON
    result.required = definition.required
    if result.score is not None and not (
        definition.score_min <= result.score <= definition.score_max
    ):
        raise ValueError("Custom evaluator score is outside the configured range")
    result.duration_ms = duration_ms
    result.metadata = sanitize({**result.metadata, "callable": reference})
    return result


async def run_custom_evaluator(
    definition: EvaluatorDefinition,
    context: EvaluationContext,
    *,
    reference: str | None = None,
) -> EvaluationResult:
    """Run an explicit sync or async evaluator, isolating failures per result."""
    if definition.evaluator_type is not EvaluatorType.PYTHON:
        raise ValueError("run_custom_evaluator requires a Python evaluator definition")
    configured_reference = reference or definition.configuration.get("callable")
    started = perf_counter()
    try:
        if (
            not isinstance(configured_reference, str)
            or not configured_reference.strip()
        ):
            raise ValueError(
                "Python evaluator configuration requires callable='module:callable'"
            )
        target = load_evaluator(configured_reference)
        value = target(context)
        if inspect.isawaitable(value):
            value = await value
        return _normalise_result(
            value,
            definition,
            configured_reference,
            round((perf_counter() - started) * 1000, 3),
        )
    except Exception as exc:  # noqa: BLE001 - isolated evaluator failures are data
        return _error_result(
            definition,
            str(configured_reference),
            exc,
            round((perf_counter() - started) * 1000, 3),
        )


execute_custom_evaluator = run_custom_evaluator
