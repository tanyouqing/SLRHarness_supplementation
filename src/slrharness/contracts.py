"""Shared v1 artifact, path, and atomic-write contracts."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class ScopeContract:
    """Canonical research choices proposed by an agent and frozen by the program."""

    research_questions: tuple[str, ...] = ()
    terminology: tuple[dict[str, Any], ...] = ()
    inclusion: tuple[str, ...] = ()
    exclusion: tuple[str, ...] = ()
    research_lines: tuple[dict[str, Any], ...] = ()
    comparison_dimensions: tuple[str, ...] = ()
    ranking_mode: str = "qualitative_fallback"
    priority_factors: tuple[dict[str, Any], ...] = ()
    tiers: tuple[str, ...] = ()
    ordering: tuple[str, ...] = ()
    missing_data_policy: str = "unknown_not_zero"
    contradiction_policy: str = "retain_and_surface"

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


@dataclass(frozen=True)
class TopicTask:
    task_id: str
    topic_path: str
    description: str
    round: int
    research_line_id: str | None = None
    execution_mode: str = "topic_coordinator"
    stage: str = "PLANNED"
    status: str = "PENDING"
    attempt: int = 0
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class TopicStage:
    task_id: str
    name: str
    status: str = "PENDING"
    invocation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentInvocation:
    invocation_id: str
    task_id: str
    role: str
    stage: str
    attempt: int
    status: str = "PENDING"
    staging_path: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    diagnostic: str | None = None


@dataclass(frozen=True)
class Issue:
    issue_id: str
    kind: str
    severity: str
    target: str
    field: str
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)
    created_by_invocation: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    kind: str
    canonical_path: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalizationState:
    phase: str = "TOPIC_RESEARCH_COMPLETE"
    status: str = "PENDING"
    section_status: dict[str, str] = field(default_factory=dict)
    blocking_issue_ids: tuple[str, ...] = ()


def check_schema_version(
    value: dict[str, Any], label: str, *, allow_legacy: bool = True
) -> tuple[list[str], list[str]]:
    """Return errors and warnings for a versioned v1 JSON artifact."""
    observed = value.get("schema_version")
    if observed is None:
        if allow_legacy:
            return [], [f"{label} has no schema_version; treating it as legacy v1"]
        return [f"{label} is missing schema_version"], []
    if observed in {1, "1", "1.0"}:
        return [], []
    return [f"{label} uses unsupported schema_version {observed!r}"], []


def workspace_path(workspace: Path, value: str | Path) -> Path:
    """Resolve a workspace-relative path and reject traversal/absolute escape."""
    raw = Path(value)
    if raw.is_absolute():
        raise ValueError(f"workspace artifact path must be relative: {value}")
    root = workspace.resolve()
    resolved = (root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"workspace artifact path escapes workspace: {value}")
    return resolved


def atomic_write_text(path: Path, text: str) -> None:
    """Flush a same-directory temporary file and atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
