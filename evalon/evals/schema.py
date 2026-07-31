"""Versioned SQLite schema for Evalon's evaluation data."""

from __future__ import annotations

import sqlite3

LATEST_EVAL_SCHEMA_VERSION = 3

_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "create evaluation tables",
        """
        CREATE TABLE IF NOT EXISTS eval_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS datasets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            current_version INTEGER NOT NULL DEFAULT 0,
            archived_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dataset_versions (
            id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            notes TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE RESTRICT,
            UNIQUE(dataset_id, version)
        );

        CREATE TABLE IF NOT EXISTS dataset_cases (
            id TEXT PRIMARY KEY,
            dataset_version_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            input_json TEXT,
            expected_json TEXT,
            reference_output_json TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            source_trace_id TEXT,
            source_project TEXT,
            captured_at TEXT,
            input_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (dataset_version_id)
                REFERENCES dataset_versions(id) ON DELETE RESTRICT,
            UNIQUE(dataset_version_id, case_id)
        );

        CREATE TABLE IF NOT EXISTS eval_suites (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            description TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            archived_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE RESTRICT,
            UNIQUE(project, name, version)
        );

        CREATE TABLE IF NOT EXISTS evaluator_definitions (
            id TEXT PRIMARY KEY,
            suite_id TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            evaluator_type TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            score_min REAL,
            score_max REAL,
            pass_threshold REAL,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (suite_id) REFERENCES eval_suites(id) ON DELETE CASCADE,
            UNIQUE(suite_id, name, version)
        );

        CREATE TABLE IF NOT EXISTS eval_runs (
            id TEXT PRIMARY KEY,
            suite_id TEXT,
            project TEXT NOT NULL,
            name TEXT,
            dataset_id TEXT NOT NULL,
            dataset_version_id TEXT NOT NULL,
            baseline_run_id TEXT,
            status TEXT NOT NULL,
            target_snapshot_json TEXT NOT NULL DEFAULT '{}',
            evaluator_snapshot_json TEXT NOT NULL DEFAULT '[]',
            environment_json TEXT NOT NULL DEFAULT '{}',
            git_commit TEXT,
            aggregate_score REAL,
            candidate_cost_usd REAL NOT NULL DEFAULT 0,
            judge_cost_usd REAL NOT NULL DEFAULT 0,
            started_at TEXT,
            ended_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (suite_id) REFERENCES eval_suites(id) ON DELETE SET NULL,
            FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE RESTRICT,
            FOREIGN KEY (dataset_version_id)
                REFERENCES dataset_versions(id) ON DELETE RESTRICT,
            FOREIGN KEY (baseline_run_id) REFERENCES eval_runs(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS eval_case_runs (
            id TEXT PRIMARY KEY,
            eval_run_id TEXT NOT NULL,
            dataset_case_id TEXT,
            case_id TEXT NOT NULL,
            status TEXT NOT NULL,
            candidate_trace_id TEXT,
            input_json TEXT,
            expected_json TEXT,
            output_json TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT,
            latency_ms REAL,
            candidate_cost_usd REAL NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            ended_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (dataset_case_id)
                REFERENCES dataset_cases(id) ON DELETE SET NULL,
            UNIQUE(eval_run_id, case_id)
        );

        CREATE TABLE IF NOT EXISTS evaluation_results (
            id TEXT PRIMARY KEY,
            eval_case_run_id TEXT NOT NULL,
            evaluator_definition_id TEXT,
            evaluator_name TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            evaluator_type TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            score REAL,
            passed INTEGER,
            label TEXT,
            reason TEXT,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT,
            judge_trace_id TEXT,
            duration_ms REAL,
            cost_usd REAL NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (eval_case_run_id)
                REFERENCES eval_case_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (evaluator_definition_id)
                REFERENCES evaluator_definitions(id) ON DELETE SET NULL,
            UNIQUE(eval_case_run_id, evaluator_name, evaluator_version)
        );

        CREATE INDEX IF NOT EXISTS idx_dataset_versions_dataset
            ON dataset_versions(dataset_id, version DESC);
        CREATE INDEX IF NOT EXISTS idx_dataset_cases_version
            ON dataset_cases(dataset_version_id, case_id);
        CREATE INDEX IF NOT EXISTS idx_dataset_cases_source_trace
            ON dataset_cases(source_trace_id);
        CREATE INDEX IF NOT EXISTS idx_dataset_cases_input_hash
            ON dataset_cases(dataset_version_id, input_hash);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_cases_unique_input
            ON dataset_cases(dataset_version_id, input_hash);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_cases_unique_source_trace
            ON dataset_cases(dataset_version_id, source_trace_id)
            WHERE source_trace_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_eval_suites_dataset
            ON eval_suites(dataset_id);
        CREATE INDEX IF NOT EXISTS idx_evaluator_definitions_suite
            ON evaluator_definitions(suite_id, name);
        CREATE INDEX IF NOT EXISTS idx_eval_runs_project_created
            ON eval_runs(project, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset
            ON eval_runs(dataset_id, dataset_version_id);
        CREATE INDEX IF NOT EXISTS idx_eval_runs_status
            ON eval_runs(status);
        CREATE INDEX IF NOT EXISTS idx_eval_case_runs_run_status
            ON eval_case_runs(eval_run_id, status);
        CREATE INDEX IF NOT EXISTS idx_eval_case_runs_trace
            ON eval_case_runs(candidate_trace_id);
        CREATE INDEX IF NOT EXISTS idx_evaluation_results_case
            ON evaluation_results(eval_case_run_id);
        CREATE INDEX IF NOT EXISTS idx_evaluation_results_evaluator
            ON evaluation_results(evaluator_name, evaluator_version);
        CREATE INDEX IF NOT EXISTS idx_evaluation_results_status
            ON evaluation_results(status);
        CREATE INDEX IF NOT EXISTS idx_evaluation_results_judge_trace
            ON evaluation_results(judge_trace_id);
        """,
    ),
    (
        2,
        "add evaluation control and dynamic trace records",
        """
        ALTER TABLE eval_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE eval_case_runs ADD COLUMN aggregate_score REAL;

        CREATE TABLE IF NOT EXISTS suite_baselines (
            suite_id TEXT PRIMARY KEY,
            eval_run_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (suite_id) REFERENCES eval_suites(id) ON DELETE CASCADE,
            FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS dynamic_evaluations (
            id TEXT PRIMARY KEY,
            source_trace_id TEXT NOT NULL,
            project TEXT NOT NULL,
            evaluator_name TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            eval_run_id TEXT NOT NULL,
            evaluation_result_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (evaluation_result_id)
                REFERENCES evaluation_results(id) ON DELETE SET NULL,
            UNIQUE(source_trace_id, evaluator_name, evaluator_version)
        );

        CREATE INDEX IF NOT EXISTS idx_dynamic_evaluations_project_created
            ON dynamic_evaluations(project, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_dynamic_evaluations_source
            ON dynamic_evaluations(source_trace_id);
        """,
    ),
    (
        3,
        "add reusable binary judge definitions",
        """
        CREATE TABLE IF NOT EXISTS judge_definitions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            rubric TEXT NOT NULL,
            included_context_fields_json TEXT NOT NULL DEFAULT '[]',
            temperature REAL NOT NULL DEFAULT 0,
            timeout_seconds REAL,
            base_url TEXT,
            max_retries INTEGER NOT NULL DEFAULT 2,
            retry_delay_seconds REAL NOT NULL DEFAULT 0.25,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            archived_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(name, version)
        );

        CREATE TABLE IF NOT EXISTS suite_judge_bindings (
            suite_id TEXT NOT NULL,
            judge_definition_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'required',
            created_at TEXT NOT NULL,
            PRIMARY KEY (suite_id, judge_definition_id),
            FOREIGN KEY (suite_id) REFERENCES eval_suites(id) ON DELETE CASCADE,
            FOREIGN KEY (judge_definition_id)
                REFERENCES judge_definitions(id) ON DELETE RESTRICT
        );

        ALTER TABLE evaluation_results ADD COLUMN judge_definition_id TEXT
            REFERENCES judge_definitions(id) ON DELETE SET NULL;

        CREATE INDEX IF NOT EXISTS idx_judge_definitions_name
            ON judge_definitions(name, version DESC);
        CREATE INDEX IF NOT EXISTS idx_suite_judge_bindings_suite
            ON suite_judge_bindings(suite_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_evaluation_results_judge_definition
            ON evaluation_results(judge_definition_id);
        """,
    ),
)


def apply_eval_migrations(connection: sqlite3.Connection) -> int:
    """Apply pending evaluation migrations and return the current version."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM eval_schema_migrations ORDER BY version"
        )
    }

    for version, name, script in _MIGRATIONS:
        if version in applied:
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _iter_statements(script):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO eval_schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise

    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM eval_schema_migrations"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _iter_statements(script: str) -> list[str]:
    """Split a migration script without giving sqlite3 permission to auto-commit."""
    statements: list[str] = []
    pending = ""
    for line in script.splitlines():
        pending = f"{pending}\n{line}" if pending else line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise ValueError("incomplete SQLite migration statement")
    return statements
