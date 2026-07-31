"""Registry and validation for Evalon's deterministic evaluator types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from evalon.evals.contracts import (
    EvaluationContext,
    EvaluationResult,
    EvaluatorDefinition,
    EvaluatorType,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


StaticCallable = Callable[[EvaluatorDefinition, EvaluationContext], EvaluationResult]


@dataclass(frozen=True, slots=True)
class StaticEvaluator:
    """One registered deterministic evaluator implementation."""

    type_name: str
    evaluate: StaticCallable
    validate: Callable[[Mapping[str, Any]], None]
    description: str


_REGISTRY: dict[str, StaticEvaluator] = {}


def register_static_evaluator(evaluator: StaticEvaluator) -> StaticEvaluator:
    """Register a built-in evaluator once at import time."""
    normalized = evaluator.type_name.strip().lower()
    if not normalized:
        raise ValueError("Static evaluator type name must be non-empty")
    if normalized in _REGISTRY:
        raise ValueError(f"Static evaluator already registered: {normalized}")
    _REGISTRY[normalized] = evaluator
    return evaluator


def get_static_evaluator(type_name: str) -> StaticEvaluator:
    """Resolve a configured static evaluator type name deterministically."""
    if not isinstance(type_name, str) or not type_name.strip():
        raise ValueError("Static evaluator configuration requires a non-empty 'type'")
    try:
        return _REGISTRY[type_name.strip().lower()]
    except KeyError as exc:
        supported = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"Unknown static evaluator type {type_name!r}; supported: {supported}"
        ) from exc


def list_static_evaluators() -> tuple[StaticEvaluator, ...]:
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


def validate_evaluator_definition(definition: EvaluatorDefinition) -> None:
    """Validate static configuration before candidate execution starts."""
    if definition.evaluator_type is not EvaluatorType.STATIC:
        raise ValueError("validate_evaluator_definition only accepts static evaluators")
    configuration = definition.configuration
    if not isinstance(configuration, dict):
        raise TypeError("Static evaluator configuration must be a dictionary")
    type_name = configuration.get("type")
    get_static_evaluator(type_name).validate(configuration)
