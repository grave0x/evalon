"""Serve the Evalon trace dashboard with a comprehensive REST API."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from evalon.paths import default_db_path

_DASHBOARD_HTML = Path(__file__).parent / "ui" / "trace-viewer.html"


class _Handler(SimpleHTTPRequestHandler):
    jsonl_path: str = ""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        routes: dict[str, Any] = {
            "": self._serve_index,
            "/index.html": self._serve_index,
            "/api/traces": self._serve_traces,
            "/api/sessions": self._serve_sessions,
            "/api/stats": self._serve_stats,
            "/api/metrics": self._serve_metrics,
        }

        if path in routes:
            routes[path]()
            return

        # /api/sessions/:id
        if path.startswith("/api/sessions/"):
            session_id = path[len("/api/sessions/"):]
            if session_id:
                self._serve_session(session_id)
                return

        # /api/traces/:id
        if path.startswith("/api/traces/"):
            remainder = path[len("/api/traces/"):]
            parts = remainder.split("/", 1)
            trace_id = parts[0]
            if len(parts) == 2 and parts[1] == "spans":
                self._serve_trace_spans(trace_id)
            elif len(parts) == 2 and parts[1] == "events":
                self._serve_trace_events(trace_id)
            elif len(parts) == 2 and parts[1] == "eval-results":
                self._serve_trace_eval_results(trace_id)
            elif trace_id and len(parts) == 1:
                self._serve_trace(trace_id)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    # ── Storage helpers ──────────────────────────────────────────────

    def _get_storage(self):
        from evalon.storage import SqliteStorage

        p = Path(self.jsonl_path)
        if not p.exists():
            return None
        suffix = p.suffix.lower()
        if suffix in (".sqlite", ".db", ".sqlite3"):
            return SqliteStorage(p)
        return None

    # ── Route handlers ───────────────────────────────────────────────

    def _serve_index(self) -> None:
        if _DASHBOARD_HTML.exists():
            self._serve_file(_DASHBOARD_HTML, "text/html")
        else:
            body = json.dumps({"name": "evalon-dashboard", "version": "0.1.0"}).encode()
            self._send_json(body)

    def _serve_traces(self) -> None:
        storage = self._get_storage()
        if storage is None:
            self._send_json(json.dumps({"traces": [], "sessions": []}).encode())
            return

        params = self._query_params()
        project = params.get("project", [None])[0]
        status = params.get("status", [None])[0]
        session_id = params.get("session_id", [None])[0]
        environment = params.get("environment", [None])[0]
        tool_name = params.get("tool", [None])[0] or params.get("tool_name", [None])[0]
        model = params.get("model", [None])[0]
        text = params.get("text", [None])[0] or params.get("q", [None])[0]
        has_error_raw = params.get("has_error", [None])[0]
        has_error = None
        if has_error_raw is not None:
            has_error = str(has_error_raw).lower() in ("1", "true", "yes")
        min_cost_raw = params.get("min_cost_usd", [None])[0]
        min_cost_usd = float(min_cost_raw) if min_cost_raw not in (None, "") else None
        limit = int(params.get("limit", ["1000"])[0])
        offset = int(params.get("offset", ["0"])[0])

        traces = storage.query(
            project=project,
            status=status,
            session_id=session_id,
            environment=environment,
            tool_name=tool_name,
            model=model,
            text=text,
            has_error=has_error,
            min_cost_usd=min_cost_usd,
            limit=limit,
            offset=offset,
        )
        self._send_json(json.dumps({"traces": traces}).encode())

    def _serve_sessions(self) -> None:
        storage = self._get_storage()
        if storage is None:
            self._send_json(json.dumps([]).encode())
            return

        params = self._query_params()
        project = params.get("project", [None])[0]
        limit = int(params.get("limit", ["1000"])[0])
        offset = int(params.get("offset", ["0"])[0])

        sessions = storage.query_sessions(project=project, limit=limit, offset=offset)
        self._send_json(json.dumps(sessions).encode())

    def _serve_session(self, session_id: str) -> None:
        storage = self._get_storage()
        if storage is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        session = storage.get_session(session_id)
        if session is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_json(json.dumps(session).encode())

    def _serve_trace(self, trace_id: str) -> None:
        storage = self._get_storage()
        if storage is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        trace = storage.get_trace(trace_id)
        if trace is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_json(json.dumps(trace).encode())

    def _serve_trace_spans(self, trace_id: str) -> None:
        storage = self._get_storage()
        if storage is None:
            self._send_json(json.dumps([]).encode())
            return

        trace = storage.get_trace(trace_id)
        if trace is None:
            self._send_json(json.dumps([]).encode())
            return
        self._send_json(json.dumps(trace.get("spans", [])).encode())

    def _serve_trace_events(self, trace_id: str) -> None:
        storage = self._get_storage()
        if storage is None:
            self._send_json(json.dumps([]).encode())
            return

        trace = storage.get_trace(trace_id)
        if trace is None:
            self._send_json(json.dumps([]).encode())
            return
        self._send_json(json.dumps(trace.get("events", [])).encode())

    def _serve_trace_eval_results(self, trace_id: str) -> None:
        storage = self._get_storage()
        if storage is None:
            self._send_json(json.dumps([]).encode())
            return
        self._send_json(json.dumps(storage.get_eval_results(trace_id)).encode())

    def _serve_stats(self) -> None:
        storage = self._get_storage()
        if storage is None:
            self._send_json(json.dumps(self._empty_stats()).encode())
            return

        params = self._query_params()
        project = params.get("project", [None])[0]

        sessions = storage.query_sessions(project=project, limit=10000)
        traces = storage.query(project=project, limit=10000)

        total_traces = len(traces)
        total_sessions = len(sessions)
        total_tool_calls = sum(t.get("metrics", {}).get("tool_calls", 0) for t in traces)
        total_cost = sum(t.get("metrics", {}).get("cost_usd", 0) for t in traces)
        total_input_tokens = sum(int(t.get("metrics", {}).get("input_tokens", 0)) for t in traces)
        total_output_tokens = sum(int(t.get("metrics", {}).get("output_tokens", 0)) for t in traces)
        total_llm_calls = sum(int(t.get("metrics", {}).get("llm_calls", 0)) for t in traces)
        total_errors = sum(int(t.get("metrics", {}).get("errors", 0)) for t in traces)
        error_traces = sum(1 for t in traces if t.get("status") == "error")

        stats = {
            "total_traces": total_traces,
            "total_sessions": total_sessions,
            "total_tool_calls": total_tool_calls,
            "total_cost_usd": total_cost,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_llm_calls": total_llm_calls,
            "total_errors": total_errors,
            "error_traces": error_traces,
            "error_rate": round(error_traces / total_traces * 100, 2) if total_traces > 0 else 0,
        }
        self._send_json(json.dumps(stats).encode())

    def _serve_metrics(self) -> None:
        storage = self._get_storage()
        if storage is None:
            self._send_json(json.dumps([]).encode())
            return

        params = self._query_params()
        project = params.get("project", [None])[0]
        name = params.get("name", [None])[0]
        limit = int(params.get("limit", ["1000"])[0])
        self._send_json(json.dumps(storage.query_metrics(project=project, name=name, limit=limit)).encode())

    # ── Helpers ──────────────────────────────────────────────────────

    def _query_params(self) -> dict[str, list[str]]:
        parsed = urlparse(self.path)
        return parse_qs(parsed.query)

    def _serve_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve(db_path: str | Path = default_db_path(), port: int = 8787, open_browser: bool = True) -> None:
    """Start a local server for the trace dashboard.

    Args:
        db_path: Path to the trace file (SQLite or JSONL).
        port: Port to listen on.
        open_browser: Whether to open the browser automatically.
    """
    _Handler.jsonl_path = db_path

    server = HTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"

    print(f"Dashboard running at {url}")
    print(f"Loading traces from: {db_path}")
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Evalon trace dashboard")
    parser.add_argument(
        "jsonl",
        nargs="?",
        default=default_db_path(),
        help="Path to trace file (default: ~/.evalon/evalon-runs.sqlite)",
    )
    parser.add_argument("-p", "--port", type=int, default=8787, help="Port to listen on")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()
    serve(db_path=args.jsonl, port=args.port, open_browser=not args.no_browser)
