"""Single-writer lifecycle state, task view rendering, and issue ledger."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from slrharness.contracts import (
    SCHEMA_VERSION,
    AgentInvocation,
    Issue,
    TopicTask,
    atomic_write_json,
    atomic_write_text,
    workspace_path,
)

ISSUES_PATH = "artifacts/ISSUES.jsonl"
STAGING_ROOT = "artifacts/staging"
TERMINAL_TOPIC_STATUSES = {
    "COMPLETE",
    "COMPLETE_WITH_WARNINGS",
    "AWAITING_INTERVENTION",
    "FAILED",
}
TOPIC_STATUSES = {"PENDING", "RUNNING", *TERMINAL_TOPIC_STATUSES}
TOPIC_STAGES = {
    "PLANNED",
    "RESEARCH",
    "NOTES_NORMALIZED",
    "METADATA_FINISHED",
    "SYNTHESIS",
    "TERMINAL",
}
INVOCATION_STATUSES = {"PENDING", "RUNNING", "COMPLETE", "FAILED"}
ISSUE_TRANSITIONS = {
    "open": {"repair_dispatched", "unresolved"},
    "repair_dispatched": {"applied", "unresolved"},
    "applied": {"verified_closed", "unresolved", "repair_dispatched"},
    "unresolved": {"repair_dispatched"},
    "verified_closed": set(),
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def _state_path(workspace: Path) -> Path:
    return workspace / "SLR_STATE.json"


def load_state(workspace: Path) -> dict[str, Any]:
    path = _state_path(workspace)
    if not path.is_file():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("SLR_STATE.json must contain an object")
    return value


def save_state(workspace: Path, state: dict[str, Any]) -> None:
    atomic_write_json(_state_path(workspace), state)


def ensure_control_state(workspace: Path) -> dict[str, Any]:
    """Add the v2 control subtree without disturbing scope lifecycle history."""
    state = load_state(workspace)
    control = state.get("control")
    if not isinstance(control, dict):
        control = {}
    changed = "control" not in state
    defaults: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "research_rounds": [],
        "topic_tasks": {},
        "invocations": {},
        "open_blocking_issues": [],
        "migration": {"legacy_loaded": False, "at": None},
    }
    for key, value in defaults.items():
        if key not in control:
            control[key] = value
            changed = True
    state["control"] = control
    if changed:
        save_state(workspace, state)
    return state


def stable_task_id(topic_path: str) -> str:
    normalized = topic_path.replace("\\", "/").strip("/")
    label = re.sub(
        r"[^a-zA-Z0-9_-]+", "-", normalized.removeprefix("topics/").replace("/", "--")
    ).strip("-") or "topic"
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return f"{label}-{digest}"


def register_tasks(
    workspace: Path,
    tasks: Iterable[Any],
    round_num: int,
    *,
    legacy_import: bool = False,
) -> list[dict[str, Any]]:
    """Import planning output once; afterwards JSON state is authoritative."""
    state = ensure_control_state(workspace)
    control = dict(state["control"])
    records = dict(control.get("topic_tasks") or {})
    registered: list[dict[str, Any]] = []
    for value in tasks:
        topic_path = str(getattr(value, "topic_path", ""))
        raw_path = Path(topic_path.replace("\\", "/"))
        if (
            raw_path.is_absolute()
            or ".." in raw_path.parts
            or not raw_path.parts
            or raw_path.parts[0] != "topics"
        ):
            raise ValueError(f"unsafe topic path: {topic_path}")
        task_id = stable_task_id(topic_path)
        prior = records.get(task_id)
        if isinstance(prior, dict):
            registered.append(prior)
            continue
        record = asdict(
            TopicTask(
                task_id=task_id,
                topic_path=topic_path,
                description=str(getattr(value, "description", "")),
                round=round_num,
                research_line_id=getattr(value, "research_line_id", None),
                execution_mode=getattr(value, "execution_mode", None)
                or "topic_coordinator",
            )
        )
        record["diagnostics"] = list(record["diagnostics"])
        records[task_id] = record
        registered.append(record)
    rounds = list(control.get("research_rounds") or [])
    if not any(isinstance(item, dict) and item.get("round") == round_num for item in rounds):
        rounds.append({"round": round_num, "status": "RUNNING", "started_at": now()})
    control.update({"topic_tasks": records, "research_rounds": rounds})
    if legacy_import:
        control["migration"] = {"legacy_loaded": True, "at": now()}
    state["control"] = control
    save_state(workspace, state)
    render_tasks_view(workspace, state)
    return registered


def migrate_legacy_tasks(workspace: Path) -> int:
    """Read old TASKS.md once, preserve old files, then switch to state writes."""
    state = ensure_control_state(workspace)
    migration = state["control"].get("migration") or {}
    if migration.get("legacy_loaded"):
        return 0
    path = workspace / "TASKS.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    records = dict(state["control"].get("topic_tasks") or {})
    round_num = 1
    seen_rounds: set[int] = set()
    added = 0
    for line in text.splitlines():
        round_match = re.match(r"^##\s+Round\s+(\d+)", line, re.IGNORECASE)
        if round_match:
            round_num = int(round_match.group(1))
            continue
        match = re.match(
            r"^\s*-\s*\[([ xX])\]\s+(topics/\S+?)\s+(?:—|--|-)\s+(.+)$",
            line,
        )
        if not match:
            continue
        checked, topic_path, description = match.groups()
        seen_rounds.add(round_num)
        mode_match = re.search(r"\[mode=([^\]]+)\]", description)
        line_match = re.search(r"\[line=([^\]]+)\]", description)
        clean = re.sub(r"\[(?:mode|line)=[^\]]+\]\s*", "", description).strip()
        identifier = stable_task_id(topic_path)
        if identifier in records:
            continue
        record = asdict(
            TopicTask(
                task_id=identifier,
                topic_path=topic_path,
                description=clean,
                round=round_num,
                execution_mode=(mode_match.group(1) if mode_match else "topic_coordinator"),
                research_line_id=(line_match.group(1) if line_match else None),
                stage="TERMINAL" if checked.lower() == "x" else "PLANNED",
                status="COMPLETE" if checked.lower() == "x" else "PENDING",
            )
        )
        record["diagnostics"] = []
        records[identifier] = record
        added += 1
    state["control"]["topic_tasks"] = records
    rounds = list(state["control"].get("research_rounds") or [])
    known_rounds = {
        int(item.get("round", 0)) for item in rounds if isinstance(item, dict)
    }
    for legacy_round in sorted(seen_rounds - known_rounds):
        rounds.append(
            {
                "round": legacy_round,
                "status": "MIGRATED",
                "started_at": None,
                "completed_at": None,
            }
        )
    state["control"]["research_rounds"] = rounds
    state["control"]["migration"] = {"legacy_loaded": True, "at": now()}
    save_state(workspace, state)
    render_tasks_view(workspace, state)
    return added


def update_round_status(workspace: Path, round_num: int, status: str) -> None:
    if status not in {"RUNNING", "COMPLETE", "AWAITING_INTERVENTION", "FAILED"}:
        raise ValueError(f"invalid round status: {status}")
    state = ensure_control_state(workspace)
    rounds = list(state["control"].get("research_rounds") or [])
    found = False
    for item in rounds:
        if isinstance(item, dict) and item.get("round") == round_num:
            item["status"] = status
            if status in {"COMPLETE", "AWAITING_INTERVENTION", "FAILED"}:
                item["completed_at"] = now()
            found = True
    if not found:
        rounds.append(
            {
                "round": round_num,
                "status": status,
                "started_at": now(),
                "completed_at": now() if status != "RUNNING" else None,
            }
        )
    state["control"]["research_rounds"] = rounds
    save_state(workspace, state)


def topic_tasks(
    workspace: Path, *, status: str | None = None, round_num: int | None = None
) -> list[dict[str, Any]]:
    state = ensure_control_state(workspace)
    values = [
        item
        for item in (state["control"].get("topic_tasks") or {}).values()
        if isinstance(item, dict)
    ]
    if status is not None:
        values = [item for item in values if item.get("status") == status]
    if round_num is not None:
        values = [item for item in values if item.get("round") == round_num]
    return sorted(values, key=lambda item: (int(item.get("round", 0)), str(item.get("task_id"))))


def update_topic_task(workspace: Path, task_id: str, **updates: Any) -> dict[str, Any]:
    protected = {"task_id", "round"}
    if protected & updates.keys():
        raise ValueError("task_id and round are immutable")
    if "status" in updates and updates["status"] not in TOPIC_STATUSES:
        raise ValueError(f"invalid topic status: {updates['status']}")
    if "stage" in updates and updates["stage"] not in TOPIC_STAGES:
        raise ValueError(f"invalid topic stage: {updates['stage']}")
    state = ensure_control_state(workspace)
    records = dict(state["control"].get("topic_tasks") or {})
    if task_id not in records:
        raise KeyError(task_id)
    record = dict(records[task_id])
    record.update(updates)
    records[task_id] = record
    state["control"]["topic_tasks"] = records
    save_state(workspace, state)
    render_tasks_view(workspace, state)
    return record


def render_tasks_view(workspace: Path, state: dict[str, Any] | None = None) -> None:
    """Render the human-readable TASKS.md projection from canonical state."""
    state = state or ensure_control_state(workspace)
    theme = str(state.get("theme") or state.get("initial_topic") or "Literature Review")
    tasks = [
        item
        for item in (state.get("control", {}).get("topic_tasks") or {}).values()
        if isinstance(item, dict)
    ]
    rounds = sorted({int(item.get("round", 0)) for item in tasks} or {1})
    lines = [f"# Tasks: {theme}", "", "> Program-rendered view. SLR_STATE.json is authoritative.", ""]
    for round_num in rounds:
        current = [item for item in tasks if int(item.get("round", 0)) == round_num]
        lines.extend([f"## Round {round_num}", "", "### Pending", ""])
        pending = [item for item in current if item.get("status") == "PENDING"]
        lines.extend(_task_lines(pending, False) or ["(none)"])
        lines.extend(["", "### Completed", ""])
        done = [item for item in current if item.get("status") in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}]
        lines.extend(_task_lines(done, True) or ["(none)"])
        lines.extend(["", "### Blocked", ""])
        blocked = [item for item in current if item.get("status") in {"AWAITING_INTERVENTION", "FAILED"}]
        lines.extend(_task_lines(blocked, False) or ["(none)"])
        lines.append("")
    atomic_write_text(workspace / "TASKS.md", "\n".join(lines).rstrip() + "\n")


def _task_lines(tasks: list[dict[str, Any]], checked: bool) -> list[str]:
    marker = "x" if checked else " "
    result = []
    for item in sorted(tasks, key=lambda value: str(value.get("task_id"))):
        line = item.get("research_line_id") or "NOT_APPLICABLE"
        mode = item.get("execution_mode") or "topic_coordinator"
        result.append(
            f"- [{marker}] {item['topic_path']} -- [mode={mode}] [line={line}] "
            f"{item.get('description', '')} <!-- task_id={item['task_id']} "
            f"stage={item.get('stage', 'PLANNED')} -->"
        )
    return result


def record_invocation(workspace: Path, invocation: AgentInvocation) -> None:
    if invocation.status not in INVOCATION_STATUSES:
        raise ValueError(f"invalid invocation status: {invocation.status}")
    state = ensure_control_state(workspace)
    invocations = dict(state["control"].get("invocations") or {})
    invocations[invocation.invocation_id] = asdict(invocation)
    state["control"]["invocations"] = invocations
    save_state(workspace, state)


def staging_directory(workspace: Path, invocation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", invocation_id):
        raise ValueError("unsafe invocation ID")
    path = workspace / STAGING_ROOT / invocation_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_issue_events(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / ISSUES_PATH
    if not path.is_file():
        return []
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"issue ledger line {number} is not an object")
        events.append(value)
    return events


def current_issues(workspace: Path) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for event in _read_issue_events(workspace):
        issue_id = str(event.get("issue_id") or "")
        if issue_id:
            current[issue_id] = event
    return current


def _append_issue(workspace: Path, value: dict[str, Any]) -> None:
    path = workspace / ISSUES_PATH
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic_write_text(path, existing + json.dumps(value, ensure_ascii=False) + "\n")
    state = ensure_control_state(workspace)
    open_blockers = sorted(
        issue_id
        for issue_id, issue in current_issues(workspace).items()
        if issue.get("severity") == "blocking" and issue.get("status") != "verified_closed"
    )
    state["control"]["open_blocking_issues"] = open_blockers
    save_state(workspace, state)


def issue_id(kind: str, target: str, field_name: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{target}\0{field_name}".encode()).hexdigest()[:12]
    return f"I-{digest.upper()}"


def ingest_metadata_observation(
    workspace: Path, observation: dict[str, Any], invocation_id: str
) -> list[Issue]:
    """Convert checker observations to program-owned issues; ignore agent status."""
    target = str(observation.get("note_path") or "")
    checked = observation.get("checked_fields")
    if not target or not isinstance(checked, dict):
        raise ValueError("metadata observation needs note_path and checked_fields")
    target_path = workspace_path(workspace, target)
    if not target_path.is_file() or not target_path.suffix.lower() == ".md":
        raise ValueError("metadata observation target must be an existing Markdown note")
    target = target_path.relative_to(workspace.resolve()).as_posix()
    created: list[Issue] = []
    existing = current_issues(workspace)
    for field_name, details in checked.items():
        if not isinstance(details, dict) or details.get("match") is not False:
            continue
        identifier = issue_id("metadata", target, str(field_name))
        if identifier in existing:
            continue
        issue = Issue(
            issue_id=identifier,
            kind="metadata",
            severity="blocking",
            target=target,
            field=str(field_name),
            status="open",
            evidence={
                "observed": details.get("observed"),
                "verified": details.get("verified"),
                "source": details.get("source"),
            },
            created_by_invocation=invocation_id,
            timestamp=now(),
        )
        _append_issue(workspace, asdict(issue))
        created.append(issue)
    return created


def apply_verification_observation(
    workspace: Path, observation: dict[str, Any]
) -> list[str]:
    """Let the program, never the checker, close applied metadata issues."""
    target = str(observation.get("note_path") or "")
    checked = observation.get("checked_fields")
    if not isinstance(checked, dict):
        raise ValueError("metadata observation needs checked_fields")
    changed: list[str] = []
    for identifier, issue in current_issues(workspace).items():
        if issue.get("target") != target or issue.get("status") != "applied":
            continue
        details = checked.get(str(issue.get("field")))
        if not isinstance(details, dict):
            continue
        status = "verified_closed" if details.get("match") is True else "unresolved"
        transition_issue(
            workspace,
            identifier,
            status,
            verification={
                "observed": details.get("observed"),
                "verified": details.get("verified"),
                "source": details.get("source"),
            },
        )
        changed.append(identifier)
    return changed


def transition_issue(
    workspace: Path, issue_id_value: str, target_status: str, **diagnostic: Any
) -> dict[str, Any]:
    current = current_issues(workspace).get(issue_id_value)
    if current is None:
        raise KeyError(issue_id_value)
    previous = str(current.get("status"))
    if target_status not in ISSUE_TRANSITIONS.get(previous, set()):
        raise ValueError(f"illegal issue transition: {previous} -> {target_status}")
    event = {**current, **diagnostic, "status": target_status, "timestamp": now()}
    _append_issue(workspace, event)
    return event


def blocking_issue_ids(workspace: Path, *, target_prefix: str | None = None) -> list[str]:
    return sorted(
        issue_id_value
        for issue_id_value, issue in current_issues(workspace).items()
        if issue.get("severity") == "blocking"
        and issue.get("status") != "verified_closed"
        and (target_prefix is None or str(issue.get("target", "")).startswith(target_prefix))
    )
