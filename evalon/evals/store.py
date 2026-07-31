"""Writable SQLite persistence for Evalon's evaluation data."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from evalon.core.errors import EvalonStorageError
from evalon.core.json import sanitize, utc_now_iso
from evalon.evals.contracts import (
    Dataset,
    DatasetCase,
    DatasetVersion,
    EvalCaseRun,
    EvalRun,
    EvalSuite,
    EvaluationResult,
    EvaluationStatus,
    EvaluatorDefinition,
    EvaluatorRole,
    EvaluatorType,
)
from evalon.evals.schema import apply_eval_migrations


def _json(value: Any) -> str:
    return json.dumps(
        sanitize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _loads(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value is not None else default


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class EvalStore:
    """Own evaluation writes without exposing writable trace operations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock, self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                apply_eval_migrations(connection)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise EvalonStorageError(
                f"Failed to initialize Evalon evaluations in {self.path}"
            ) from exc

    @property
    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM eval_schema_migrations"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def create_dataset(self, dataset: Dataset) -> Dataset:
        now = utc_now_iso()
        dataset.created_at = dataset.created_at or now
        dataset.updated_at = dataset.updated_at or dataset.created_at
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO datasets
                    (id, name, description, current_version, archived_at,
                     metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        dataset.id,
                        dataset.name,
                        dataset.description,
                        dataset.archived_at,
                        _json(dataset.metadata),
                        dataset.created_at,
                        dataset.updated_at,
                    ),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(
                f"Failed to create dataset {dataset.name!r}"
            ) from exc
        return dataset

    def get_dataset(self, dataset_id: str) -> Dataset | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()
        return self._dataset_from_row(row) if row is not None else None

    def list_datasets(self, *, include_archived: bool = False) -> list[Dataset]:
        where = "1=1" if include_archived else "archived_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM datasets WHERE {where} ORDER BY updated_at DESC, name"
            ).fetchall()
        return [self._dataset_from_row(row) for row in rows]

    def archive_dataset(self, dataset_id: str) -> Dataset:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE datasets
                SET archived_at = COALESCE(archived_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, dataset_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise EvalonStorageError(f"Dataset {dataset_id!r} does not exist")
        dataset = self.get_dataset(dataset_id)
        if dataset is None:
            raise EvalonStorageError(f"Dataset {dataset_id!r} does not exist")
        return dataset

    def list_dataset_versions(self, dataset_id: str) -> list[DatasetVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT version FROM dataset_versions
                WHERE dataset_id = ?
                ORDER BY version DESC
                """,
                (dataset_id,),
            ).fetchall()
        versions: list[DatasetVersion] = []
        for row in rows:
            version = self.get_dataset_version(dataset_id, int(row["version"]))
            if version is not None:
                versions.append(version)
        return versions

    def create_dataset_version(self, version: DatasetVersion) -> DatasetVersion:
        now = utc_now_iso()
        version.created_at = version.created_at or now
        prepared_cases: list[tuple[DatasetCase, str]] = []
        seen_case_ids: set[str] = set()
        seen_inputs: set[str] = set()
        seen_traces: set[str] = set()

        for case in version.cases:
            case.dataset_version_id = version.id
            input_hash = _content_hash(case.input)
            if case.case_id in seen_case_ids:
                raise EvalonStorageError(
                    f"Duplicate case ID {case.case_id!r} in dataset version"
                )
            if input_hash in seen_inputs:
                raise EvalonStorageError(
                    f"Duplicate normalized input for case {case.case_id!r}"
                )
            if case.source_trace_id and case.source_trace_id in seen_traces:
                raise EvalonStorageError(
                    f"Trace {case.source_trace_id!r} is captured more than once"
                )
            seen_case_ids.add(case.case_id)
            seen_inputs.add(input_hash)
            if case.source_trace_id:
                seen_traces.add(case.source_trace_id)
            prepared_cases.append((case, input_hash))

        content_hash = _content_hash(
            [
                {
                    "case_id": case.case_id,
                    "input": case.input,
                    "expected_output": case.expected_output,
                    "reference_output": case.reference_output,
                    "tags": case.tags,
                    "notes": case.notes,
                    "metadata": case.metadata,
                    "source_trace_id": case.source_trace_id,
                    "source_project": case.source_project,
                    "source_captured_at": case.source_captured_at,
                }
                for case, _ in prepared_cases
            ]
        )

        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                dataset_row = connection.execute(
                    "SELECT current_version FROM datasets WHERE id = ?",
                    (version.dataset_id,),
                ).fetchone()
                if dataset_row is None:
                    raise EvalonStorageError(
                        f"Dataset {version.dataset_id!r} does not exist"
                    )
                expected_version = int(dataset_row["current_version"]) + 1
                if version.version != expected_version:
                    raise EvalonStorageError(
                        f"Dataset version must be {expected_version}, got {version.version}"
                    )

                connection.execute(
                    """
                    INSERT INTO dataset_versions
                    (id, dataset_id, version, content_hash, notes,
                     metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.id,
                        version.dataset_id,
                        version.version,
                        content_hash,
                        version.change_note,
                        _json(version.metadata),
                        version.created_at,
                    ),
                )
                for case, input_hash in prepared_cases:
                    connection.execute(
                        """
                        INSERT INTO dataset_cases
                        (id, dataset_version_id, case_id, input_json,
                         expected_json, reference_output_json, tags_json,
                         notes, metadata_json, source_trace_id, source_project,
                         captured_at, input_hash, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            case.id,
                            version.id,
                            case.case_id,
                            _json(case.input),
                            _json(case.expected_output),
                            _json(case.reference_output),
                            _json(case.tags),
                            case.notes,
                            _json(case.metadata),
                            case.source_trace_id,
                            case.source_project,
                            case.source_captured_at,
                            input_hash,
                            version.created_at,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE datasets
                    SET current_version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (version.version, now, version.dataset_id),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(
                f"Failed to create dataset version {version.dataset_id}:{version.version}"
            ) from exc
        return version

    def get_dataset_version(
        self,
        dataset_id: str,
        version: int | None = None,
    ) -> DatasetVersion | None:
        with self._connect() as connection:
            if version is None:
                row = connection.execute(
                    """
                    SELECT v.* FROM dataset_versions v
                    JOIN datasets d ON d.id = v.dataset_id
                    WHERE v.dataset_id = ? AND v.version = d.current_version
                    """,
                    (dataset_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM dataset_versions
                    WHERE dataset_id = ? AND version = ?
                    """,
                    (dataset_id, version),
                ).fetchone()
            if row is None:
                return None
            cases = [
                self._case_from_row(case_row)
                for case_row in connection.execute(
                    """
                    SELECT * FROM dataset_cases
                    WHERE dataset_version_id = ?
                    ORDER BY case_id
                    """,
                    (row["id"],),
                )
            ]
        return DatasetVersion(
            id=row["id"],
            dataset_id=row["dataset_id"],
            version=int(row["version"]),
            cases=cases,
            metadata=_loads(row["metadata_json"], {}),
            change_note=row["notes"],
            created_at=row["created_at"],
        )

    def find_duplicate_case(
        self,
        dataset_version_id: str,
        *,
        input: Any,
        source_trace_id: str | None = None,
    ) -> DatasetCase | None:
        input_hash = _content_hash(input)
        conditions = ["dataset_version_id = ?", "(input_hash = ?"]
        params: list[Any] = [dataset_version_id, input_hash]
        if source_trace_id:
            conditions[-1] += " OR source_trace_id = ?"
            params.append(source_trace_id)
        conditions[-1] += ")"
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM dataset_cases WHERE {' AND '.join(conditions)} LIMIT 1",
                params,
            ).fetchone()
        return self._case_from_row(row) if row is not None else None

    def create_suite(self, suite: EvalSuite) -> EvalSuite:
        now = utc_now_iso()
        suite.created_at = suite.created_at or now
        suite.updated_at = suite.updated_at or suite.created_at
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO eval_suites
                    (id, project, name, version, dataset_id, target_ref,
                     description, config_json, archived_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        suite.id,
                        suite.project,
                        suite.name,
                        suite.version,
                        suite.dataset_id,
                        suite.target_ref,
                        suite.description,
                        _json(suite.metadata),
                        suite.archived_at,
                        suite.created_at,
                        suite.updated_at,
                    ),
                )
                for evaluator in suite.evaluators:
                    self._insert_evaluator_conn(connection, suite.id, evaluator, now)
                connection.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(
                f"Failed to create eval suite {suite.name!r}"
            ) from exc
        return suite

    def get_suite(self, suite_id: str) -> EvalSuite | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM eval_suites WHERE id = ?",
                (suite_id,),
            ).fetchone()
            if row is None:
                return None
            evaluators = self._evaluators_for_suite_conn(connection, suite_id)
        return EvalSuite(
            id=row["id"],
            project=row["project"],
            name=row["name"],
            version=row["version"],
            dataset_id=row["dataset_id"],
            target_ref=row["target_ref"],
            description=row["description"],
            metadata=_loads(row["config_json"], {}),
            evaluators=evaluators,
            archived_at=row["archived_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_suites(
        self,
        *,
        project: str | None = None,
        include_archived: bool = False,
    ) -> list[EvalSuite]:
        conditions = ["1=1" if include_archived else "archived_at IS NULL"]
        params: list[Any] = []
        if project:
            conditions.append("project = ?")
            params.append(project)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM eval_suites
                WHERE {" AND ".join(conditions)}
                ORDER BY updated_at DESC, name
                """,
                params,
            ).fetchall()
        suites: list[EvalSuite] = []
        for row in rows:
            suite = self.get_suite(str(row["id"]))
            if suite is not None:
                suites.append(suite)
        return suites

    def create_run(self, run: EvalRun) -> EvalRun:
        now = utc_now_iso()
        run.created_at = run.created_at or now
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                suite_row = connection.execute(
                    "SELECT id FROM eval_suites WHERE id = ?",
                    (run.suite_id,),
                ).fetchone()
                if suite_row is None:
                    raise EvalonStorageError(
                        f"Eval suite {run.suite_id!r} does not exist"
                    )
                version_row = connection.execute(
                    """
                    SELECT id, dataset_id FROM dataset_versions
                    WHERE id = ?
                    """,
                    (run.dataset_version_id,),
                ).fetchone()
                if version_row is None or version_row["dataset_id"] != run.dataset_id:
                    raise EvalonStorageError(
                        f"Dataset version {run.dataset_version_id!r} does not belong "
                        f"to dataset {run.dataset_id!r}"
                    )

                evaluators = run.evaluator_snapshots or self._evaluators_for_suite_conn(
                    connection, run.suite_id
                )
                run.evaluator_snapshots = evaluators
                connection.execute(
                    """
                    INSERT INTO eval_runs
                    (id, suite_id, project, name, dataset_id, dataset_version_id,
                     baseline_run_id, status, target_snapshot_json,
                     evaluator_snapshot_json, environment_json, git_commit,
                     aggregate_score, candidate_cost_usd, judge_cost_usd,
                     started_at, ended_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        run.suite_id,
                        run.project,
                        run.name,
                        run.dataset_id,
                        run.dataset_version_id,
                        run.baseline_run_id,
                        run.status.value,
                        _json(run.target_configuration),
                        _json([item.to_dict() for item in evaluators]),
                        _json(run.environment),
                        run.git_commit,
                        run.aggregate_score,
                        run.candidate_cost_usd,
                        run.judge_cost_usd,
                        run.started_at,
                        run.ended_at,
                        run.created_at,
                    ),
                )

                case_rows = connection.execute(
                    """
                    SELECT * FROM dataset_cases
                    WHERE dataset_version_id = ?
                    ORDER BY case_id
                    """,
                    (run.dataset_version_id,),
                ).fetchall()
                cases_by_id = {row["case_id"]: row for row in case_rows}
                if not run.case_runs:
                    run.case_runs = [
                        EvalCaseRun(
                            eval_run_id=run.id,
                            case_id=row["case_id"],
                            dataset_case_id=row["id"],
                            case_input=_loads(row["input_json"]),
                            expected_output=_loads(row["expected_json"]),
                            case_metadata=_loads(row["metadata_json"], {}),
                        )
                        for row in case_rows
                    ]

                for case_run in run.case_runs:
                    case_run.eval_run_id = run.id
                    source = cases_by_id.get(case_run.case_id)
                    if source is None:
                        raise EvalonStorageError(
                            f"Case {case_run.case_id!r} is not in dataset version"
                        )
                    case_run.dataset_case_id = case_run.dataset_case_id or source["id"]
                    if case_run.case_input is None:
                        case_run.case_input = _loads(source["input_json"])
                    if case_run.expected_output is None:
                        case_run.expected_output = _loads(source["expected_json"])
                    if not case_run.case_metadata:
                        case_run.case_metadata = _loads(source["metadata_json"], {})
                    self._insert_case_run_conn(connection, case_run, now)

                    existing = {result.evaluator_id for result in case_run.results}
                    for evaluator in evaluators:
                        if evaluator.id not in existing:
                            case_run.results.append(
                                EvaluationResult(
                                    evaluator_id=evaluator.id,
                                    evaluator_name=evaluator.name,
                                    evaluator_version=evaluator.version,
                                    evaluator_type=evaluator.evaluator_type,
                                    required=evaluator.required,
                                    eval_case_run_id=case_run.id,
                                )
                            )
                    for result in case_run.results:
                        result.eval_case_run_id = case_run.id
                        self._upsert_result_conn(connection, result, now)
                connection.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to create eval run {run.id!r}") from exc
        return run

    def update_case_run(self, case_run: EvalCaseRun) -> None:
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE eval_case_runs
                    SET status = ?, candidate_trace_id = ?, output_json = ?,
                        metadata_json = ?, error_json = ?, latency_ms = ?,
                        candidate_cost_usd = ?, input_tokens = ?,
                        output_tokens = ?, aggregate_score = ?,
                        started_at = ?, ended_at = ?
                    WHERE id = ?
                    """,
                    (
                        case_run.status.value,
                        case_run.candidate_trace_id,
                        _json(case_run.candidate_output),
                        _json(case_run.case_metadata),
                        _json(case_run.candidate_error)
                        if case_run.candidate_error is not None
                        else None,
                        case_run.metrics.get("latency_ms"),
                        case_run.metrics.get("cost_usd", 0),
                        int(case_run.metrics.get("input_tokens", 0)),
                        int(case_run.metrics.get("output_tokens", 0)),
                        case_run.aggregate_score,
                        case_run.started_at,
                        case_run.ended_at,
                        case_run.id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise EvalonStorageError(
                        f"Eval case run {case_run.id!r} does not exist"
                    )
                for result in case_run.results:
                    result.eval_case_run_id = case_run.id
                    self._upsert_result_conn(connection, result, utc_now_iso())
                connection.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(
                f"Failed to update eval case run {case_run.id!r}"
            ) from exc

    def update_run(self, run: EvalRun) -> None:
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE eval_runs
                    SET status = ?, aggregate_score = ?,
                        candidate_cost_usd = ?, judge_cost_usd = ?,
                        started_at = ?, ended_at = ?
                    WHERE id = ?
                    """,
                    (
                        run.status.value,
                        run.aggregate_score,
                        run.candidate_cost_usd,
                        run.judge_cost_usd,
                        run.started_at,
                        run.ended_at,
                        run.id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise EvalonStorageError(f"Eval run {run.id!r} does not exist")
                connection.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to update eval run {run.id!r}") from exc

    def request_cancellation(self, run_id: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE eval_runs SET cancel_requested = 1 WHERE id = ?",
                (run_id,),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise EvalonStorageError(f"Eval run {run_id!r} does not exist")

    def cancellation_requested(self, run_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM eval_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return bool(row["cancel_requested"]) if row is not None else False

    def list_runs(
        self,
        *,
        project: str | None = None,
        suite_id: str | None = None,
        dataset_id: str | None = None,
        status: EvaluationStatus | str | None = None,
        limit: int = 100,
    ) -> list[EvalRun]:
        conditions = ["1=1"]
        params: list[Any] = []
        for column, value in (
            ("project", project),
            ("suite_id", suite_id),
            ("dataset_id", dataset_id),
        ):
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        if status:
            conditions.append("status = ?")
            params.append(EvaluationStatus(status).value)
        params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM eval_runs
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        runs: list[EvalRun] = []
        for row in rows:
            run = self.get_run(str(row["id"]))
            if run is not None:
                runs.append(run)
        return runs

    def set_baseline(self, suite_id: str, run_id: str) -> None:
        run = self.get_run(run_id)
        if run is None or run.suite_id != suite_id:
            raise EvalonStorageError(
                f"Run {run_id!r} is not a run for suite {suite_id!r}"
            )
        if run.status not in {EvaluationStatus.PASSED, EvaluationStatus.FAILED}:
            raise EvalonStorageError("Only completed scored runs can be baselines")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO suite_baselines (suite_id, eval_run_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(suite_id) DO UPDATE SET
                    eval_run_id = excluded.eval_run_id,
                    updated_at = excluded.updated_at
                """,
                (suite_id, run_id, utc_now_iso()),
            )
            connection.commit()

    def get_baseline(self, suite_id: str) -> EvalRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT eval_run_id FROM suite_baselines WHERE suite_id = ?",
                (suite_id,),
            ).fetchone()
        return self.get_run(str(row["eval_run_id"])) if row is not None else None

    def record_dynamic_evaluation(
        self,
        *,
        source_trace_id: str,
        project: str,
        evaluator_name: str,
        evaluator_version: str,
        eval_run_id: str,
        evaluation_result_id: str | None,
        force: bool = False,
    ) -> str:
        record_id = f"dynamic_{uuid.uuid4().hex}"
        conflict = (
            """
            ON CONFLICT(source_trace_id, evaluator_name, evaluator_version)
            DO UPDATE SET
                id = excluded.id,
                project = excluded.project,
                eval_run_id = excluded.eval_run_id,
                evaluation_result_id = excluded.evaluation_result_id,
                created_at = excluded.created_at
            """
            if force
            else ""
        )
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    f"""
                    INSERT INTO dynamic_evaluations
                    (id, source_trace_id, project, evaluator_name, evaluator_version,
                     eval_run_id, evaluation_result_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    {conflict}
                    """,
                    (
                        record_id,
                        source_trace_id,
                        project,
                        evaluator_name,
                        evaluator_version,
                        eval_run_id,
                        evaluation_result_id,
                        utc_now_iso(),
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise EvalonStorageError(
                f"Trace {source_trace_id!r} was already evaluated by "
                f"{evaluator_name}:{evaluator_version}"
            ) from exc
        return record_id

    def has_dynamic_evaluation(
        self,
        source_trace_id: str,
        evaluator_name: str,
        evaluator_version: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM dynamic_evaluations
                WHERE source_trace_id = ? AND evaluator_name = ?
                  AND evaluator_version = ?
                """,
                (source_trace_id, evaluator_name, evaluator_version),
            ).fetchone()
        return row is not None

    def validate_storage(self) -> list[dict[str, Any]]:
        """Return broken references and inconsistent run lifecycle records."""
        issues: list[dict[str, Any]] = []
        with self._connect() as connection:
            for row in connection.execute("PRAGMA foreign_key_check").fetchall():
                issues.append(
                    {
                        "type": "broken_foreign_key",
                        "table": row[0],
                        "rowid": row[1],
                        "parent": row[2],
                    }
                )
            incomplete = connection.execute(
                """
                SELECT r.id AS run_id, r.status AS run_status,
                       c.id AS case_run_id, c.status AS case_status
                FROM eval_runs r
                JOIN eval_case_runs c ON c.eval_run_id = r.id
                WHERE r.status NOT IN ('pending', 'running')
                  AND c.status IN ('pending', 'running')
                """
            ).fetchall()
            issues.extend(
                {
                    "type": "terminal_run_has_incomplete_case",
                    "run_id": row["run_id"],
                    "run_status": row["run_status"],
                    "case_run_id": row["case_run_id"],
                    "case_status": row["case_status"],
                }
                for row in incomplete
            )
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "traces" in tables:
                missing_candidate = connection.execute(
                    """
                    SELECT c.id, c.candidate_trace_id FROM eval_case_runs c
                    LEFT JOIN traces t ON t.id = c.candidate_trace_id
                    WHERE c.candidate_trace_id IS NOT NULL AND t.id IS NULL
                    """
                ).fetchall()
                issues.extend(
                    {
                        "type": "missing_candidate_trace",
                        "case_run_id": row["id"],
                        "trace_id": row["candidate_trace_id"],
                    }
                    for row in missing_candidate
                )
                missing_judge = connection.execute(
                    """
                    SELECT e.id, e.judge_trace_id FROM evaluation_results e
                    LEFT JOIN traces t ON t.id = e.judge_trace_id
                    WHERE e.judge_trace_id IS NOT NULL AND t.id IS NULL
                    """
                ).fetchall()
                issues.extend(
                    {
                        "type": "missing_judge_trace",
                        "evaluation_result_id": row["id"],
                        "trace_id": row["judge_trace_id"],
                    }
                    for row in missing_judge
                )
        return issues

    def get_run(self, run_id: str) -> EvalRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM eval_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            case_rows = connection.execute(
                """
                SELECT * FROM eval_case_runs
                WHERE eval_run_id = ?
                ORDER BY case_id
                """,
                (run_id,),
            ).fetchall()
            case_runs = [
                self._case_run_from_row(
                    case_row,
                    connection.execute(
                        """
                        SELECT * FROM evaluation_results
                        WHERE eval_case_run_id = ?
                        ORDER BY evaluator_name
                        """,
                        (case_row["id"],),
                    ).fetchall(),
                )
                for case_row in case_rows
            ]
        snapshots = [
            EvaluatorDefinition.from_dict(item)
            for item in _loads(row["evaluator_snapshot_json"], [])
        ]
        return EvalRun(
            id=row["id"],
            project=row["project"],
            name=row["name"],
            dataset_id=row["dataset_id"],
            dataset_version_id=row["dataset_version_id"],
            suite_id=row["suite_id"],
            baseline_run_id=row["baseline_run_id"],
            status=EvaluationStatus(row["status"]),
            target_configuration=_loads(row["target_snapshot_json"], {}),
            evaluator_snapshots=snapshots,
            environment=_loads(row["environment_json"], {}),
            git_commit=row["git_commit"],
            case_runs=case_runs,
            aggregate_score=row["aggregate_score"],
            candidate_cost_usd=float(row["candidate_cost_usd"] or 0),
            judge_cost_usd=float(row["judge_cost_usd"] or 0),
            created_at=row["created_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )

    @staticmethod
    def _dataset_from_row(row: sqlite3.Row) -> Dataset:
        return Dataset(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            metadata=_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
        )

    @staticmethod
    def _case_from_row(row: sqlite3.Row) -> DatasetCase:
        return DatasetCase(
            id=row["id"],
            case_id=row["case_id"],
            dataset_version_id=row["dataset_version_id"],
            input=_loads(row["input_json"]),
            expected_output=_loads(row["expected_json"]),
            reference_output=_loads(row["reference_output_json"]),
            tags=_loads(row["tags_json"], []),
            notes=row["notes"],
            metadata=_loads(row["metadata_json"], {}),
            source_trace_id=row["source_trace_id"],
            source_project=row["source_project"],
            source_captured_at=row["captured_at"],
        )

    @staticmethod
    def _insert_evaluator_conn(
        connection: sqlite3.Connection,
        suite_id: str,
        evaluator: EvaluatorDefinition,
        now: str,
    ) -> None:
        created_at = evaluator.created_at or now
        evaluator.created_at = created_at
        connection.execute(
            """
            INSERT INTO evaluator_definitions
            (id, suite_id, name, version, evaluator_type, required,
             score_min, score_max, pass_threshold, config_json,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluator.id,
                suite_id,
                evaluator.name,
                evaluator.version,
                evaluator.evaluator_type.value,
                int(evaluator.required),
                evaluator.score_min,
                evaluator.score_max,
                evaluator.pass_threshold,
                _json(evaluator.configuration),
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def _evaluators_for_suite_conn(
        connection: sqlite3.Connection,
        suite_id: str,
    ) -> list[EvaluatorDefinition]:
        rows = connection.execute(
            """
            SELECT * FROM evaluator_definitions
            WHERE suite_id = ?
            ORDER BY created_at, name
            """,
            (suite_id,),
        ).fetchall()
        return [
            EvaluatorDefinition(
                id=row["id"],
                name=row["name"],
                version=row["version"],
                evaluator_type=EvaluatorType(row["evaluator_type"]),
                role=(
                    EvaluatorRole.REQUIRED
                    if row["required"]
                    else EvaluatorRole.ADVISORY
                ),
                score_min=float(row["score_min"]),
                score_max=float(row["score_max"]),
                pass_threshold=float(row["pass_threshold"]),
                configuration=_loads(row["config_json"], {}),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _insert_case_run_conn(
        connection: sqlite3.Connection,
        case_run: EvalCaseRun,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO eval_case_runs
            (id, eval_run_id, dataset_case_id, case_id, status,
             candidate_trace_id, input_json, expected_json, output_json,
             metadata_json, error_json, latency_ms, candidate_cost_usd,
             input_tokens, output_tokens, started_at, ended_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_run.id,
                case_run.eval_run_id,
                case_run.dataset_case_id,
                case_run.case_id,
                case_run.status.value,
                case_run.candidate_trace_id,
                _json(case_run.case_input),
                _json(case_run.expected_output),
                _json(case_run.candidate_output),
                _json(case_run.case_metadata),
                _json(case_run.candidate_error)
                if case_run.candidate_error is not None
                else None,
                case_run.metrics.get("latency_ms"),
                case_run.metrics.get("cost_usd", 0),
                int(case_run.metrics.get("input_tokens", 0)),
                int(case_run.metrics.get("output_tokens", 0)),
                case_run.started_at,
                case_run.ended_at,
                now,
            ),
        )

    @staticmethod
    def _upsert_result_conn(
        connection: sqlite3.Connection,
        result: EvaluationResult,
        now: str,
    ) -> None:
        if result.eval_case_run_id is None:
            raise EvalonStorageError("EvaluationResult requires eval_case_run_id")
        definition = connection.execute(
            "SELECT * FROM evaluator_definitions WHERE id = ?",
            (result.evaluator_id,),
        ).fetchone()
        if definition is None:
            raise EvalonStorageError(
                f"Evaluator definition {result.evaluator_id!r} does not exist"
            )
        name = result.evaluator_name or definition["name"]
        version = result.evaluator_version or definition["version"]
        evaluator_type = (
            result.evaluator_type.value
            if result.evaluator_type is not None
            else definition["evaluator_type"]
        )
        required = (
            result.required
            if result.required is not None
            else bool(definition["required"])
        )
        result.evaluator_name = name
        result.evaluator_version = version
        result.evaluator_type = EvaluatorType(evaluator_type)
        result.created_at = result.created_at or now
        connection.execute(
            """
            INSERT INTO evaluation_results
            (id, eval_case_run_id, evaluator_definition_id, evaluator_name,
             evaluator_version, evaluator_type, required, status, score,
             passed, label, reason, evidence_json, metadata_json, error_json,
             judge_trace_id, duration_ms, cost_usd, input_tokens,
             output_tokens, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(eval_case_run_id, evaluator_name, evaluator_version)
            DO UPDATE SET
                status=excluded.status,
                score=excluded.score,
                passed=excluded.passed,
                label=excluded.label,
                reason=excluded.reason,
                evidence_json=excluded.evidence_json,
                metadata_json=excluded.metadata_json,
                error_json=excluded.error_json,
                judge_trace_id=excluded.judge_trace_id,
                duration_ms=excluded.duration_ms,
                cost_usd=excluded.cost_usd,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens
            """,
            (
                result.id,
                result.eval_case_run_id,
                result.evaluator_id,
                name,
                version,
                evaluator_type,
                int(required),
                result.status.value,
                result.score,
                int(result.passed) if result.passed is not None else None,
                result.label,
                result.reason,
                _json(result.evidence),
                _json(result.metadata),
                _json(result.evaluator_error)
                if result.evaluator_error is not None
                else None,
                result.judge_trace_id,
                result.duration_ms,
                result.cost_usd,
                result.input_tokens,
                result.output_tokens,
                result.created_at,
            ),
        )

    @staticmethod
    def _case_run_from_row(
        row: sqlite3.Row,
        result_rows: list[sqlite3.Row],
    ) -> EvalCaseRun:
        results = [
            EvaluationResult(
                id=result["id"],
                evaluator_id=result["evaluator_definition_id"],
                evaluator_name=result["evaluator_name"],
                evaluator_version=result["evaluator_version"],
                evaluator_type=EvaluatorType(result["evaluator_type"]),
                score=result["score"],
                passed=(
                    bool(result["passed"]) if result["passed"] is not None else None
                ),
                label=result["label"],
                reason=result["reason"],
                evidence=_loads(result["evidence_json"], []),
                metadata=_loads(result["metadata_json"], {}),
                evaluator_error=_loads(result["error_json"]),
                skipped=result["status"] == EvaluationStatus.SKIPPED.value,
                required=bool(result["required"]),
                eval_case_run_id=result["eval_case_run_id"],
                judge_trace_id=result["judge_trace_id"],
                duration_ms=result["duration_ms"],
                cost_usd=float(result["cost_usd"] or 0),
                input_tokens=int(result["input_tokens"] or 0),
                output_tokens=int(result["output_tokens"] or 0),
                created_at=result["created_at"],
            )
            for result in result_rows
        ]
        metrics = {
            "latency_ms": float(row["latency_ms"] or 0),
            "cost_usd": float(row["candidate_cost_usd"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
        }
        return EvalCaseRun(
            id=row["id"],
            eval_run_id=row["eval_run_id"],
            case_id=row["case_id"],
            dataset_case_id=row["dataset_case_id"],
            case_input=_loads(row["input_json"]),
            expected_output=_loads(row["expected_json"]),
            case_metadata=_loads(row["metadata_json"], {}),
            status=EvaluationStatus(row["status"]),
            candidate_output=_loads(row["output_json"]),
            candidate_error=_loads(row["error_json"]),
            candidate_trace_id=row["candidate_trace_id"],
            results=results,
            metrics=metrics,
            aggregate_score=row["aggregate_score"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )
