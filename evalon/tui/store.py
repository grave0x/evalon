"""Read-only queries for the Evalon observability TUI."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote


@dataclass(slots=True)
class TraceRow:
    id: str
    project: str
    name: str
    status: str
    session_id: str | None
    started_at: str | None
    latency_ms: float
    cost_usd: float
    input_tokens: float
    output_tokens: float
    span_count: int


@dataclass(slots=True)
class ProjectRow:
    name: str
    trace_count: int
    session_count: int
    running_count: int
    error_count: int
    p50_latency_ms: float
    p95_latency_ms: float
    total_cost_usd: float
    total_tokens: int
    last_activity: str | None

    @property
    def error_rate(self) -> float:
        return self.error_count / self.trace_count if self.trace_count else 0.0


@dataclass(slots=True)
class ProjectSnapshot:
    projects: list[ProjectRow] = field(default_factory=list)

    @property
    def trace_count(self) -> int:
        return sum(project.trace_count for project in self.projects)

    @property
    def session_count(self) -> int:
        return sum(project.session_count for project in self.projects)

    @property
    def running_count(self) -> int:
        return sum(project.running_count for project in self.projects)

    @property
    def error_count(self) -> int:
        return sum(project.error_count for project in self.projects)

    @property
    def total_cost_usd(self) -> float:
        return sum(project.total_cost_usd for project in self.projects)

    @property
    def total_tokens(self) -> int:
        return sum(project.total_tokens for project in self.projects)

    @property
    def error_rate(self) -> float:
        return self.error_count / self.trace_count if self.trace_count else 0.0


@dataclass(slots=True)
class Snapshot:
    traces: list[TraceRow] = field(default_factory=list)
    trace_count: int = 0
    session_count: int = 0
    running_count: int = 0
    error_count: int = 0
    p95_latency_ms: float = 0
    total_cost_usd: float = 0
    total_tokens: int = 0
    latency_samples: list[float] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.error_count / self.trace_count if self.trace_count else 0.0


@dataclass(slots=True)
class TraceDetail:
    trace: dict[str, Any]
    spans: list[dict[str, Any]]
    events: list[dict[str, Any]]
    metrics: dict[str, float]


class ObservabilityStore:
    """Open an Evalon SQLite database without ever creating or mutating it."""

    REQUIRED_TABLES = {"traces", "spans", "events", "metrics"}

    def __init__(self, path: str | Path, project: str | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.project = project
        if not self.path.is_file():
            raise FileNotFoundError(f"trace database not found: {self.path}")
        with self.connect() as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        missing = sorted(self.REQUIRED_TABLES - tables)
        if missing:
            raise ValueError(
                f"{self.path} is not an Evalon trace database; missing: {', '.join(missing)}"
            )
        self.has_sessions = "sessions" in tables

    def connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def projects(self) -> ProjectSnapshot:
        """Return one observability rollup per project."""
        with self.connect() as connection:
            trace_rows = connection.execute(
                """
                SELECT
                    project,
                    COUNT(*) AS trace_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)
                        AS running_count,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)
                        AS error_count,
                    MAX(started_at) AS last_activity
                FROM traces
                GROUP BY project
                ORDER BY last_activity DESC, project
                """
            ).fetchall()
            metric_rows = connection.execute(
                """
                SELECT t.project, m.name, m.value
                FROM metrics m
                JOIN traces t ON t.id = m.trace_id
                WHERE m.name IN (
                    'latency_ms', 'cost_usd', 'input_tokens', 'output_tokens'
                )
                """
            ).fetchall()
            session_counts: dict[str, int] = {}
            if self.has_sessions:
                session_counts = {
                    str(row["project"]): int(row["session_count"])
                    for row in connection.execute(
                        """
                        SELECT project, COUNT(*) AS session_count
                        FROM sessions
                        GROUP BY project
                        """
                    )
                }

        metrics_by_project: dict[str, dict[str, list[float]]] = {}
        for row in metric_rows:
            project_metrics = metrics_by_project.setdefault(str(row["project"]), {})
            project_metrics.setdefault(str(row["name"]), []).append(float(row["value"]))

        projects: list[ProjectRow] = []
        for row in trace_rows:
            name = str(row["project"])
            metrics = metrics_by_project.get(name, {})
            latencies = sorted(value for value in metrics.get("latency_ms", []) if value > 0)
            projects.append(
                ProjectRow(
                    name=name,
                    trace_count=int(row["trace_count"] or 0),
                    session_count=session_counts.get(name, 0),
                    running_count=int(row["running_count"] or 0),
                    error_count=int(row["error_count"] or 0),
                    p50_latency_ms=self._percentile(latencies, 0.50),
                    p95_latency_ms=self._percentile(latencies, 0.95),
                    total_cost_usd=sum(metrics.get("cost_usd", [])),
                    total_tokens=round(
                        sum(metrics.get("input_tokens", []))
                        + sum(metrics.get("output_tokens", []))
                    ),
                    last_activity=row["last_activity"],
                )
            )
        return ProjectSnapshot(projects=projects)

    def snapshot(self, limit: int = 200) -> Snapshot:
        where, params = self._project_where("t")
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    t.id, t.project, t.name, t.status, t.session_id, t.started_at,
                    COALESCE(MAX(CASE WHEN m.name = 'latency_ms' THEN m.value END), 0)
                        AS latency_ms,
                    COALESCE(MAX(CASE WHEN m.name = 'cost_usd' THEN m.value END), 0)
                        AS cost_usd,
                    COALESCE(MAX(CASE WHEN m.name = 'input_tokens' THEN m.value END), 0)
                        AS input_tokens,
                    COALESCE(MAX(CASE WHEN m.name = 'output_tokens' THEN m.value END), 0)
                        AS output_tokens,
                    COUNT(DISTINCT s.id) AS span_count
                FROM traces t
                LEFT JOIN metrics m ON m.trace_id = t.id
                LEFT JOIN spans s ON s.trace_id = t.id
                WHERE {where}
                GROUP BY t.id
                ORDER BY t.started_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()

            aggregate = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS trace_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
                FROM traces t WHERE {where}
                """,
                params,
            ).fetchone()
            metric_rows = connection.execute(
                f"""
                SELECT m.name, m.value
                FROM metrics m
                JOIN traces t ON t.id = m.trace_id
                WHERE {where}
                  AND m.name IN (
                      'latency_ms', 'cost_usd', 'input_tokens', 'output_tokens'
                  )
                """,
                params,
            ).fetchall()

            session_count = 0
            if self.has_sessions:
                session_where, session_params = self._project_where("s")
                session_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM sessions s WHERE {session_where}",
                        session_params,
                    ).fetchone()[0]
                )

        traces = [
            TraceRow(
                id=str(row["id"]),
                project=str(row["project"]),
                name=str(row["name"]),
                status=str(row["status"]),
                session_id=row["session_id"],
                started_at=row["started_at"],
                latency_ms=float(row["latency_ms"]),
                cost_usd=float(row["cost_usd"]),
                input_tokens=float(row["input_tokens"]),
                output_tokens=float(row["output_tokens"]),
                span_count=int(row["span_count"]),
            )
            for row in rows
        ]
        latencies = sorted(
            float(row["value"])
            for row in metric_rows
            if row["name"] == "latency_ms" and float(row["value"]) > 0
        )
        p95 = self._percentile(latencies, 0.95)
        total_cost = sum(
            float(row["value"]) for row in metric_rows if row["name"] == "cost_usd"
        )
        total_tokens = sum(
            float(row["value"])
            for row in metric_rows
            if row["name"] in {"input_tokens", "output_tokens"}
        )
        return Snapshot(
            traces=traces,
            trace_count=int(aggregate["trace_count"] or 0),
            session_count=session_count,
            running_count=int(aggregate["running_count"] or 0),
            error_count=int(aggregate["error_count"] or 0),
            p95_latency_ms=p95,
            total_cost_usd=total_cost,
            total_tokens=round(total_tokens),
            latency_samples=[
                trace.latency_ms for trace in reversed(traces[-32:]) if trace.latency_ms > 0
            ],
        )

    def trace_detail(self, trace_id: str) -> TraceDetail | None:
        with self.connect() as connection:
            trace_row = connection.execute(
                "SELECT * FROM traces WHERE id = ?", (trace_id,)
            ).fetchone()
            if trace_row is None:
                return None
            spans = [
                self._decode_row(row, ("metadata_json", "input_json", "output_json", "error_json"))
                for row in connection.execute(
                    "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at, id",
                    (trace_id,),
                )
            ]
            events = [
                self._decode_row(row, ("payload_json",))
                for row in connection.execute(
                    "SELECT * FROM events WHERE trace_id = ? ORDER BY timestamp, id",
                    (trace_id,),
                )
            ]
            metrics = {
                str(row["name"]): float(row["value"])
                for row in connection.execute(
                    "SELECT name, value FROM metrics WHERE trace_id = ?", (trace_id,)
                )
            }
        return TraceDetail(
            trace=self._decode_row(
                trace_row,
                ("input_json", "output_json", "expected_json", "metadata_json"),
            ),
            spans=spans,
            events=events,
            metrics=metrics,
        )

    def _project_where(self, alias: str) -> tuple[str, list[str]]:
        if self.project:
            return f"{alias}.project = ?", [self.project]
        return "1 = 1", []

    @staticmethod
    def _decode_row(row: sqlite3.Row, json_columns: tuple[str, ...]) -> dict[str, Any]:
        result = dict(row)
        for column in json_columns:
            raw = result.pop(column, None)
            key = column.removesuffix("_json")
            if raw is None:
                result[key] = None
                continue
            try:
                result[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result[key] = raw
        return result

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        index = max(0, math.ceil(len(values) * percentile) - 1)
        return values[index]
