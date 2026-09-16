"""Read-only environment and persisted-project diagnostics."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slrharness.contracts import check_schema_version
from slrharness.finalization import (
    FINAL_AUDIT_PATH,
    PREFINAL_AUDIT_PATH,
    FinalizationConfig,
    validate_final_report,
)
from slrharness.project_lock import LOCK_FILENAME, project_lock_path
from slrharness.scope_workflow import load_scope_state
from slrharness.source_registry import (
    DEDUP_AUDIT_PATH,
    NOTE_AUDIT_PATH,
    REGISTRY_PATH,
)
from slrharness.topic_execution import topic_paths, validate_coordinator_outputs
from slrharness.workspace_assets import (
    CLAUDE_AGENT_FILES,
    CLAUDE_SKILL_DIRS,
    CLAUDE_TEMPLATE_FILES,
    required_asset_sources,
)


@dataclass
class DiagnosticReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_json(
    path: Path, label: str, report: DiagnosticReport
) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.errors.append(f"missing {label}: {path}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f"invalid {label}: {path}: {exc}")
        return None
    if not isinstance(value, dict):
        report.errors.append(f"{label} must be a JSON object: {path}")
        return None
    errors, warnings = check_schema_version(value, label)
    report.errors.extend(errors)
    report.warnings.extend(warnings)
    return value


def doctor(workspace: Path | None = None) -> DiagnosticReport:
    """Check hard runtime requirements without performing searches or model calls."""
    report = DiagnosticReport()
    report.info["python"] = platform.python_version()
    report.info["platform"] = platform.platform()
    if sys.version_info < (3, 11):  # noqa: UP036 - doctor reports the runtime contract
        report.errors.append("Python 3.11 or newer is required")
    linux_target = (
        sys.platform.startswith("linux") or "microsoft" in platform.release().lower()
    )
    if not linux_target:
        report.errors.append(
            "the v1 runtime target is Linux or WSL; native Windows is unsupported"
        )
    for command in ("git", "tmux", "claude"):
        executable = shutil.which(command)
        report.info[command] = executable or "missing"
        if not executable:
            report.errors.append(f"required command is missing: {command}")
    claude = shutil.which("claude")
    if claude:
        try:
            result = subprocess.run(
                [claude, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            report.info["claude_version"] = (result.stdout or result.stderr).strip()[
                :300
            ]
            if result.returncode != 0:
                report.errors.append("Claude Code version check failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.errors.append(f"Claude Code version check failed: {exc}")
    missing_assets = [
        str(path) for path in required_asset_sources() if not path.is_file()
    ]
    report.info["packaged_assets"] = "complete" if not missing_assets else "incomplete"
    if missing_assets:
        report.errors.append(f"required packaged assets are missing: {missing_assets}")
    target = (workspace or Path.cwd()).resolve()
    report.info["workspace"] = str(target)
    if not target.is_dir() or not os.access(target, os.W_OK):
        report.errors.append(f"workspace is not writable: {target}")
    report.info["tavily"] = (
        "configured" if os.environ.get("TAVILY_API_KEY") else "not configured"
    )
    if not os.environ.get("TAVILY_API_KEY"):
        report.warnings.append("optional Tavily capability is not configured")
    report.warnings.append(
        "optional arXiv/scholarly/scholar/Tavily MCP connectivity was not probed; "
        "use `claude mcp list`"
    )
    return report


def validate_project(workspace: Path) -> DiagnosticReport:
    """Validate artifacts without changing state or invoking an agent."""
    workspace = workspace.resolve()
    report = DiagnosticReport(info={"workspace": str(workspace)})
    if not workspace.is_dir():
        report.errors.append(f"workspace does not exist: {workspace}")
        return report
    if not (workspace / ".git").is_dir():
        report.errors.append("workspace is not a Git repository")
    try:
        state = load_scope_state(workspace)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report.errors.append(f"invalid project state: {exc}")
        state = {}
    status = str(state.get("status") or "LEGACY_UNKNOWN")
    report.info["scope_status"] = status
    report.info["scope_revision"] = state.get("revision")
    if not state:
        report.warnings.append("legacy project has no readable SLR_STATE.json")
    elif status == "AWAITING_SCOPE_APPROVAL":
        for filename in ("SCOPE_PROPOSAL.md", "SCOPE_SOURCES.md"):
            if not (workspace / filename).is_file():
                report.errors.append(
                    f"scope state awaits approval but {filename} is missing"
                )
    elif status == "SCOPE_APPROVED":
        if not state.get("formal_research_allowed"):
            report.errors.append("approved scope does not allow formal research")
        if state.get("approved_revision") is None:
            report.warnings.append(
                "approved scope has no approved_revision (legacy project)"
            )
        if not (workspace / "SCOPE.md").is_file():
            report.errors.append("approved canonical SCOPE.md is missing")
    if status == "SCOPE_APPROVED" and not (workspace / "TASKS.md").is_file():
        report.errors.append("approved project is missing TASKS.md")
    deployed = [
        *(workspace / ".claude" / "agents" / name for name in CLAUDE_AGENT_FILES),
        *(
            workspace / ".claude" / "skills" / name / "SKILL.md"
            for name in CLAUDE_SKILL_DIRS
        ),
        *(workspace / ".claude" / "templates" / name for name in CLAUDE_TEMPLATE_FILES),
    ]
    missing_deployed = [
        str(path.relative_to(workspace)) for path in deployed if not path.is_file()
    ]
    if missing_deployed:
        report.warnings.append(
            f"workspace is missing deployed Claude assets: {missing_deployed}"
        )

    manifests = sorted((workspace / "topics").rglob("coordinator_manifest.json"))
    accepted = 0
    for manifest in manifests:
        rel_topic = manifest.parent.relative_to(workspace).as_posix()
        validation = validate_coordinator_outputs(
            workspace, topic_paths(workspace, rel_topic)
        )
        report.errors.extend(f"{rel_topic}: {item}" for item in validation.errors)
        report.warnings.extend(f"{rel_topic}: {item}" for item in validation.warnings)
        accepted += int(validation.accepted)
    report.info["coordinator_manifests"] = len(manifests)
    report.info["accepted_coordinators"] = accepted
    task_counts: dict[str, int] = {}
    recent_errors: list[str] = []
    for task_file in sorted((workspace / "topics").rglob("task.json")):
        task = _read_json(task_file, "topic task state", report)
        if not task:
            continue
        task_status = str(task.get("status") or "UNKNOWN").upper()
        task_counts[task_status] = task_counts.get(task_status, 0) + 1
        if task.get("last_error"):
            recent_errors.append(str(task["last_error"]))
    report.info["topic_task_statuses"] = task_counts
    report.info["recent_errors"] = recent_errors[-3:]
    topic_syntheses = (
        [
            path
            for path in (workspace / "topics").rglob("*.md")
            if all(
                part not in {"papers", "technical_sources", "audits"}
                for part in path.parts
            )
            and path.name not in {"INDEX.md", "NO_RESULTS.md"}
        ]
        if (workspace / "topics").is_dir()
        else []
    )
    if topic_syntheses and not manifests:
        report.warnings.append("legacy topic syntheses have no coordinator manifests")
    report.info["topic_syntheses"] = len(topic_syntheses)

    registry = None
    if (workspace / REGISTRY_PATH).is_file():
        registry = _read_json(workspace / REGISTRY_PATH, "source registry", report)
        for relative, label in (
            (NOTE_AUDIT_PATH, "paper-note validation"),
            (DEDUP_AUDIT_PATH, "source deduplication audit"),
            (PREFINAL_AUDIT_PATH, "pre-final audit"),
        ):
            if (workspace / relative).is_file():
                _read_json(workspace / relative, label, report)
    finalization = state.get("finalization") if isinstance(state, dict) else None
    phase = finalization.get("phase") if isinstance(finalization, dict) else None
    report.info["finalization_phase"] = phase or "NOT_STARTED"
    if phase == "COMPLETE":
        validation = validate_final_report(
            workspace, FinalizationConfig(), write_audit=False
        )
        report.errors.extend(validation.errors)
        report.warnings.extend(validation.warnings)
        final_audit = _read_json(workspace / FINAL_AUDIT_PATH, "final audit", report)
        if final_audit and final_audit.get("status") not in {
            "PASS",
            "PASS_WITH_WARNINGS",
        }:
            report.errors.append("project is COMPLETE but final audit is not accepted")
    if project_lock_path(workspace).exists():
        report.warnings.append(f"workspace has an active or stale {LOCK_FILENAME}")
    if registry:
        report.info["papers"] = len(registry.get("papers", []))
        report.info["technical_sources"] = len(registry.get("technical_sources", []))
        report.info["unresolved_metadata"] = len(
            registry.get("unresolved_metadata", [])
        )
    if status == "AWAITING_SCOPE_APPROVAL":
        report.info["next_step"] = "slrharness show PROJECT, then revise or approve"
    elif phase == "COMPLETE" and report.ok:
        report.info["next_step"] = "review SUMMARY.md and final_audit.json"
    elif report.errors:
        report.info["next_step"] = (
            "fix listed artifact errors, then run slrharness validate PROJECT"
        )
    else:
        report.info["next_step"] = "slrharness run --workspace PROJECT"
    return report


def render_report(report: DiagnosticReport) -> str:
    lines = [f"RESULT: {'PASS' if report.ok else 'FAIL'}"]
    lines.extend(f"INFO {key}: {value}" for key, value in report.info.items())
    lines.extend(f"WARNING: {item}" for item in report.warnings)
    lines.extend(f"ERROR: {item}" for item in report.errors)
    return "\n".join(lines)
