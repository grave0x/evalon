"""Stable public contracts for Evalon's local evaluation platform."""

from evalon.evals.aggregation import (
    AggregationPolicy,
    AggregationResult,
    ScoreAggregation,
    aggregate_case_statuses,
    aggregate_evaluation_results,
)
from evalon.evals.contracts import (
    CaseRunStatus,
    Dataset,
    DatasetCase,
    DatasetVersion,
    EvalCaseRun,
    EvalRun,
    EvalSuite,
    EvaluationContext,
    EvaluationResult,
    EvaluationStatus,
    EvaluatorDefinition,
    EvaluatorRole,
    EvaluatorType,
    JudgeDefinition,
    JsonValue,
    RunStatus,
)
from evalon.evals.datasets import DatasetDiff, DatasetService, TraceCaseProposal
from evalon.evals.store import EvalStore

__all__ = [
    "AggregationPolicy",
    "AggregationResult",
    "CaseRunStatus",
    "Dataset",
    "DatasetCase",
    "DatasetDiff",
    "DatasetService",
    "DatasetVersion",
    "EvalCaseRun",
    "EvalRun",
    "EvalStore",
    "EvalSuite",
    "EvaluationContext",
    "EvaluationResult",
    "EvaluationStatus",
    "EvaluatorDefinition",
    "EvaluatorRole",
    "EvaluatorType",
    "JsonValue",
    "JudgeDefinition",
    "RunStatus",
    "ScoreAggregation",
    "TraceCaseProposal",
    "aggregate_case_statuses",
    "aggregate_evaluation_results",
]
