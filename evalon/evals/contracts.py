"""Public, storage-independent contracts for Evalon evaluations.

The dataclasses in this module are deliberately free of persistence and TUI
concerns.  IDs are stable public identities; timestamps describe lifecycle
transitions and are populated by the service or store that performs them.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Self

from evalon.core.json import sanitize

if TYPE_CHECKING:
    from collections.abc import Mapping


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_json_value(value: Any, field_name: str) -> None:
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int | float) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, f"{field_name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} object keys must be strings")
            _require_json_value(item, f"{field_name}.{key}")
        return
    raise TypeError(f"{field_name} must contain only JSON-compatible values")


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _enum_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_enum_value(item) for item in value]
    return value


class SerializableContract:
    """Mixin providing sanitized JSON-compatible dictionary conversion."""

    _enum_fields: ClassVar[dict[str, type[Enum]]] = {}
    _nested_fields: ClassVar[dict[str, type[SerializableContract]]] = {}
    _nested_list_fields: ClassVar[dict[str, type[SerializableContract]]] = {}

    def to_dict(self) -> dict[str, Any]:
        payload = {
            item.name: _enum_value(getattr(self, item.name))
            for item in fields(self)
            if not item.name.startswith("_")
        }
        return sanitize(payload)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, dict):
            raise TypeError(f"{cls.__name__}.from_dict() requires a dictionary")

        known = {item.name for item in fields(cls) if item.init}
        unknown = set(value) - known
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"Unknown {cls.__name__} field(s): {names}")

        payload = dict(value)
        for name, enum_type in cls._enum_fields.items():
            if (
                name in payload
                and payload[name] is not None
                and not isinstance(payload[name], enum_type)
            ):
                payload[name] = enum_type(payload[name])
        for name, nested_type in cls._nested_fields.items():
            item = payload.get(name)
            if isinstance(item, dict):
                payload[name] = nested_type.from_dict(item)
        for name, nested_type in cls._nested_list_fields.items():
            if name in payload:
                if not isinstance(payload[name], list):
                    raise TypeError(f"{cls.__name__}.{name} must be a list")
                payload[name] = [
                    nested_type.from_dict(item) if isinstance(item, dict) else item
                    for item in payload[name]
                ]
        return cls(**payload)


class EvaluationStatus(str, Enum):
    """Lifecycle status shared by evaluation runs and individual case runs."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    EXECUTION_ERROR = "execution_error"
    EVALUATOR_ERROR = "evaluator_error"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self not in {self.PENDING, self.RUNNING}


class EvaluatorType(str, Enum):
    """How an evaluator is implemented."""

    STATIC = "static"
    PYTHON = "python"
    LLM_JUDGE = "llm_judge"


class EvaluatorRole(str, Enum):
    """Whether an evaluator is allowed to block a case."""

    REQUIRED = "required"
    ADVISORY = "advisory"


@dataclass(slots=True)
class JudgeDefinition(SerializableContract):
    """An immutable, reusable binary LLM judge configuration.

    A judge version is deliberately independent from an evaluation suite.  A
    suite binding supplies its blocking role; the definition itself never
    carries scoring ranges or thresholds.
    """

    name: str
    provider: str
    model: str
    rubric: str
    version: str = "1"
    included_context_fields: list[str] = field(
        default_factory=lambda: ["case_input", "candidate_output", "expected_output"]
    )
    temperature: float = 0.0
    timeout_seconds: float | None = None
    base_url: str | None = None
    max_retries: int = 2
    retry_delay_seconds: float = 0.25
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("judge"))
    created_at: str | None = None
    archived_at: str | None = None

    _SUPPORTED_PROVIDERS: ClassVar[frozenset[str]] = frozenset(
        {"openai", "openrouter", "anthropic"}
    )

    def __post_init__(self) -> None:
        for field_name, value in (
            ("JudgeDefinition.id", self.id),
            ("JudgeDefinition.name", self.name),
            ("JudgeDefinition.version", self.version),
            ("JudgeDefinition.provider", self.provider),
            ("JudgeDefinition.model", self.model),
            ("JudgeDefinition.rubric", self.rubric),
        ):
            _require_text(value, field_name)
        if self.provider not in self._SUPPORTED_PROVIDERS:
            providers = ", ".join(sorted(self._SUPPORTED_PROVIDERS))
            raise ValueError(f"JudgeDefinition.provider must be one of: {providers}")
        if not self.included_context_fields or any(
            not isinstance(item, str) or not item.strip()
            for item in self.included_context_fields
        ):
            raise ValueError(
                "JudgeDefinition.included_context_fields must contain non-empty strings"
            )
        for field_name, value in (
            ("temperature", self.temperature),
            ("retry_delay_seconds", self.retry_delay_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise ValueError(f"JudgeDefinition.{field_name} must be finite numeric")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("JudgeDefinition.timeout_seconds must be positive when provided")
        if self.retry_delay_seconds < 0:
            raise ValueError("JudgeDefinition.retry_delay_seconds must be non-negative")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("JudgeDefinition.max_retries must be a non-negative integer")
        if self.base_url is not None:
            _require_text(self.base_url, "JudgeDefinition.base_url")
        _require_json_value(self.metadata, "JudgeDefinition.metadata")


@dataclass(slots=True)
class Dataset(SerializableContract):
    """A named dataset whose content changes only through new versions.

    ``id`` is the stable identity. Archiving sets ``archived_at`` and does not
    remove versions already referenced by runs.
    """

    name: str
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("dataset"))
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.id, "Dataset.id")
        _require_text(self.name, "Dataset.name")


@dataclass(slots=True)
class DatasetCase(SerializableContract):
    """An immutable case snapshot belonging to one dataset version.

    ``case_id`` is the stable user-facing identity retained across versions.
    ``id`` uniquely identifies this immutable snapshot within persistence, and
    ``dataset_version_id`` identifies the version containing the snapshot.
    """

    case_id: str
    input: Any
    expected_output: Any = None
    reference_output: Any = None
    tags: list[str] = field(default_factory=list)
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    dataset_version_id: str | None = None
    source_trace_id: str | None = None
    source_project: str | None = None
    source_captured_at: str | None = None
    id: str = field(default_factory=lambda: _new_id("case"))

    def __post_init__(self) -> None:
        _require_text(self.id, "DatasetCase.id")
        _require_text(self.case_id, "DatasetCase.case_id")
        if self.dataset_version_id is not None:
            _require_text(self.dataset_version_id, "DatasetCase.dataset_version_id")
        if any(not isinstance(tag, str) or not tag.strip() for tag in self.tags):
            raise ValueError("DatasetCase.tags must contain non-empty strings")
        _require_json_value(self.input, "DatasetCase.input")
        _require_json_value(self.expected_output, "DatasetCase.expected_output")
        _require_json_value(self.reference_output, "DatasetCase.reference_output")
        _require_json_value(self.metadata, "DatasetCase.metadata")


@dataclass(slots=True)
class DatasetVersion(SerializableContract):
    """An immutable numbered snapshot of all cases in a dataset.

    ``id`` is the persisted identity and ``(dataset_id, version)`` is the
    human-readable identity. Published versions are never rewritten.
    """

    dataset_id: str
    version: int
    cases: list[DatasetCase] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    change_note: str | None = None
    id: str = field(default_factory=lambda: _new_id("dataset_version"))
    created_at: str | None = None

    _nested_list_fields: ClassVar[dict[str, type[SerializableContract]]] = {
        "cases": DatasetCase
    }

    def __post_init__(self) -> None:
        _require_text(self.id, "DatasetVersion.id")
        _require_text(self.dataset_id, "DatasetVersion.dataset_id")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("DatasetVersion.version must be a positive integer")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("DatasetVersion case_id values must be unique")


@dataclass(slots=True)
class EvaluatorDefinition(SerializableContract):
    """A versioned evaluator configuration snapshotted into every run."""

    name: str
    version: str
    evaluator_type: EvaluatorType
    role: EvaluatorRole = EvaluatorRole.REQUIRED
    score_min: float = 0.0
    score_max: float = 1.0
    pass_threshold: float = 1.0
    configuration: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("evaluator"))
    created_at: str | None = None

    _enum_fields: ClassVar[dict[str, type[Enum]]] = {
        "evaluator_type": EvaluatorType,
        "role": EvaluatorRole,
    }

    def __post_init__(self) -> None:
        _require_text(self.id, "EvaluatorDefinition.id")
        _require_text(self.name, "EvaluatorDefinition.name")
        _require_text(self.version, "EvaluatorDefinition.version")
        if not isinstance(self.evaluator_type, EvaluatorType):
            self.evaluator_type = EvaluatorType(self.evaluator_type)
        if not isinstance(self.role, EvaluatorRole):
            self.role = EvaluatorRole(self.role)
        for name, value in (
            ("score_min", self.score_min),
            ("score_max", self.score_max),
            ("pass_threshold", self.pass_threshold),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"EvaluatorDefinition.{name} must be numeric")
            if not math.isfinite(value):
                raise ValueError(f"EvaluatorDefinition.{name} must be finite")
        if self.score_min >= self.score_max:
            raise ValueError(
                "EvaluatorDefinition.score_min must be less than score_max"
            )
        if not self.score_min <= self.pass_threshold <= self.score_max:
            raise ValueError(
                "EvaluatorDefinition.pass_threshold must be within the score range"
            )

    @property
    def required(self) -> bool:
        return self.role is EvaluatorRole.REQUIRED


@dataclass(slots=True)
class EvalSuite(SerializableContract):
    """A named, versioned binding of a dataset, target, and evaluators."""

    name: str
    project: str
    dataset_id: str
    target_ref: str
    evaluators: list[EvaluatorDefinition] = field(default_factory=list)
    version: str = "1"
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("suite"))
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None

    _nested_list_fields: ClassVar[dict[str, type[SerializableContract]]] = {
        "evaluators": EvaluatorDefinition
    }

    def __post_init__(self) -> None:
        _require_text(self.id, "EvalSuite.id")
        _require_text(self.name, "EvalSuite.name")
        _require_text(self.project, "EvalSuite.project")
        _require_text(self.dataset_id, "EvalSuite.dataset_id")
        _require_text(self.target_ref, "EvalSuite.target_ref")
        _require_text(self.version, "EvalSuite.version")
        evaluator_ids = [evaluator.id for evaluator in self.evaluators]
        if len(evaluator_ids) != len(set(evaluator_ids)):
            raise ValueError("EvalSuite evaluator IDs must be unique")


@dataclass(slots=True)
class EvaluationContext(SerializableContract):
    """The common input contract used by static, Python, and LLM evaluators."""

    case_input: Any
    candidate_output: Any
    expected_output: Any = None
    case_metadata: dict[str, Any] = field(default_factory=dict)
    candidate_trace: dict[str, Any] | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int | float] = field(default_factory=dict)
    reference_output: Any = None
    case_id: str | None = None

    def __post_init__(self) -> None:
        if self.case_id is not None:
            _require_text(self.case_id, "EvaluationContext.case_id")
        if any(
            isinstance(value, bool) or not isinstance(value, int | float)
            for value in self.metrics.values()
        ):
            raise TypeError("EvaluationContext.metrics values must be numeric")


@dataclass(slots=True)
class EvaluationResult(SerializableContract):
    """One evaluator outcome for one case run.

    A normal outcome sets ``passed`` (and usually ``score``). An evaluator
    failure sets ``evaluator_error`` and leaves ``passed`` unset, ensuring an
    infrastructure failure cannot be mistaken for a candidate score failure.
    """

    evaluator_id: str
    judge_definition_id: str | None = None
    evaluator_name: str | None = None
    evaluator_version: str | None = None
    evaluator_type: EvaluatorType | None = None
    score: float | None = None
    passed: bool | None = None
    label: str | None = None
    reason: str | None = None
    evidence: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    evaluator_error: dict[str, Any] | None = None
    skipped: bool = False
    required: bool = True
    eval_case_run_id: str | None = None
    judge_trace_id: str | None = None
    duration_ms: float | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    id: str = field(default_factory=lambda: _new_id("result"))
    created_at: str | None = None

    _enum_fields: ClassVar[dict[str, type[Enum]]] = {"evaluator_type": EvaluatorType}

    def __post_init__(self) -> None:
        _require_text(self.id, "EvaluationResult.id")
        _require_text(self.evaluator_id, "EvaluationResult.evaluator_id")
        if self.judge_definition_id is not None:
            _require_text(
                self.judge_definition_id, "EvaluationResult.judge_definition_id"
            )
        if self.evaluator_name is not None:
            _require_text(self.evaluator_name, "EvaluationResult.evaluator_name")
        if self.evaluator_version is not None:
            _require_text(self.evaluator_version, "EvaluationResult.evaluator_version")
        if self.evaluator_type is not None and not isinstance(
            self.evaluator_type, EvaluatorType
        ):
            self.evaluator_type = EvaluatorType(self.evaluator_type)
        if self.eval_case_run_id is not None:
            _require_text(self.eval_case_run_id, "EvaluationResult.eval_case_run_id")
        if not isinstance(self.required, bool):
            raise TypeError("EvaluationResult.required must be a boolean")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, int | float):
                raise TypeError("EvaluationResult.score must be numeric or None")
            if not math.isfinite(self.score):
                raise ValueError("EvaluationResult.score must be finite")
        if self.evaluator_type is EvaluatorType.LLM_JUDGE and self.score is not None:
            raise ValueError("LLM judge results must not contain a numeric score")
        if self.passed is not None and not isinstance(self.passed, bool):
            raise TypeError("EvaluationResult.passed must be a boolean or None")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int | float)
            or not math.isfinite(self.duration_ms)
            or self.duration_ms < 0
        ):
            raise ValueError(
                "EvaluationResult.duration_ms must be a finite non-negative number"
            )
        if self.evaluator_error is not None:
            if not isinstance(self.evaluator_error, dict):
                raise TypeError(
                    "EvaluationResult.evaluator_error must be a dictionary or None"
                )
            if self.passed is not None:
                raise ValueError(
                    "An evaluator error cannot also be a pass/fail score outcome"
                )
        if not isinstance(self.skipped, bool):
            raise TypeError("EvaluationResult.skipped must be a boolean")
        if self.skipped and (
            self.passed is not None or self.evaluator_error is not None
        ):
            raise ValueError("A skipped evaluator cannot also pass, fail, or error")
        if (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, int | float)
            or not math.isfinite(self.cost_usd)
            or self.cost_usd < 0
        ):
            raise ValueError(
                "EvaluationResult.cost_usd must be a finite non-negative number"
            )
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"EvaluationResult.{name} must be a non-negative integer"
                )

    @property
    def status(self) -> EvaluationStatus:
        if self.skipped:
            return EvaluationStatus.SKIPPED
        if self.evaluator_error is not None:
            return EvaluationStatus.EVALUATOR_ERROR
        if self.passed is True:
            return EvaluationStatus.PASSED
        if self.passed is False:
            return EvaluationStatus.FAILED
        return EvaluationStatus.PENDING


@dataclass(slots=True)
class EvalCaseRun(SerializableContract):
    """Candidate execution and evaluator outcomes for one dataset case."""

    eval_run_id: str
    case_id: str
    dataset_case_id: str | None = None
    case_input: Any = None
    expected_output: Any = None
    case_metadata: dict[str, Any] = field(default_factory=dict)
    status: EvaluationStatus = EvaluationStatus.PENDING
    candidate_output: Any = None
    candidate_error: dict[str, Any] | None = None
    candidate_trace_id: str | None = None
    results: list[EvaluationResult] = field(default_factory=list)
    aggregate_score: float | None = None
    metrics: dict[str, int | float] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("case_run"))
    started_at: str | None = None
    ended_at: str | None = None

    _enum_fields: ClassVar[dict[str, type[Enum]]] = {"status": EvaluationStatus}
    _nested_list_fields: ClassVar[dict[str, type[SerializableContract]]] = {
        "results": EvaluationResult
    }

    def __post_init__(self) -> None:
        _require_text(self.id, "EvalCaseRun.id")
        _require_text(self.eval_run_id, "EvalCaseRun.eval_run_id")
        _require_text(self.case_id, "EvalCaseRun.case_id")
        if self.dataset_case_id is not None:
            _require_text(self.dataset_case_id, "EvalCaseRun.dataset_case_id")
        if not isinstance(self.status, EvaluationStatus):
            self.status = EvaluationStatus(self.status)
        if self.candidate_error is not None and self.status not in {
            EvaluationStatus.PENDING,
            EvaluationStatus.RUNNING,
            EvaluationStatus.EXECUTION_ERROR,
        }:
            raise ValueError("A candidate error requires execution_error status")


@dataclass(slots=True)
class EvalRun(SerializableContract):
    """A reproducible evaluation of one dataset version with one suite snapshot."""

    project: str
    dataset_id: str
    dataset_version_id: str
    suite_id: str
    name: str | None = None
    baseline_run_id: str | None = None
    status: EvaluationStatus = EvaluationStatus.PENDING
    target_configuration: dict[str, Any] = field(default_factory=dict)
    evaluator_snapshots: list[EvaluatorDefinition] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    git_commit: str | None = None
    case_runs: list[EvalCaseRun] = field(default_factory=list)
    aggregate_score: float | None = None
    candidate_cost_usd: float = 0.0
    judge_cost_usd: float = 0.0
    id: str = field(default_factory=lambda: _new_id("eval_run"))
    created_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None

    _enum_fields: ClassVar[dict[str, type[Enum]]] = {"status": EvaluationStatus}
    _nested_list_fields: ClassVar[dict[str, type[SerializableContract]]] = {
        "evaluator_snapshots": EvaluatorDefinition,
        "case_runs": EvalCaseRun,
    }

    def __post_init__(self) -> None:
        _require_text(self.id, "EvalRun.id")
        _require_text(self.project, "EvalRun.project")
        _require_text(self.dataset_id, "EvalRun.dataset_id")
        _require_text(self.dataset_version_id, "EvalRun.dataset_version_id")
        _require_text(self.suite_id, "EvalRun.suite_id")
        if self.name is not None:
            _require_text(self.name, "EvalRun.name")
        if self.baseline_run_id is not None:
            _require_text(self.baseline_run_id, "EvalRun.baseline_run_id")
        if not isinstance(self.status, EvaluationStatus):
            self.status = EvaluationStatus(self.status)
        for name, value in (
            ("candidate_cost_usd", self.candidate_cost_usd),
            ("judge_cost_usd", self.judge_cost_usd),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"EvalRun.{name} must be a finite non-negative number")


# Explicit aliases make the shared lifecycle contract discoverable at each level
# without creating subtly divergent status vocabularies.
RunStatus = EvaluationStatus
CaseRunStatus = EvaluationStatus
