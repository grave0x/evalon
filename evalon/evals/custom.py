from __future__ import annotations

import importlib

from evalon.evals.models import EvalResult


def run_custom_eval(trace: dict, module_path: str) -> EvalResult:
    try:
        module_name, func_name = module_path.rsplit(".", 1)
    except ValueError:
        return EvalResult(
            name=module_path,
            passed=False,
            message=f"Invalid module path '{module_path}': expected 'module.function' format",
        )

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        return EvalResult(
            name=module_path,
            passed=False,
            message=f"Could not import '{module_name}': {exc}",
        )

    func = getattr(module, func_name, None)
    if func is None:
        return EvalResult(
            name=module_path,
            passed=False,
            message=f"Module '{module_name}' has no attribute '{func_name}'",
        )

    if not callable(func):
        return EvalResult(
            name=module_path,
            passed=False,
            message=f"'{func_name}' in '{module_name}' is not callable",
        )

    try:
        result = func(trace)
    except Exception as exc:
        return EvalResult(
            name=module_path,
            passed=False,
            message=f"Custom eval raised {type(exc).__name__}: {exc}",
        )

    if not isinstance(result, EvalResult):
        return EvalResult(
            name=module_path,
            passed=False,
            message=f"Custom eval must return EvalResult, got {type(result).__name__}",
        )

    return result
