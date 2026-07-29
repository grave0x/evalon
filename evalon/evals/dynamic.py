"""LLM-as-judge dynamic evaluations."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import litellm

from evalon.evals.models import EvalResult, JudgeResult, LLMJudgeConfig

JUDGE_SYSTEM_PROMPT = (
    "You are evalon, an expert evaluator for AI agent runs. "
    "Your task is to evaluate whether the agent's behavior meets the "
    "specified criteria.\n\n"
    "You must respond with a JSON object containing exactly two fields:\n"
    '- "result": either "PASS" or "FAIL"\n'
    '- "reasoning": a clear explanation of your judgment\n\n'
    "Do not include any text outside the JSON object."
)

PREDEFINED_JUDGES: dict[str, str] = {
    "user_goal_achieved": (
        "Evaluate whether the agent successfully achieved the user's goal.\n\n"
        "User input: {input}\n"
        "Agent output: {output}\n"
        "Tool calls made: {tool_calls}\n\n"
        "Did the agent accomplish what the user asked? Consider:\n"
        "- Was the user's question answered directly?\n"
        "- Was the response accurate and complete?\n"
        "- Were appropriate tools used to gather information?"
    ),
}


def _format_tool_calls(trace: dict) -> str:
    """Format tool call spans into a readable string for the judge prompt."""
    tool_spans = [s for s in trace.get("spans", []) if s.get("kind") == "tool"]
    if not tool_spans:
        return "None"
    lines: list[str] = []
    for span in tool_spans:
        name = span.get("name", "?")
        inp = span.get("input")
        out = span.get("output")
        parts = [f"{name}("]
        if inp is not None:
            inp_str = json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp)
            if len(inp_str) > 120:
                inp_str = inp_str[:117] + "..."
            parts.append(inp_str)
        parts.append(")")
        if out is not None:
            out_str = json.dumps(out, ensure_ascii=False) if isinstance(out, (dict, list)) else str(out)
            if len(out_str) > 120:
                out_str = out_str[:117] + "..."
            parts.append(f" -> {out_str}")
        lines.append("  " + "".join(parts))
    return "\n".join(lines)


def _resolve_model(judge: LLMJudgeConfig) -> str:
    """Resolve the model to use: judge.model > EVALON_JUDGE_MODEL env var."""
    if judge.model:
        return judge.model
    model = os.environ.get("EVALON_JUDGE_MODEL")
    if model:
        return model
    raise RuntimeError(
        "No model specified for LLM judge. Set the EVALON_JUDGE_MODEL "
        "environment variable or pass model= to LLMJudgeConfig."
    )


def _build_user_prompt(judge: LLMJudgeConfig, trace: dict) -> str:
    """Build the user message for the LLM judge call."""
    if judge.predefined:
        template = PREDEFINED_JUDGES.get(judge.predefined)
        if template is None:
            raise RuntimeError(
                f"Unknown predefined judge '{judge.predefined}'. "
                f"Available: {', '.join(PREDEFINED_JUDGES)}"
            )
        return template.format(
            input=trace.get("input", ""),
            output=trace.get("output", ""),
            tool_calls=_format_tool_calls(trace),
        )
    if judge.prompt:
        return judge.prompt
    raise RuntimeError(
        f"LLM judge '{judge.name}' must have either 'predefined' or 'prompt' set."
    )


def _parse_judge_response(raw: str) -> JudgeResult:
    """Parse the LLM text response into a JudgeResult.

    Handles clean JSON, JSON in code fences, and JSON embedded in surrounding text.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object in the text
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                return JudgeResult(
                    result="FAIL",
                    reasoning=f"Failed to parse judge response as JSON: {text[:500]}",
                )
        else:
            return JudgeResult(
                result="FAIL",
                reasoning=f"Failed to parse judge response as JSON: {text[:500]}",
            )

    result_value = data.get("result", "").upper()
    if result_value not in ("PASS", "FAIL"):
        return JudgeResult(
            result="FAIL",
            reasoning=f"Judge returned invalid result '{result_value}'. Expected PASS or FAIL.",
        )

    return JudgeResult(
        result=result_value,  # type: ignore[arg-type]
        reasoning=data.get("reasoning", "No reasoning provided."),
    )


def run_llm_judge(trace: dict[str, Any], judge: LLMJudgeConfig) -> EvalResult:
    """Run an LLM judge against a trace and return an EvalResult."""
    try:
        model = _resolve_model(judge)
        user_prompt = _build_user_prompt(judge, trace)
    except RuntimeError as exc:
        return EvalResult(name=judge.name, passed=False, message=str(exc))

    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        response = litellm.completion(model=model, messages=messages)
        raw_content = response.choices[0].message.content or ""
    except Exception as exc:
        return EvalResult(
            name=judge.name,
            passed=False,
            message=f"LLM judge call failed: {type(exc).__name__}: {exc}",
        )

    judge_result = _parse_judge_response(raw_content)

    return EvalResult(
        name=judge.name,
        passed=judge_result.result == "PASS",
        message=judge_result.reasoning,
        details={
            "model": model,
            "judge_result": judge_result.result,
            "predefined": judge.predefined,
        },
    )
