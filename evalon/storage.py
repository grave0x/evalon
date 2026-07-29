"""Trace storage backends."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from evalon.errors import EvalonStorageError
from evalon.json import sanitize

# SQLite schema for traces, events, and metrics
_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    environment TEXT,
    session_id TEXT,
    input_json TEXT,
    output_json TEXT,
    expected_json TEXT,
    metadata_json TEXT,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT,
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT,
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT,
    ended_at TEXT,
    latency_ms REAL,
    metadata_json TEXT,
    input_json TEXT,
    output_json TEXT,
    error_json TEXT,
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
    UNIQUE(trace_id, name)
);

CREATE TABLE IF NOT EXISTS eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    eval_name TEXT NOT NULL,
    passed INTEGER NOT NULL,
    message TEXT,
    details_json TEXT,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
    UNIQUE(trace_id, eval_name)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    metadata_json TEXT,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE(session_id, name)
);

CREATE INDEX IF NOT EXISTS idx_traces_project ON traces(project);
CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status);
CREATE INDEX IF NOT EXISTS idx_traces_name ON traces(name);
CREATE INDEX IF NOT EXISTS idx_traces_started_at ON traces(started_at);
CREATE INDEX IF NOT EXISTS idx_traces_session_id ON traces(session_id);
CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent_span_id ON spans(parent_span_id);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind);
CREATE INDEX IF NOT EXISTS idx_metrics_trace_id ON metrics(trace_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_trace_id ON eval_results(trace_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_eval_name ON eval_results(eval_name);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_session_events_session_id ON session_events(session_id);
CREATE INDEX IF NOT EXISTS idx_session_metrics_session_id ON session_metrics(session_id);
"""


class JsonlStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, trace: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(sanitize(trace), ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            raise EvalonStorageError(f"Failed to write Evalon trace to {self.path}") from exc

    def write_session(self, session: dict[str, Any]) -> None:
        raise EvalonStorageError("JsonlStorage does not support session queries. Use SqliteStorage.")

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise EvalonStorageError("JsonlStorage does not support queries. Use SqliteStorage.")

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        raise EvalonStorageError("JsonlStorage does not support trace lookups. Use SqliteStorage.")

    def count(self, **kwargs: Any) -> int:
        raise EvalonStorageError("JsonlStorage does not support count. Use SqliteStorage.")

    def delete_trace(self, trace_id: str) -> bool:
        raise EvalonStorageError("JsonlStorage does not support delete. Use SqliteStorage.")

    def write_eval_results(self, trace_id: str, results: list[Any]) -> None:
        raise EvalonStorageError("JsonlStorage does not support eval results. Use SqliteStorage.")

    def get_eval_results(self, trace_id: str) -> list[dict[str, Any]]:
        raise EvalonStorageError("JsonlStorage does not support eval result queries. Use SqliteStorage.")

    def query_eval_results(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise EvalonStorageError("JsonlStorage does not support eval result queries. Use SqliteStorage.")

    def query_sessions(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise EvalonStorageError("JsonlStorage does not support session queries. Use SqliteStorage.")

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        raise EvalonStorageError("JsonlStorage does not support session lookups. Use SqliteStorage.")

    def query_metrics(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise EvalonStorageError("JsonlStorage does not support metric queries. Use SqliteStorage.")

    # Incremental flush no-ops (live observability requires SQLite)
    def upsert_session(self, session: dict[str, Any]) -> None:
        return None

    def upsert_trace_header(self, trace: dict[str, Any]) -> None:
        return None

    def upsert_span(self, span: dict[str, Any]) -> None:
        return None

    def append_event(self, trace_id: str, event: dict[str, Any]) -> None:
        return None

    def upsert_metrics(self, trace_id: str, metrics: dict[str, Any]) -> None:
        return None

    def finalize_trace(self, trace: dict[str, Any]) -> None:
        self.write(trace)

    def finalize_session(self, session: dict[str, Any]) -> None:
        return None


class SqliteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            # Core tables
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS traces (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    environment TEXT,
                    session_id TEXT,
                    input_json TEXT,
                    output_json TEXT,
                    expected_json TEXT,
                    metadata_json TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    span_id TEXT,
                    type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT,
                    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS spans (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    started_at TEXT,
                    ended_at TEXT,
                    latency_ms REAL,
                    metadata_json TEXT,
                    input_json TEXT,
                    output_json TEXT,
                    error_json TEXT,
                    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    value REAL NOT NULL,
                    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
                    UNIQUE(trace_id, name)
                );
                CREATE TABLE IF NOT EXISTS eval_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    eval_name TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    message TEXT,
                    details_json TEXT,
                    run_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
                    UNIQUE(trace_id, eval_name)
                );
            """)
            # Session tables (may fail on older sqlite, that's ok)
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'running',
                        metadata_json TEXT,
                        started_at TEXT,
                        ended_at TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS session_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        payload_json TEXT,
                        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS session_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        value REAL NOT NULL,
                        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                        UNIQUE(session_id, name)
                    );
                """)
            except sqlite3.OperationalError:
                pass
            # Ensure columns for backwards compatibility
            self._ensure_column(conn, "events", "span_id", "TEXT")
            self._ensure_column(conn, "traces", "session_id", "TEXT")
            # Indexes
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_traces_project ON traces(project)",
                "CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status)",
                "CREATE INDEX IF NOT EXISTS idx_traces_name ON traces(name)",
                "CREATE INDEX IF NOT EXISTS idx_traces_started_at ON traces(started_at)",
                "CREATE INDEX IF NOT EXISTS idx_traces_session_id ON traces(session_id)",
                "CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events(trace_id)",
                "CREATE INDEX IF NOT EXISTS idx_events_span_id ON events(span_id)",
                "CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id)",
                "CREATE INDEX IF NOT EXISTS idx_spans_parent_span_id ON spans(parent_span_id)",
                "CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind)",
                "CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name)",
                "CREATE INDEX IF NOT EXISTS idx_metrics_trace_id ON metrics(trace_id)",
                "CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name)",
                "CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project)",
                "CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at)",
                "CREATE INDEX IF NOT EXISTS idx_session_events_session_id ON session_events(session_id)",
                "CREATE INDEX IF NOT EXISTS idx_session_metrics_session_id ON session_metrics(session_id)",
            ]:
                try:
                    conn.execute(idx_sql)
                except sqlite3.OperationalError:
                    pass

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _get_connection(self) -> sqlite3.Connection:
        """Get a connection with WAL mode for better concurrency."""
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def upsert_trace_header(self, trace: dict[str, Any]) -> None:
        """Insert or update a trace row without replacing child spans/events."""
        try:
            with self._lock:
                with self._get_connection() as conn:
                    self._upsert_trace_header_conn(conn, trace)
                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to upsert trace header to {self.path}") from exc

    def upsert_span(self, span: dict[str, Any]) -> None:
        """Insert or replace a single span row."""
        try:
            with self._lock:
                with self._get_connection() as conn:
                    self._upsert_span_conn(conn, span)
                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to upsert span to {self.path}") from exc

    def append_event(self, trace_id: str, event: dict[str, Any]) -> None:
        """Append a single event (live path; finish may rewrite full event set)."""
        try:
            with self._lock:
                with self._get_connection() as conn:
                    self._append_event_conn(conn, trace_id, event)
                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to append event to {self.path}") from exc

    def upsert_metrics(self, trace_id: str, metrics: dict[str, Any]) -> None:
        """Merge metrics for a trace (name → value)."""
        if not metrics:
            return
        try:
            with self._lock:
                with self._get_connection() as conn:
                    self._upsert_metrics_conn(conn, trace_id, metrics)
                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to upsert metrics to {self.path}") from exc

    def upsert_session(self, session: dict[str, Any]) -> None:
        """Insert or update a session header (no nested traces required)."""
        try:
            with self._lock:
                with self._get_connection() as conn:
                    self._upsert_session_header_conn(conn, session)
                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to upsert session to {self.path}") from exc

    def finalize_trace(self, trace: dict[str, Any]) -> None:
        """Full snapshot write used at trace end (replaces children)."""
        self.write(trace)

    def finalize_session(self, session: dict[str, Any]) -> None:
        """Full session snapshot write used at session end."""
        self.write_session(session)

    def _upsert_trace_header_conn(self, conn: sqlite3.Connection, trace: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO traces
            (id, project, name, status, environment, session_id,
             input_json, output_json, expected_json, metadata_json,
             started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project=excluded.project,
                name=excluded.name,
                status=excluded.status,
                environment=excluded.environment,
                session_id=excluded.session_id,
                input_json=excluded.input_json,
                output_json=excluded.output_json,
                expected_json=excluded.expected_json,
                metadata_json=excluded.metadata_json,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at
            """,
            (
                trace["id"],
                trace["project"],
                trace["name"],
                trace.get("status", "running"),
                trace.get("environment"),
                trace.get("session_id"),
                json.dumps(trace.get("input")),
                json.dumps(trace.get("output")),
                json.dumps(trace.get("expected")),
                json.dumps(trace.get("metadata") or {}),
                trace.get("started_at"),
                trace.get("ended_at"),
            ),
        )

    def _upsert_span_conn(self, conn: sqlite3.Connection, span: dict[str, Any]) -> None:
        trace_id = span.get("trace_id")
        if not trace_id:
            raise EvalonStorageError("upsert_span requires span['trace_id']")
        conn.execute(
            """
            INSERT OR REPLACE INTO spans
            (id, trace_id, parent_span_id, name, kind, status,
             started_at, ended_at, latency_ms, metadata_json,
             input_json, output_json, error_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span["id"],
                trace_id,
                span.get("parent_span_id"),
                span["name"],
                span.get("kind", "custom"),
                span.get("status", "running"),
                span.get("started_at"),
                span.get("ended_at"),
                span.get("latency_ms"),
                json.dumps(span.get("metadata") or {}),
                json.dumps(span.get("input")),
                json.dumps(span.get("output")),
                json.dumps(span.get("error")),
            ),
        )

    def _append_event_conn(
        self, conn: sqlite3.Connection, trace_id: str, event: dict[str, Any]
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (trace_id, span_id, type, timestamp, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                event.get("span_id"),
                event["type"],
                event["timestamp"],
                json.dumps(event.get("payload")),
            ),
        )

    def _upsert_metrics_conn(
        self, conn: sqlite3.Connection, trace_id: str, metrics: dict[str, Any]
    ) -> None:
        for name, value in metrics.items():
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            conn.execute(
                """
                INSERT INTO metrics (trace_id, name, value)
                VALUES (?, ?, ?)
                ON CONFLICT(trace_id, name) DO UPDATE SET value=excluded.value
                """,
                (trace_id, name, numeric),
            )

    def _upsert_session_header_conn(self, conn: sqlite3.Connection, session: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO sessions
            (id, project, name, status, metadata_json, started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project=excluded.project,
                name=excluded.name,
                status=excluded.status,
                metadata_json=excluded.metadata_json,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at
            """,
            (
                session["id"],
                session["project"],
                session["name"],
                session.get("status", "running"),
                json.dumps(session.get("metadata") or {}),
                session.get("started_at"),
                session.get("ended_at"),
            ),
        )

    def write(self, trace: dict[str, Any]) -> None:
        """Write a full trace snapshot to SQLite (replaces children)."""
        try:
            with self._lock:
                with self._get_connection() as conn:
                    self._upsert_trace_header_conn(conn, trace)

                    conn.execute("DELETE FROM events WHERE trace_id = ?", (trace["id"],))
                    conn.execute("DELETE FROM spans WHERE trace_id = ?", (trace["id"],))
                    conn.execute("DELETE FROM metrics WHERE trace_id = ?", (trace["id"],))

                    for span in trace.get("spans", []):
                        span_row = dict(span)
                        span_row.setdefault("trace_id", trace["id"])
                        self._upsert_span_conn(conn, span_row)

                    for event in trace.get("events", []):
                        self._append_event_conn(conn, trace["id"], event)

                    self._upsert_metrics_conn(conn, trace["id"], trace.get("metrics") or {})
                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to write Evalon trace to {self.path}") from exc

    def write_session(self, session: dict[str, Any]) -> None:
        """Write a session to SQLite."""
        try:
            with self._lock:
                with self._get_connection() as conn:
                    self._upsert_session_header_conn(conn, session)

                    conn.execute("DELETE FROM session_events WHERE session_id = ?", (session["id"],))
                    conn.execute("DELETE FROM session_metrics WHERE session_id = ?", (session["id"],))

                    for event in session.get("events", []):
                        conn.execute(
                            """
                            INSERT INTO session_events (session_id, type, timestamp, payload_json)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                session["id"],
                                event["type"],
                                event["timestamp"],
                                json.dumps(event.get("payload")),
                            ),
                        )

                    for name, value in (session.get("metrics") or {}).items():
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO session_metrics (session_id, name, value)
                            VALUES (?, ?, ?)
                            """,
                            (session["id"], name, value),
                        )

                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to write Evalon session to {self.path}") from exc

    def query(
        self,
        *,
        project: str | None = None,
        name: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        environment: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
        min_cost_usd: float | None = None,
        max_latency_ms: float | None = None,
        has_error: bool | None = None,
        tool_name: str | None = None,
        model: str | None = None,
        text: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query traces with filters."""
        conditions: list[str] = []
        params: list[Any] = []

        if project:
            conditions.append("t.project = ?")
            params.append(project)
        if name:
            conditions.append("t.name = ?")
            params.append(name)
        if status:
            conditions.append("t.status = ?")
            params.append(status)
        if session_id:
            conditions.append("t.session_id = ?")
            params.append(session_id)
        if environment:
            conditions.append("t.environment = ?")
            params.append(environment)
        if started_after:
            conditions.append("t.started_at >= ?")
            params.append(started_after)
        if started_before:
            conditions.append("t.started_at <= ?")
            params.append(started_before)
        if has_error is True:
            conditions.append("t.status = 'error'")
        elif has_error is False:
            conditions.append("t.status != 'error'")
        if min_cost_usd is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM metrics m "
                "WHERE m.trace_id = t.id AND m.name = 'cost_usd' AND m.value >= ?)"
            )
            params.append(min_cost_usd)
        if max_latency_ms is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM metrics m "
                "WHERE m.trace_id = t.id AND m.name = 'latency_ms' AND m.value <= ?)"
            )
            params.append(max_latency_ms)
        if tool_name:
            conditions.append(
                "EXISTS (SELECT 1 FROM spans s "
                "WHERE s.trace_id = t.id AND s.kind = 'tool' AND s.name = ?)"
            )
            params.append(tool_name)
        if model:
            conditions.append(
                "EXISTS (SELECT 1 FROM spans s WHERE s.trace_id = t.id AND ("
                "s.metadata_json LIKE ? OR s.input_json LIKE ?))"
            )
            like = f"%{model}%"
            params.extend([like, like])
        if text:
            conditions.append(
                "(lower(t.id) LIKE ? OR lower(t.name) LIKE ? "
                "OR lower(COALESCE(t.input_json,'')) LIKE ? "
                "OR lower(COALESCE(t.output_json,'')) LIKE ?)"
            )
            like = f"%{text.lower()}%"
            params.extend([like, like, like, like])

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT t.* FROM traces t
                WHERE {where_clause}
                ORDER BY
                  CASE t.status WHEN 'error' THEN 0 WHEN 'running' THEN 1 ELSE 2 END,
                  t.started_at DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()

            return [self._row_to_trace(conn, row) for row in rows]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Get a single trace by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM traces WHERE id = ?", (trace_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_trace(conn, row)

    def count(self, *, project: str | None = None, status: str | None = None) -> int:
        """Count traces with optional filters."""
        conditions = []
        params: list[Any] = []

        if project:
            conditions.append("project = ?")
            params.append(project)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._get_connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM traces WHERE {where_clause}",
                params,
            ).fetchone()
            return row["cnt"] if row else 0

    def delete_trace(self, trace_id: str) -> bool:
        """Delete a trace and its associated events/metrics. Returns True if deleted."""
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM traces WHERE id = ?", (trace_id,))
            conn.commit()
            return cursor.rowcount > 0

    def _row_to_trace(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a trace dict."""
        trace_id = row["id"]

        # Get events
        events = [
            {
                "type": e["type"],
                **({"span_id": e["span_id"]} if e["span_id"] else {}),
                "timestamp": e["timestamp"],
                "payload": json.loads(e["payload_json"]) if e["payload_json"] else {},
            }
            for e in conn.execute(
                "SELECT * FROM events WHERE trace_id = ? ORDER BY timestamp",
                (trace_id,),
            ).fetchall()
        ]

        # Get spans
        spans = [
            {
                "id": s["id"],
                "trace_id": s["trace_id"],
                "parent_span_id": s["parent_span_id"],
                "name": s["name"],
                "kind": s["kind"],
                "status": s["status"],
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "latency_ms": s["latency_ms"],
                "metadata": json.loads(s["metadata_json"]) if s["metadata_json"] else {},
                "input": json.loads(s["input_json"]) if s["input_json"] else None,
                "output": json.loads(s["output_json"]) if s["output_json"] else None,
                "error": json.loads(s["error_json"]) if s["error_json"] else None,
            }
            for s in conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at",
                (trace_id,),
            ).fetchall()
        ]

        # Get metrics
        metrics = {
            m["name"]: m["value"]
            for m in conn.execute(
                "SELECT * FROM metrics WHERE trace_id = ?",
                (trace_id,),
            ).fetchall()
        }

        return {
            "id": trace_id,
            "project": row["project"],
            "name": row["name"],
            "status": row["status"],
            "environment": row["environment"],
            "session_id": row["session_id"],
            "input": json.loads(row["input_json"]) if row["input_json"] else None,
            "output": json.loads(row["output_json"]) if row["output_json"] else None,
            "expected": json.loads(row["expected_json"]) if row["expected_json"] else None,
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "spans": spans,
            "events": events,
            "metrics": metrics,
        }

    def write_eval_results(self, trace_id: str, results: list[Any]) -> None:
        """Write eval results for a trace (upserts by trace_id + eval_name)."""
        try:
            with self._lock:
                with self._get_connection() as conn:
                    for result in results:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO eval_results
                            (trace_id, eval_name, passed, message, details_json)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                trace_id,
                                result.name,
                                1 if result.passed else 0,
                                result.message,
                                json.dumps(result.details),
                            ),
                        )
                    conn.commit()
        except sqlite3.Error as exc:
            raise EvalonStorageError(f"Failed to write eval results for {trace_id}") from exc

    def get_eval_results(self, trace_id: str) -> list[dict[str, Any]]:
        """Get all eval results for a trace."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_results WHERE trace_id = ? ORDER BY run_at",
                (trace_id,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "trace_id": r["trace_id"],
                    "eval_name": r["eval_name"],
                    "passed": bool(r["passed"]),
                    "message": r["message"],
                    "details": json.loads(r["details_json"]) if r["details_json"] else {},
                    "run_at": r["run_at"],
                }
                for r in rows
            ]

    def query_metrics(
        self,
        *,
        project: str | None = None,
        name: str | None = None,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        """Return recorded trace metrics with the trace timestamp and project context."""
        conditions = []
        params: list[Any] = []
        if project:
            conditions.append("t.project = ?")
            params.append(project)
        if name:
            conditions.append("m.name = ?")
            params.append(name)
        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.name, m.value, m.trace_id, t.project, t.name AS trace_name,
                       t.started_at, t.ended_at
                FROM metrics m
                JOIN traces t ON t.id = m.trace_id
                WHERE {where_clause}
                ORDER BY t.started_at DESC, m.name ASC
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
            return [dict(row) for row in rows]

    def query_eval_results(
        self,
        *,
        eval_name: str | None = None,
        passed: bool | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query eval results with optional filters."""
        conditions = []
        params: list[Any] = []

        if eval_name:
            conditions.append("er.eval_name = ?")
            params.append(eval_name)
        if passed is not None:
            conditions.append("er.passed = ?")
            params.append(1 if passed else 0)
        if project:
            conditions.append("t.project = ?")
            params.append(project)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        join_clause = "JOIN traces t ON er.trace_id = t.id" if project else ""

        with self._get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT er.* FROM eval_results er
                {join_clause}
                WHERE {where_clause}
                ORDER BY er.run_at DESC
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "trace_id": r["trace_id"],
                    "eval_name": r["eval_name"],
                    "passed": bool(r["passed"]),
                    "message": r["message"],
                    "details": json.loads(r["details_json"]) if r["details_json"] else {},
                    "run_at": r["run_at"],
                }
                for r in rows
            ]

    def query_sessions(
        self,
        *,
        project: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query sessions with filters."""
        conditions = []
        params: list[Any] = []

        if project:
            conditions.append("project = ?")
            params.append(project)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM sessions
                WHERE {where_clause}
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()

            return [self._row_to_session(conn, row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a single session by ID with its traces."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(conn, row)

    def _row_to_session(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a session dict with nested traces."""
        session_id = row["id"]

        events = [
            {
                "type": e["type"],
                "timestamp": e["timestamp"],
                "payload": json.loads(e["payload_json"]) if e["payload_json"] else {},
            }
            for e in conn.execute(
                "SELECT * FROM session_events WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            ).fetchall()
        ]

        metrics = {
            m["name"]: m["value"]
            for m in conn.execute(
                "SELECT * FROM session_metrics WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        }

        traces = [
            self._row_to_trace(conn, t)
            for t in conn.execute(
                "SELECT * FROM traces WHERE session_id = ? ORDER BY started_at",
                (session_id,),
            ).fetchall()
        ]

        return {
            "id": session_id,
            "project": row["project"],
            "name": row["name"],
            "status": row["status"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "traces": traces,
            "events": events,
            "metrics": metrics,
        }
