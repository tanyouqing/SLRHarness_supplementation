"""Deterministic topic-coordinator state, prompts, and artifact validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEGACY_WORKER = "legacy_worker"
TOPIC_COORDINATOR = "topic_coordinator"
TOPIC_EXECUTION_MODES = (LEGACY_WORKER, TOPIC_COORDINATOR)


@dataclass(frozen=True)
class TopicExecutionConfig:
    """Bounded settings for one topic-coordinator execution."""

    mode: str = LEGACY_WORKER
    coordinator_agent: str = "topic-coordinator"
    allow_partial_completion: bool = True
    coordinator_timeout_seconds: int = 3600
    coordinator_max_turns: int = 80
    coordinator_retries: int = 1
    academic_agent: str = "academic-paper-worker"
    academic_max_turns: int = 35
    target_papers: int = 10
    max_paper_candidates: int = 30
    enable_backward_citation_search: bool = True
    enable_forward_citation_search: bool = True
    metadata_agent: str = "academic-metadata-checker"
    metadata_max_turns: int = 20
    max_correction_rounds: int = 2
    unresolved_metadata_is_fatal: bool = False
    technical_agent: str = "technical-source-worker"
    technical_max_turns: int = 25
    target_technical_sources: int = 5
    max_technical_candidates: int = 15
    message_wait_seconds: int = 300
    enable_send_message: bool = False
    persist_coordination_log: bool = True
    file_fallback: bool = True

    def __post_init__(self) -> None:
        if self.mode not in TOPIC_EXECUTION_MODES:
            raise ValueError(f"Unknown topic execution mode: {self.mode}")
        bounded = {
            "coordinator_timeout_seconds": self.coordinator_timeout_seconds,
            "coordinator_max_turns": self.coordinator_max_turns,
            "academic_max_turns": self.academic_max_turns,
            "metadata_max_turns": self.metadata_max_turns,
            "technical_max_turns": self.technical_max_turns,
            "max_correction_rounds": self.max_correction_rounds,
            "message_wait_seconds": self.message_wait_seconds,
        }
        invalid = [name for name, value in bounded.items() if value <= 0]
        if invalid:
            raise ValueError(
                "Topic execution limits must be positive: " + ", ".join(invalid)
            )
        if self.coordinator_retries < 0:
            raise ValueError("coordinator_retries must be non-negative")
        for value in (
            self.target_papers,
            self.max_paper_candidates,
            self.target_technical_sources,
            self.max_technical_candidates,
        ):
            if value < 0:
                raise ValueError("Topic search targets must be non-negative")


@dataclass(frozen=True)
class TopicPaths:
    task_id: str
    topic_path: str
    synthesis: Path
    artifact_root: Path
    task_state: Path
    paper_dir: Path
    technical_dir: Path
    audit_dir: Path
    manifest: Path
    coordination_log: Path


@dataclass(frozen=True)
class CoordinatorValidation:
    accepted: bool
    effective_status: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    manifest: dict[str, Any] | None = field(default=None, repr=False)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def task_id_for_topic_path(topic_path: str) -> str:
    """Create a stable readable task ID without relying on dynamic numbering."""
    normalized = topic_path.replace("\\", "/").strip("/")
    label = normalized.removeprefix("topics/").replace("/", "--")
    label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "topic"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{label}-{digest}"


def topic_paths(workspace: Path, topic_path: str) -> TopicPaths:
    """Map a legacy synthesis path to its adjacent supporting-artifact tree."""
    normalized = Path(topic_path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"Unsafe topic path: {topic_path}")
    if not normalized.parts or normalized.parts[0] != "topics":
        raise ValueError(f"Topic path must start with topics/: {topic_path}")
    artifact_root = (workspace / normalized).resolve()
    topics_root = (workspace / "topics").resolve()
    if not artifact_root.is_relative_to(topics_root):
        raise ValueError(f"Topic path escapes topics/: {topic_path}")
    synthesis = artifact_root.with_suffix(".md")
    return TopicPaths(
        task_id=task_id_for_topic_path(topic_path),
        topic_path=normalized.as_posix(),
        synthesis=synthesis,
        artifact_root=artifact_root,
        task_state=artifact_root / "task.json",
        paper_dir=artifact_root / "papers",
        technical_dir=artifact_root / "technical_sources",
        audit_dir=artifact_root / "audits",
        manifest=artifact_root / "coordinator_manifest.json",
        coordination_log=artifact_root / "coordination_log.jsonl",
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def initialize_topic_attempt(
    paths: TopicPaths,
    description: str,
    round_num: int,
    attempt: int,
    config: TopicExecutionConfig,
    previous_diagnostics: list[str] | None = None,
) -> dict[str, Any]:
    """Create directories and atomically record a coordinator attempt."""
    for directory in (paths.paper_dir, paths.technical_dir, paths.audit_dir):
        directory.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "task_id": paths.task_id,
        "topic_path": paths.topic_path,
        "description": description,
        "execution_mode": TOPIC_COORDINATOR,
        "coordinator_agent": config.coordinator_agent,
        "round": round_num,
        "attempt": attempt,
        "status": "RUNNING",
        "started_at": utc_now(),
        "completed_at": None,
        "last_error": None,
        "diagnostics": list(previous_diagnostics or []),
        "process": {
            "kind": "tmux",
            "session": None,
            "window": None,
            "pane_pid": None,
            "exit_code": None,
            "timed_out": False,
        },
        "config": asdict(config),
    }
    _atomic_write_json(paths.task_state, state)
    return state


def record_topic_process(
    paths: TopicPaths, state: dict[str, Any], session: str, window: str, pid: int | None
) -> dict[str, Any]:
    state = dict(state)
    process = dict(state["process"])
    process.update({"session": session, "window": window, "pane_pid": pid})
    state["process"] = process
    _atomic_write_json(paths.task_state, state)
    return state


def finish_topic_attempt(
    paths: TopicPaths,
    state: dict[str, Any],
    status: str,
    exit_code: int | None,
    timed_out: bool,
    diagnostics: list[str],
) -> None:
    """Atomically replace model-visible task state with program-owned result."""
    state = dict(state)
    process = dict(state["process"])
    process.update({"exit_code": exit_code, "timed_out": timed_out})
    state.update(
        {
            "status": status,
            "completed_at": utc_now(),
            "last_error": "; ".join(diagnostics) if diagnostics else None,
            "diagnostics": diagnostics,
            "process": process,
        }
    )
    _atomic_write_json(paths.task_state, state)


def build_coordinator_prompt(
    workspace: Path,
    paths: TopicPaths,
    description: str,
    round_num: int,
    attempt: int,
    config: TopicExecutionConfig,
    retry_diagnostics: list[str] | None = None,
) -> str:
    """Build the complete per-task contract for the top-level coordinator."""
    diagnostics = retry_diagnostics or []
    return f"""You are the single top-level Topic Coordinator for one bounded SLR task.

TASK ID: {paths.task_id}
ROUND: {round_num}
ATTEMPT: {attempt}
TASK: {description}
WORKSPACE: {workspace}
APPROVED SCOPE: {workspace / "SCOPE.md"}
LEGACY-COMPATIBLE TOPIC SYNTHESIS: {paths.synthesis}
SUPPORTING ARTIFACT ROOT: {paths.artifact_root}
PROGRAM-OWNED TASK STATE: {paths.task_state}

Read the approved scope, the task, existing supporting artifacts, and the
preloaded `slr-topic-research` skill. Preserve valid files from earlier
attempts. Never modify SCOPE.md, SCOPE_ORIGINAL.md, TASKS.md, SUMMARY.md,
SLR_STATE.json, task.json, another topic, Git state, or harness configuration.

OUTER/INNER BOUNDARY:
- You are the only top-level Claude CLI process for this topic.
- Use the ordinary Claude Code `Agent` tool to invoke exactly these named
  subagent types: `{config.academic_agent}`, `{config.technical_agent}`, and
  `{config.metadata_agent}`. Do not create an Agent Team, team task list, or
  external Claude process.
- Start `{config.academic_agent}` and `{config.technical_agent}` concurrently
  as background subagents. Their write trees do not overlap.
- Wait at most {config.message_wait_seconds}s per bounded coordination step.
- After the academic worker creates `papers/index.json` or an explicit
  no-result record, invoke `{config.metadata_agent}` for independent metadata
  verification.
- Ordinary subagents cannot directly peer-message in the supported non-team
  mode. The checker writes `audits/correction_requests.jsonl`; you then invoke
  `{config.academic_agent}` again with only the pending corrections, and invoke
  the checker again. Stop after {config.max_correction_rounds} correction
  rounds. Record every handoff in `coordination_log.jsonl`.
- Maximum-turn budgets: coordinator {config.coordinator_max_turns}, academic
  {config.academic_max_turns}, technical {config.technical_max_turns}, metadata
  checker {config.metadata_max_turns}. Never wait or revise indefinitely.

ACADEMIC WORK CONTRACT:
- Soft target {config.target_papers} included papers from at most
  {config.max_paper_candidates} initial candidates.
- Bounded backward citation search: {config.enable_backward_citation_search}.
- Bounded forward citation search: {config.enable_forward_citation_search}.
- One stable `<year>-<first-author>-<short-title>.md` note per included paper,
  merging duplicate preprint/conference/journal versions.
- Produce `papers/index.json` and `papers/INDEX.md`, or a clear no-result record.

TECHNICAL WORK CONTRACT:
- Soft target {config.target_technical_sources} sources from at most
  {config.max_technical_candidates} candidates.
- Keep technical evidence explicitly non-peer-reviewed unless it truly is a
  paper. Produce one stable note per resource plus
  `technical_sources/index.json` and `technical_sources/INDEX.md`, or a clear
  no-result record.

METADATA CHECK CONTRACT:
- Independently check only title, authors, year, venue/publication, DOI, arXiv
  ID, URL, version relationships, duplicates, and note-to-paper identity.
- Do not check methods, experiments, result numbers, conclusions, or analysis.
- Write `audits/metadata_check.json` and a readable metadata_check.md. Valid
  item statuses: PASS, CORRECTED, UNRESOLVED, NOT_CHECKED. Valid overall
  statuses: PASS, PARTIAL, FAILED.

REQUIRED FINAL OUTPUTS:
- Existing-compatible synthesis: `{paths.synthesis}`.
- Supporting tree under `{paths.artifact_root}` with paper index/no-result,
  technical index/no-result, parseable metadata audit, coordination log, and
  `coordinator_manifest.json`.
- The synthesis must distinguish papers from technical sources, link claims to
  supporting note paths, and disclose PARTIAL/UNRESOLVED metadata.
- Write the manifest last, after validating all other outputs. Its task_id
  must be exactly `{paths.task_id}`; status must be COMPLETE, PARTIAL,
  or FAILED; paths must be workspace-relative. Include topic_synthesis,
  academic_worker.index_path and paper_count,
  metadata_checker.audit_path and overall_status,
  technical_worker.index_path and source_count, limitations, and completed_at.

MCP AND NETWORK FALLBACK:
- Academic and metadata roles prefer configured scholarly/arXiv tools, then
  WebSearch/WebFetch. Technical work prefers configured Tavily, then
  WebSearch/WebFetch. Missing MCPs, rate limits, empty results, inaccessible
  pages, and missing Tavily keys are non-fatal.
- If retrieval is unavailable, write explicit no-result/limited-access records.
  Never fabricate papers, metadata, access depth, sources, or results. Mark
  unverifiable content `[UNVERIFIED]`.

RETRY DIAGNOSTICS FROM THE HARNESS:
{json.dumps(diagnostics, ensure_ascii=False, indent=2)}

Before finishing, validate every required path and count. Do not claim success
from subagent prose alone. Do not generate the global Summary or final review.
"""


def _load_json(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        errors.append(f"missing {label}: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"invalid {label}: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"invalid {label}: expected a JSON object")
        return None
    return data


def _resolve_manifest_path(workspace: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate.resolve()


def _supporting_path(
    workspace: Path,
    artifact_root: Path,
    value: Any,
    label: str,
    errors: list[str],
) -> Path | None:
    path = _resolve_manifest_path(workspace, value)
    if path is None:
        errors.append(f"manifest is missing {label}")
        return None
    if not path.is_relative_to(artifact_root.resolve()):
        errors.append(f"{label} escapes the task artifact root: {value}")
        return None
    if not path.is_file():
        errors.append(f"{label} does not exist: {value}")
        return None
    return path


def _manifest_nested(manifest: dict[str, Any], section: str, key: str) -> Any:
    value = manifest.get(section)
    return value.get(key) if isinstance(value, dict) else None


def validate_coordinator_outputs(
    workspace: Path,
    paths: TopicPaths,
    config: TopicExecutionConfig | bool = True,
) -> CoordinatorValidation:
    """Validate only deterministic structure, identity, paths, counts, and status."""
    allow_partial = (
        config.allow_partial_completion
        if isinstance(config, TopicExecutionConfig)
        else config
    )
    unresolved_is_fatal = (
        config.unresolved_metadata_is_fatal
        if isinstance(config, TopicExecutionConfig)
        else False
    )
    require_coordination_log = (
        config.persist_coordination_log
        if isinstance(config, TopicExecutionConfig)
        else True
    )
    errors: list[str] = []
    warnings: list[str] = []
    if not paths.synthesis.is_file():
        errors.append(f"missing topic synthesis: {paths.synthesis}")
    elif len(paths.synthesis.read_text(encoding="utf-8").strip()) < 200:
        errors.append("topic synthesis is empty or only a placeholder")

    manifest = _load_json(paths.manifest, "coordinator manifest", errors)
    if manifest is None:
        return CoordinatorValidation(False, "FAILED", tuple(errors), tuple(warnings))
    if manifest.get("task_id") != paths.task_id:
        errors.append(
            f"manifest task_id mismatch: expected {paths.task_id}, "
            f"got {manifest.get('task_id')!r}"
        )
    if require_coordination_log and not paths.coordination_log.is_file():
        errors.append("missing coordination_log.jsonl")
    correction_queue = paths.audit_dir / "correction_requests.jsonl"
    if not correction_queue.is_file():
        errors.append("missing audits/correction_requests.jsonl")
    status = str(manifest.get("status", ""))
    if status not in ("COMPLETE", "PARTIAL", "FAILED"):
        errors.append(f"manifest has invalid or running status: {status!r}")
    elif status == "FAILED":
        errors.append("coordinator manifest reports FAILED")
    elif status == "PARTIAL" and not allow_partial:
        errors.append("PARTIAL completion is disabled")

    synthesis_path = _resolve_manifest_path(workspace, manifest.get("topic_synthesis"))
    if synthesis_path != paths.synthesis.resolve():
        errors.append(
            "manifest topic_synthesis does not match the assigned output path"
        )

    paper_index = _supporting_path(
        workspace,
        paths.artifact_root,
        _manifest_nested(manifest, "academic_worker", "index_path"),
        "academic_worker.index_path",
        errors,
    )
    technical_index = _supporting_path(
        workspace,
        paths.artifact_root,
        _manifest_nested(manifest, "technical_worker", "index_path"),
        "technical_worker.index_path",
        errors,
    )
    audit_path = _supporting_path(
        workspace,
        paths.artifact_root,
        _manifest_nested(manifest, "metadata_checker", "audit_path"),
        "metadata_checker.audit_path",
        errors,
    )

    paper_data = (
        _load_json(paper_index, "paper index", errors)
        if paper_index and paper_index.suffix.lower() == ".json"
        else None
    )
    if paper_data and paper_data.get("task_id") != paths.task_id:
        errors.append("paper index task_id mismatch")
    if paper_index and paper_index.suffix.lower() != ".json":
        no_result = paths.paper_dir / "NO_RESULTS.md"
        if paper_index != no_result.resolve():
            errors.append("paper index must be JSON unless it is NO_RESULTS.md")
    technical_data = (
        _load_json(technical_index, "technical index", errors)
        if technical_index and technical_index.suffix.lower() == ".json"
        else None
    )
    if technical_data and technical_data.get("task_id") != paths.task_id:
        errors.append("technical index task_id mismatch")
    if technical_index and technical_index.suffix.lower() != ".json":
        no_result = paths.technical_dir / "NO_RESULTS.md"
        if technical_index != no_result.resolve():
            errors.append("technical index must be JSON unless it is NO_RESULTS.md")
    audit = _load_json(audit_path, "metadata audit", errors) if audit_path else None
    if audit and audit.get("task_id") != paths.task_id:
        errors.append("metadata audit task_id mismatch")

    manifest_paper_count = _manifest_nested(manifest, "academic_worker", "paper_count")
    index_paper_count = paper_data.get("paper_count") if paper_data else 0
    if not isinstance(manifest_paper_count, int) or manifest_paper_count < 0:
        errors.append("manifest academic paper_count must be a non-negative integer")
    elif manifest_paper_count != index_paper_count:
        warnings.append(
            "paper count mismatch between coordinator manifest and paper index"
        )
    if paper_data:
        papers = paper_data.get("papers")
        if not isinstance(papers, list):
            errors.append("paper index papers must be a list")
        else:
            if isinstance(index_paper_count, int) and len(papers) != index_paper_count:
                warnings.append("paper index paper_count does not match papers")
            for item in papers:
                note_value = item.get("note_path") if isinstance(item, dict) else None
                note_path = _resolve_manifest_path(workspace, note_value)
                if (
                    note_path is None
                    or not note_path.is_relative_to(paths.paper_dir.resolve())
                    or not note_path.is_file()
                ):
                    errors.append(f"invalid paper note path: {note_value!r}")

    if audit:
        audit_status = audit.get("overall_status")
        if audit_status not in ("PASS", "PARTIAL", "FAILED"):
            errors.append(f"metadata audit has invalid status: {audit_status!r}")
        elif audit_status == "FAILED":
            errors.append("metadata audit reports FAILED")
        elif audit_status == "PARTIAL" and not allow_partial:
            errors.append("PARTIAL metadata audit is disabled")
        audit_count = audit.get("paper_count")
        if not isinstance(audit_count, int) or audit_count < 0:
            errors.append("metadata audit paper_count must be a non-negative integer")
        elif (
            isinstance(manifest_paper_count, int)
            and audit_count != manifest_paper_count
        ):
            warnings.append(
                "paper count mismatch between paper index and metadata audit"
            )
        items = audit.get("items", [])
        if not isinstance(items, list):
            errors.append("metadata audit items must be a list")
            items = []
        allowed_items = {"PASS", "CORRECTED", "UNRESOLVED", "NOT_CHECKED"}
        invalid_items = [
            item.get("status")
            for item in items
            if not isinstance(item, dict) or item.get("status") not in allowed_items
        ]
        if invalid_items:
            errors.append(f"metadata audit has invalid item statuses: {invalid_items}")
        derived = {
            "checked_count": len(items),
            "passed_count": sum(
                isinstance(item, dict) and item.get("status") == "PASS"
                for item in items
            ),
            "corrected_count": sum(
                isinstance(item, dict) and item.get("status") == "CORRECTED"
                for item in items
            ),
            "unresolved_count": sum(
                isinstance(item, dict)
                and item.get("status") in ("UNRESOLVED", "NOT_CHECKED")
                for item in items
            ),
        }
        for count_name, expected_count in derived.items():
            observed_count = audit.get(count_name)
            if not isinstance(observed_count, int) or observed_count < 0:
                errors.append(f"metadata audit {count_name} must be non-negative")
            elif observed_count != expected_count:
                warnings.append(f"metadata audit {count_name} does not match items")
        unresolved = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("status") in ("UNRESOLVED", "NOT_CHECKED")
        ]
        if unresolved:
            if not allow_partial or unresolved_is_fatal:
                errors.append("metadata audit contains unresolved items")
            else:
                warnings.append(
                    f"metadata audit contains {len(unresolved)} unresolved items"
                )
                synthesis_text = (
                    paths.synthesis.read_text(encoding="utf-8").lower()
                    if paths.synthesis.is_file()
                    else ""
                )
                if "unresolved" not in synthesis_text:
                    errors.append("synthesis does not disclose unresolved metadata")

    manifest_source_count = _manifest_nested(
        manifest, "technical_worker", "source_count"
    )
    index_source_count = technical_data.get("source_count") if technical_data else 0
    if not isinstance(manifest_source_count, int) or manifest_source_count < 0:
        errors.append("manifest technical source_count must be non-negative")
    elif manifest_source_count != index_source_count:
        warnings.append(
            "source count mismatch between coordinator manifest and technical index"
        )
    if technical_data:
        sources = technical_data.get("sources")
        if not isinstance(sources, list):
            errors.append("technical index sources must be a list")
        else:
            if (
                isinstance(index_source_count, int)
                and len(sources) != index_source_count
            ):
                warnings.append("technical index source_count does not match sources")
            for item in sources:
                note_value = item.get("note_path") if isinstance(item, dict) else None
                note_path = _resolve_manifest_path(workspace, note_value)
                if (
                    note_path is None
                    or not note_path.is_relative_to(paths.technical_dir.resolve())
                    or not note_path.is_file()
                ):
                    errors.append(f"invalid technical note path: {note_value!r}")

    effective_status = "PARTIAL" if warnings or status == "PARTIAL" else status
    if warnings and not allow_partial:
        errors.extend(warnings)
    accepted = not errors and effective_status in ("COMPLETE", "PARTIAL")
    return CoordinatorValidation(
        accepted,
        effective_status if accepted else "FAILED",
        tuple(errors),
        tuple(warnings),
        manifest,
    )
