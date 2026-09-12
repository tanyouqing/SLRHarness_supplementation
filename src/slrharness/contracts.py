"""Shared v1 artifact, path, and atomic-write contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


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
