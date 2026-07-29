"""CLI for running LLM judge evaluations against traces in SQLite."""

from __future__ import annotations

import argparse
import json
import sys

from evalon.evals.dynamic import PREDEFINED_JUDGES, run_llm_judge
from evalon.evals.models import EvalResult, LLMJudgeConfig
from evalon.paths import default_db_path
from evalon.storage import SqliteStorage


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evalon-judge",
        description="Run LLM-as-judge evaluations on evalon traces.",
    )
    parser.add_argument(
        "--db",
        default=default_db_path(),
        help="Path to the SQLite database (default: ~/.evalon/evalon-runs.sqlite)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model for the judge (default: EVALON_JUDGE_MODEL env var or deepseek/deepseek-v4-flash)",
    )
    parser.add_argument(
        "--trace",
        default=None,
        help="Run judge on a specific trace ID",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Filter traces by project name",
    )
    parser.add_argument(
        "--judge",
        default="user_goal_achieved",
        help="Predefined judge name (default: user_goal_achieved)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Custom judge prompt (overrides --judge)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of traces to evaluate (default: 100)",
    )
    parser.add_argument(
        "--list-judges",
        action="store_true",
        help="List available predefined judges and exit",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list_judges:
        print("Predefined judges:")
        for name in PREDEFINED_JUDGES:
            print(f"  - {name}")
        return

    storage = SqliteStorage(args.db)

    # Resolve traces
    if args.trace:
        trace = storage.get_trace(args.trace)
        if trace is None:
            sys.stderr.write(f"Trace not found: {args.trace}\n")
            sys.exit(1)
        traces = [trace]
    else:
        traces = storage.query(project=args.project, limit=args.limit)
        if not traces:
            sys.stderr.write("No traces found.\n")
            sys.exit(1)

    # Build judge config
    if args.prompt:
        judge = LLMJudgeConfig(name="custom", prompt=args.prompt, model=args.model)
    else:
        judge = LLMJudgeConfig(name=args.judge, predefined=args.judge, model=args.model)

    # Run judge on each trace
    results: list[dict] = []
    for trace in traces:
        trace_id = trace["id"]
        result = run_llm_judge(trace, judge)

        # Store result
        storage.write_eval_results(trace_id, [result])

        results.append({
            "trace_id": trace_id,
            "name": result.name,
            "passed": result.passed,
            "message": result.message,
        })

    # Output
    if args.format == "json":
        print(json.dumps(results, indent=2))
    else:
        passed = sum(1 for r in results if r["passed"])
        failed = len(results) - passed
        print(f"\n{'='*60}")
        print(f"  LLM Judge: {judge.name or judge.predefined}")
        print(f"  Model: {judge.model or 'EVALON_JUDGE_MODEL'}")
        print(f"  Traces evaluated: {len(results)}")
        print(f"  Passed: {passed}  Failed: {failed}")
        print(f"{'='*60}\n")
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            print(f"  [{status}] {r['trace_id']}")
            print(f"         {r['message'][:200]}")
            print()


if __name__ == "__main__":
    main()
