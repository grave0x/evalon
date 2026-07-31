"""Deterministic aggregation rules for evaluator and case outcomes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from statistics import fmean
from typing import Any

from evalon.core.json import sanitize
from evalon.evals.contracts import EvaluationResult, EvaluationStatus


class ScoreAggregation(str, Enum):
    """Supported numeric score aggregation strategies."""

    MEAN = "mean"


@dataclass(frozen=True, slots=True)
class AggregationPolicy:
    """Policy for reported scores.

    Advisory scores are reported by default, but advisory failures and errors
    never block the case. Required evaluators alone decide pass/fail.
    """

    score_aggregation: ScoreAggregation = ScoreAggregation.MEAN
    include_advisory_scores: bool = True


@dataclass(frozen=True, slots=True)
class AggregationResult:
    """A case or run aggregate with explicit outcome counts."""

    status: EvaluationStatus
    score: float | None
    passed: int = 0
    failed: int = 0
    evaluator_errors: int = 0
    advisory_failures: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return sanitize(
            {
                "status": self.status.value,
                "score": self.score,
                "passed": self.passed,
                "failed": self.failed,
                "evaluator_errors": self.evaluator_errors,
                "advisory_failures": self.advisory_failures,
                "total": self.total,
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AggregationResult:
        if not isinstance(value, dict):
            raise TypeError("AggregationResult.from_dict() requires a dictionary")
        return cls(
            status=EvaluationStatus(value["status"]),
            score=value.get("score"),
            passed=int(value.get("passed", 0)),
            failed=int(value.get("failed", 0)),
            evaluator_errors=int(value.get("evaluator_errors", 0)),
            advisory_failures=int(value.get("advisory_failures", 0)),
            total=int(value.get("total", 0)),
        )


def aggregate_evaluation_results(
    results: Iterable[EvaluationResult],
    *,
    candidate_status: EvaluationStatus | str = EvaluationStatus.PASSED,
    policy: AggregationPolicy | None = None,
) -> AggregationResult:
    """Aggregate evaluator results without collapsing distinct failure types.

    Candidate execution errors take precedence and are never presented as
    score failures. A required evaluator error yields ``evaluator_error``. A
    required scored failure yields ``failed``. Advisory outcomes contribute to
    the score according to policy but cannot block the case.
    """

    policy = policy or AggregationPolicy()
    candidate_status = EvaluationStatus(candidate_status)
    items = list(results)

    if candidate_status in {
        EvaluationStatus.PENDING,
        EvaluationStatus.RUNNING,
        EvaluationStatus.EXECUTION_ERROR,
        EvaluationStatus.CANCELLED,
        EvaluationStatus.SKIPPED,
    }:
        return AggregationResult(status=candidate_status, score=None, total=len(items))

    passed = sum(item.status is EvaluationStatus.PASSED for item in items)
    required_failures = sum(
        item.required and item.status is EvaluationStatus.FAILED for item in items
    )
    advisory_failures = sum(
        not item.required and item.status is EvaluationStatus.FAILED for item in items
    )
    required_errors = sum(
        item.required and item.status is EvaluationStatus.EVALUATOR_ERROR
        for item in items
    )
    evaluator_errors = sum(
        item.status is EvaluationStatus.EVALUATOR_ERROR for item in items
    )

    scores = [
        float(item.score)
        for item in items
        if item.score is not None and (item.required or policy.include_advisory_scores)
    ]
    score = fmean(scores) if scores else None

    if required_errors:
        status = EvaluationStatus.EVALUATOR_ERROR
    elif required_failures:
        status = EvaluationStatus.FAILED
    else:
        status = EvaluationStatus.PASSED

    return AggregationResult(
        status=status,
        score=score,
        passed=passed,
        failed=required_failures,
        evaluator_errors=evaluator_errors,
        advisory_failures=advisory_failures,
        total=len(items),
    )


def aggregate_case_statuses(
    statuses: Iterable[EvaluationStatus | str],
) -> EvaluationStatus:
    """Aggregate case statuses into a run status with failure-type precedence."""

    values = [EvaluationStatus(status) for status in statuses]
    if not values:
        return EvaluationStatus.SKIPPED
    if any(status is EvaluationStatus.RUNNING for status in values):
        return EvaluationStatus.RUNNING
    if any(status is EvaluationStatus.PENDING for status in values):
        return EvaluationStatus.PENDING
    for status in (
        EvaluationStatus.EXECUTION_ERROR,
        EvaluationStatus.EVALUATOR_ERROR,
        EvaluationStatus.FAILED,
        EvaluationStatus.CANCELLED,
    ):
        if status in values:
            return status
    if all(status is EvaluationStatus.SKIPPED for status in values):
        return EvaluationStatus.SKIPPED
    return EvaluationStatus.PASSED
