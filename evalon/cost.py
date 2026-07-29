"""Cost calculation with cache-aware pricing via litellm (optional dependency)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_HAS_LITELLM = False
_model_cost: dict[str, Any] = {}

try:
    from litellm import cost_per_token as _litellm_cost_per_token
    from litellm import model_cost as _litellm_model_cost

    _HAS_LITELLM = True
    _model_cost = _litellm_model_cost  # type: ignore[assignment]
except ImportError:
    pass


@dataclass(frozen=True)
class CostBreakdown:
    """Per-call cost breakdown in USD."""

    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_creation_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.input_cost + self.output_cost + self.cache_read_cost + self.cache_creation_cost


import re

_MODEL_DATE_RE = re.compile(r"-\d{8}$")


def _get_model_info(model: str) -> dict[str, Any] | None:
    if not model:
        return None
    info = _model_cost.get(model)
    if info is not None:
        return info
    # Strip date suffix (e.g. "deepseek/deepseek-v4-flash-20260423" → "deepseek/deepseek-v4-flash")
    stripped = _MODEL_DATE_RE.sub("", model)
    return _model_cost.get(stripped)


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    *,
    prompt_tokens_includes_cache: bool = True,
) -> CostBreakdown | None:
    """Calculate cost in USD with cache-aware pricing.

    For OpenAI-compatible providers (prompt_tokens_includes_cache=True):
        prompt_tokens already includes cached tokens.
        input_cost = (prompt_tokens - cache_read_tokens) * input_rate + cache_read_tokens * cache_read_rate

    For Anthropic (prompt_tokens_includes_cache=False):
        prompt_tokens does NOT include cache tokens.
        input_cost = prompt_tokens * input_rate + cache_read_tokens * cache_read_rate + cache_creation_tokens * cache_creation_rate

    Returns None if litellm is not installed or the model is unknown.
    """
    if not _HAS_LITELLM or not model:
        return None

    model_info = _get_model_info(model)
    if model_info is None:
        return None

    input_rate = model_info.get("input_cost_per_token", 0) or 0
    output_rate = model_info.get("output_cost_per_token", 0) or 0
    cache_read_rate = model_info.get("cache_read_input_token_cost", 0) or 0
    cache_creation_rate = model_info.get("cache_creation_input_token_cost", 0) or 0

    output_cost = completion_tokens * output_rate

    if cache_read_tokens > 0 or cache_creation_tokens > 0:
        if prompt_tokens_includes_cache:
            # OpenAI: prompt_tokens includes cached tokens — subtract them
            # so cached tokens are billed at cache_read_rate, not input_rate
            uncached_input = max(prompt_tokens - cache_read_tokens, 0)
            input_cost = uncached_input * input_rate + cache_read_tokens * cache_read_rate
            cache_read_cost = cache_read_tokens * cache_read_rate
            cache_creation_cost = cache_creation_tokens * cache_creation_rate
        else:
            # Anthropic: prompt_tokens does NOT include cache tokens
            input_cost = prompt_tokens * input_rate
            cache_read_cost = cache_read_tokens * cache_read_rate
            cache_creation_cost = cache_creation_tokens * cache_creation_rate
    else:
        # No cache tokens — simple calculation
        input_cost = prompt_tokens * input_rate
        cache_read_cost = 0.0
        cache_creation_cost = 0.0

    return CostBreakdown(
        input_cost=input_cost,
        output_cost=output_cost,
        cache_read_cost=cache_read_cost,
        cache_creation_cost=cache_creation_cost,
    )


def is_available() -> bool:
    return _HAS_LITELLM
