"""Validated, backwards-compatible configuration loading for v1."""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from slrharness.agent_backends import available_backends
from slrharness.contracts import SCHEMA_VERSION, check_schema_version
from slrharness.finalization import FinalizationConfig
from slrharness.scope_workflow import ScopeConfig
from slrharness.topic_execution import TopicExecutionConfig

FIXED_SECTIONS = {
    "academic_worker",
    "metadata_checker",
    "technical_worker",
    "communication",
    "paper_notes",
    "source_registry",
}


@dataclass(frozen=True)
class HarnessConfig:
    scope: ScopeConfig
    topic_execution: TopicExecutionConfig
    finalization: FinalizationConfig
    backend: str
    fixed_contracts: dict[str, dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": asdict(self.scope),
            "topic_execution": asdict(self.topic_execution),
            "finalization": asdict(self.finalization),
            "backends": {"default": self.backend},
            **self.fixed_contracts,
        }


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"configuration section {name!r} must be an object")
    return value


def load_config(path: Path) -> HarnessConfig:
    """Load JSON/TOML; missing v1 fields get defaults and unknown fields fail."""
    try:
        if path.suffix.lower() == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot load configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("configuration root must be an object")
    errors, _ = check_schema_version(data, "configuration")
    if errors:
        raise ValueError("; ".join(errors))
    allowed = {
        "schema_version",
        "scope",
        "topic_execution",
        "finalization",
        "backends",
        *FIXED_SECTIONS,
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown configuration sections: {unknown}")
    try:
        scope = ScopeConfig(**_section(data, "scope"))
        topic = TopicExecutionConfig(**_section(data, "topic_execution"))
        finalization = FinalizationConfig(**_section(data, "finalization"))
    except TypeError as exc:
        raise ValueError(f"unknown or invalid configuration field: {exc}") from exc
    backends = _section(data, "backends")
    if set(backends) - {"default"}:
        raise ValueError("backends supports only the 'default' field in v1")
    backend = str(backends.get("default", "claude-code"))
    if backend not in available_backends():
        raise ValueError(f"unknown agent backend: {backend}")
    fixed = {name: _section(data, name) for name in sorted(FIXED_SECTIONS)}
    return HarnessConfig(scope, topic, finalization, backend, fixed)
