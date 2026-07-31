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
from evalon.evals.custom import (
    evaluator,
    execute_custom_evaluator,
    load_evaluator,
    run_custom_evaluator,
)
from evalon.evals.datasets import DatasetDiff, DatasetService, TraceCaseProposal
from evalon.evals.evaluators import (
    StaticEvaluator,
    evaluate_static,
    get_static_evaluator,
    list_static_evaluators,
    validate_evaluator_definition,
)
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
    "StaticEvaluator",
    "TraceCaseProposal",
    "aggregate_case_statuses",
    "aggregate_evaluation_results",
    "evaluate_static",
    "evaluator",
    "execute_custom_evaluator",
    "get_static_evaluator",
    "list_static_evaluators",
    "load_evaluator",
    "run_custom_evaluator",
    "validate_evaluator_definition",
]
