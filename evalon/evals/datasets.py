"""Versioned dataset workflows shared by the Python API, CLI, and TUI."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evalon.core.errors import EvalonStorageError
from evalon.core.json import sanitize
from evalon.evals.contracts import Dataset, DatasetCase, DatasetVersion
from evalon.evals.store import EvalStore
from evalon.storage import SqliteStorage


@dataclass(slots=True)
class TraceCaseProposal:
    """A redacted, editable proposal that cannot silently promote trace output."""

    trace_id: str
    project: str
    captured_at: str | None
    case_input: Any
    trace_output: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_results: list[dict[str, Any]] = field(default_factory=list)

    def to_case(
        self,
        case_id: str,
        *,
        expected_output: Any = None,
        use_trace_output_as_expected: bool = False,
        use_trace_output_as_reference: bool = False,
        tags: list[str] | None = None,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
        include_tool_results: bool = False,
    ) -> DatasetCase:
        if use_trace_output_as_expected and expected_output is not None:
            raise ValueError(
                "Pass expected_output or confirm trace output, but not both"
            )
        case_metadata = dict(self.metadata)
        case_metadata.update(metadata or {})
        if include_tool_results:
            case_metadata["selected_tool_results"] = self.tool_results
        return DatasetCase(
            case_id=case_id,
            input=sanitize(self.case_input),
            expected_output=sanitize(
                self.trace_output if use_trace_output_as_expected else expected_output
            ),
            reference_output=sanitize(
                self.trace_output if use_trace_output_as_reference else None
            ),
            tags=list(tags or []),
            notes=notes,
            metadata=sanitize(case_metadata),
            source_trace_id=self.trace_id,
            source_project=self.project,
            source_captured_at=self.captured_at,
        )


@dataclass(frozen=True, slots=True)
class DatasetDiff:
    dataset_id: str
    from_version: int
    to_version: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: dict[str, tuple[dict[str, Any], dict[str, Any]]]

    def to_dict(self) -> dict[str, Any]:
        return sanitize(
            {
                "dataset_id": self.dataset_id,
                "from_version": self.from_version,
                "to_version": self.to_version,
                "added": list(self.added),
                "removed": list(self.removed),
                "changed": self.changed,
            }
        )


class DatasetService:
    """Create immutable dataset versions and capture immutable observed traces."""

    def __init__(
        self,
        store: EvalStore,
        *,
        trace_storage: SqliteStorage | None = None,
    ) -> None:
        self.store = store
        self.trace_storage = trace_storage

    def create(
        self,
        name: str,
        *,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Dataset:
        return self.store.create_dataset(
            Dataset(
                name=name,
                description=description,
                metadata=sanitize(metadata or {}),
            )
        )

    def add_case(
        self,
        dataset_id: str,
        case: DatasetCase,
        *,
        change_note: str | None = None,
    ) -> DatasetVersion:
        current = self.store.get_dataset_version(dataset_id)
        cases = self._clone_cases(current.cases if current else [])
        if any(item.case_id == case.case_id for item in cases):
            raise EvalonStorageError(f"Case ID {case.case_id!r} already exists")
        if current is not None:
            duplicate = self.store.find_duplicate_case(
                current.id,
                input=case.input,
                source_trace_id=case.source_trace_id,
            )
            if duplicate is not None:
                reason = (
                    f"source trace {case.source_trace_id!r}"
                    if duplicate.source_trace_id == case.source_trace_id
                    else "normalized input"
                )
                raise EvalonStorageError(
                    f"Dataset already contains {reason} as {duplicate.case_id!r}"
                )
        cases.append(self._clone_case(case))
        return self._publish(
            dataset_id,
            cases,
            change_note=change_note or f"Add case {case.case_id}",
        )

    def edit_case(
        self,
        dataset_id: str,
        case_id: str,
        **changes: Any,
    ) -> DatasetVersion:
        current = self._require_current(dataset_id)
        allowed = {
            "input",
            "expected_output",
            "reference_output",
            "tags",
            "notes",
            "metadata",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported case fields: {', '.join(sorted(unknown))}")
        cases = self._clone_cases(current.cases)
        for index, item in enumerate(cases):
            if item.case_id != case_id:
                continue
            payload = item.to_dict()
            payload.update({key: sanitize(value) for key, value in changes.items()})
            payload["id"] = DatasetCase(case_id=case_id, input=None).id
            payload["dataset_version_id"] = None
            cases[index] = DatasetCase.from_dict(payload)
            return self._publish(
                dataset_id,
                cases,
                change_note=f"Edit case {case_id}",
            )
        raise EvalonStorageError(f"Case {case_id!r} does not exist")

    def archive_case(self, dataset_id: str, case_id: str) -> DatasetVersion:
        current = self._require_current(dataset_id)
        cases = [
            case for case in self._clone_cases(current.cases) if case.case_id != case_id
        ]
        if len(cases) == len(current.cases):
            raise EvalonStorageError(f"Case {case_id!r} does not exist")
        return self._publish(
            dataset_id,
            cases,
            change_note=f"Archive case {case_id}",
        )

    def propose_from_trace(
        self,
        trace_id: str,
        *,
        selected_tool_names: set[str] | None = None,
    ) -> TraceCaseProposal:
        if self.trace_storage is None:
            raise EvalonStorageError("Trace storage is required for trace capture")
        trace = self.trace_storage.get_trace(trace_id)
        if trace is None:
            raise EvalonStorageError(f"Trace {trace_id!r} does not exist")
        selected = selected_tool_names or set()
        tool_results = [
            sanitize(
                {
                    "span_id": span.get("id"),
                    "tool": span.get("name"),
                    "arguments": span.get("input"),
                    "output": span.get("output"),
                    "status": span.get("status"),
                }
            )
            for span in trace.get("spans", [])
            if span.get("kind") == "tool"
            and (not selected or str(span.get("name")) in selected)
        ]
        return TraceCaseProposal(
            trace_id=str(trace["id"]),
            project=str(trace.get("project") or ""),
            captured_at=trace.get("started_at"),
            case_input=sanitize(trace.get("input")),
            trace_output=sanitize(trace.get("output")),
            metadata=sanitize(
                {
                    "trace_name": trace.get("name"),
                    "environment": trace.get("environment"),
                    "trace_metadata": trace.get("metadata") or {},
                }
            ),
            tool_results=tool_results,
        )

    def import_file(
        self,
        path: str | Path,
        *,
        dataset_id: str | None = None,
        name: str | None = None,
    ) -> DatasetVersion:
        source = Path(path)
        payload = _read_dataset_file(source)
        dataset_data = payload.get("dataset", {})
        if dataset_id is None:
            dataset = self.create(
                name or str(dataset_data.get("name") or source.stem),
                description=dataset_data.get("description"),
                metadata=dataset_data.get("metadata") or {},
            )
            dataset_id = dataset.id
        cases = [
            DatasetCase.from_dict(
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "case_id",
                        "input",
                        "expected_output",
                        "reference_output",
                        "tags",
                        "notes",
                        "metadata",
                        "source_trace_id",
                        "source_project",
                        "source_captured_at",
                    }
                }
            )
            for item in payload.get("cases", [])
        ]
        current = self.store.get_dataset_version(dataset_id)
        if current is not None:
            raise EvalonStorageError(
                "Import into an existing non-empty dataset is ambiguous; "
                "create a new dataset or add cases explicitly"
            )
        return self._publish(dataset_id, cases, change_note=f"Import {source.name}")

    def export_file(
        self,
        dataset_id: str,
        path: str | Path,
        *,
        version: int | None = None,
    ) -> Path:
        dataset = self.store.get_dataset(dataset_id)
        snapshot = self.store.get_dataset_version(dataset_id, version)
        if dataset is None or snapshot is None:
            raise EvalonStorageError(
                f"Dataset version {dataset_id}:{version or 'current'} not found"
            )
        destination = Path(path)
        payload = {
            "dataset": dataset.to_dict(),
            "version": snapshot.version,
            "version_metadata": snapshot.metadata,
            "cases": [case.to_dict() for case in snapshot.cases],
        }
        _write_dataset_file(destination, payload)
        return destination

    def diff(
        self,
        dataset_id: str,
        from_version: int,
        to_version: int,
    ) -> DatasetDiff:
        before = self.store.get_dataset_version(dataset_id, from_version)
        after = self.store.get_dataset_version(dataset_id, to_version)
        if before is None or after is None:
            raise EvalonStorageError("Both dataset versions must exist")
        before_by_id = {case.case_id: _case_content(case) for case in before.cases}
        after_by_id = {case.case_id: _case_content(case) for case in after.cases}
        before_ids = set(before_by_id)
        after_ids = set(after_by_id)
        changed = {
            case_id: (before_by_id[case_id], after_by_id[case_id])
            for case_id in sorted(before_ids & after_ids)
            if before_by_id[case_id] != after_by_id[case_id]
        }
        return DatasetDiff(
            dataset_id=dataset_id,
            from_version=from_version,
            to_version=to_version,
            added=tuple(sorted(after_ids - before_ids)),
            removed=tuple(sorted(before_ids - after_ids)),
            changed=changed,
        )

    def _publish(
        self,
        dataset_id: str,
        cases: list[DatasetCase],
        *,
        change_note: str,
    ) -> DatasetVersion:
        current = self.store.get_dataset_version(dataset_id)
        return self.store.create_dataset_version(
            DatasetVersion(
                dataset_id=dataset_id,
                version=(current.version + 1) if current else 1,
                cases=cases,
                change_note=change_note,
            )
        )

    def _require_current(self, dataset_id: str) -> DatasetVersion:
        current = self.store.get_dataset_version(dataset_id)
        if current is None:
            raise EvalonStorageError(f"Dataset {dataset_id!r} has no versions")
        return current

    @staticmethod
    def _clone_case(case: DatasetCase) -> DatasetCase:
        payload = copy.deepcopy(case.to_dict())
        payload["id"] = DatasetCase(case_id=case.case_id, input=None).id
        payload["dataset_version_id"] = None
        return DatasetCase.from_dict(payload)

    @classmethod
    def _clone_cases(cls, cases: list[DatasetCase]) -> list[DatasetCase]:
        return [cls._clone_case(case) for case in cases]


def _case_content(case: DatasetCase) -> dict[str, Any]:
    payload = case.to_dict()
    payload.pop("id", None)
    payload.pop("dataset_version_id", None)
    return payload


def _read_dataset_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML support requires PyYAML") from exc
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Dataset YAML must contain an object")
        return value
    if suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        header = rows[0] if rows and rows[0].get("_type") == "dataset" else {}
        cases = rows[1:] if header else rows
        return {"dataset": header.get("dataset", {}), "cases": cases}
    raise ValueError("Dataset files must use .yaml, .yml, or .jsonl")


def _write_dataset_file(path: Path, payload: dict[str, Any]) -> None:
    suffix = path.suffix.lower()
    path.parent.mkdir(parents=True, exist_ok=True)
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML support requires PyYAML") from exc
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return
    if suffix == ".jsonl":
        lines = [
            json.dumps(
                {"_type": "dataset", "dataset": payload["dataset"]},
                ensure_ascii=False,
            ),
            *[
                json.dumps(case, ensure_ascii=False)
                for case in payload.get("cases", [])
            ],
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    raise ValueError("Dataset files must use .yaml, .yml, or .jsonl")
