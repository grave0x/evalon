"""Pydantic models for eval configuration, results, and datasets."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EvalResult(BaseModel):
    """Result of a single eval check."""

    name: str
    passed: bool
    message: str
    details: dict = Field(default_factory=dict)


class JudgeResult(BaseModel):
    """Structured output from the LLM judge."""

    result: Literal["PASS", "FAIL"]
    reasoning: str = Field(description="Why the judge passed or failed the trace")


class LLMJudgeConfig(BaseModel):
    """Configuration for an LLM judge (predefined or custom).

    For predefined judges, set ``predefined`` to a known key (e.g.
    ``"user_goal_achieved"``).  For custom judges, supply ``prompt`` with
    your evaluation instructions.  The ``model`` field overrides the
    ``EVALON_JUDGE_MODEL`` environment variable when set.
    """

    name: str
    prompt: str | None = None
    model: str | None = None
    predefined: str | None = None


class EvalConfig(BaseModel):
    """Per-trace eval criteria. All fields optional — populated = activated."""

    max_tool_calls: int | None = None
    min_tool_calls: int | None = None
    max_latency_ms: float | None = None
    min_latency_ms: float | None = None
    allowed_tools: list[str] | None = None
    forbidden_tools: list[str] | None = None
    no_profanity: bool = False
    max_turns: int | None = None
    contains_terms: list[str] | None = None
    not_contains_terms: list[str] | None = None
    expected: str | None = None
    expected_match: str = "exact"  # "exact" | "contains"
    custom_eval: str | None = None  # Python module path
    llm_judges: list[LLMJudgeConfig] | None = None


class DatasetEntry(BaseModel):
    """One trace definition in a dataset with optional eval criteria."""

    name: str
    input: str
    expected: str | None = None
    expected_match: str = "exact"
    max_tool_calls: int | None = None
    min_tool_calls: int | None = None
    max_latency_ms: float | None = None
    min_latency_ms: float | None = None
    allowed_tools: list[str] | None = None
    forbidden_tools: list[str] | None = None
    no_profanity: bool = False
    max_turns: int | None = None
    contains_terms: list[str] | None = None
    not_contains_terms: list[str] | None = None
    custom_eval: str | None = None
    llm_judges: list[LLMJudgeConfig] | None = None

    def to_eval_config(self) -> EvalConfig:
        """Extract eval-only fields into an EvalConfig."""
        eval_fields = set(EvalConfig.model_fields.keys())
        return EvalConfig(**{k: v for k, v in self.model_dump().items() if k in eval_fields})


class Dataset(BaseModel):
    """A collection of trace definitions with eval criteria."""

    project: str
    evals: list[DatasetEntry]
