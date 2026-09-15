"""Deterministic topic-coordinator state, prompts, and artifact validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slrharness.contracts import SCHEMA_VERSION, check_schema_version
from slrharness.control_plane import blocking_issue_ids, current_issues
from slrharness.prioritization import (
    PAPER_ROLES,
    PRIORITIZATION_PATH,
    PRIORITY_TIERS,
    load_scope_prioritization,
)
from slrharness.source_registry import (
    CORRECTABLE_FRONTMATTER_FIELDS,
    parse_frontmatter,
)

LEGACY_WORKER = "legacy_worker"
TOPIC_COORDINATOR = "topic_coordinator"
TOPIC_EXECUTION_MODES = (LEGACY_WORKER, TOPIC_COORDINATOR)
GENERATED_NOTE_NAMES = {"index.md", "no_results.md", "metadata_check.md"}


@dataclass(frozen=True)
class TopicExecutionConfig:
    """Bounded settings for one topic-coordinator execution."""

    mode: str = LEGACY_WORKER
    coordinator_agent: str = "topic-coordinator"
    allow_partial_completion: bool = True
    coordinator_timeout_seconds: int = 7200
    coordinator_max_turns: int = 120
    coordinator_retries: int = 1
    academic_agent: str = "academic-paper-worker"
    academic_max_turns: int = 50
    target_papers: int = 4
    max_paper_candidates: int = 12
    enable_backward_citation_search: bool = True
    enable_forward_citation_search: bool = True
    metadata_agent: str = "academic-metadata-checker"
    metadata_max_turns: int = 30
    max_correction_rounds: int = 1
    max_metadata_repairs: int = 6
    unresolved_metadata_is_fatal: bool = False
    technical_agent: str = "technical-source-worker"
    technical_max_turns: int = 40
    target_technical_sources: int = 1
    max_technical_candidates: int = 5
    message_wait_seconds: int = 600
    enable_send_message: bool = False
    persist_coordination_log: bool = True
    file_fallback: bool = True
    accept_valid_artifacts_after_process_failure: bool = True

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
        if self.coordinator_retries > 5 or self.max_correction_rounds > 5:
            raise ValueError("topic retries and correction rounds must not exceed 5")
        if self.coordinator_timeout_seconds > 86400 or self.message_wait_seconds > 3600:
            raise ValueError("topic timeout exceeds the v1 safety limit")
        if (
            max(
                self.coordinator_max_turns,
                self.academic_max_turns,
                self.metadata_max_turns,
                self.technical_max_turns,
            )
            > 200
        ):
            raise ValueError("agent max turns must not exceed 200")
        for value in (
            self.target_papers,
            self.max_paper_candidates,
            self.target_technical_sources,
            self.max_technical_candidates,
        ):
            if value < 0:
                raise ValueError("Topic search targets must be non-negative")
            if value > 1000:
                raise ValueError("Topic search targets exceed the v1 safety limit")


@dataclass(frozen=True)
class TopicPaths:
    task_id: str
    topic_path: str
    synthesis: Path
    artifact_root: Path
    task_state: Path
    task_contract: Path
    paper_dir: Path
    technical_dir: Path
    audit_dir: Path
    manifest: Path
    coordination_log: Path
    metadata_findings: Path
    correction_requests: Path
    normalization_report: Path
    checkpoint: Path


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
        task_contract=artifact_root / "task_contract.json",
        paper_dir=artifact_root / "papers",
        technical_dir=artifact_root / "technical_sources",
        audit_dir=artifact_root / "audits",
        manifest=artifact_root / "coordinator_manifest.json",
        coordination_log=artifact_root / "coordination_log.jsonl",
        metadata_findings=artifact_root / "audits" / "metadata_findings.jsonl",
        correction_requests=(
            artifact_root / "audits" / "correction_requests.jsonl"
        ),
        normalization_report=(
            artifact_root / "audits" / "artifact_normalization.json"
        ),
        checkpoint=artifact_root / "audits" / "checkpoint.json",
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
    research_line_id: str | None = None,
    prioritization_warnings: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Create directories and atomically record a coordinator attempt."""
    workspace = paths.artifact_root.parents[
        len(Path(paths.topic_path).parts) - 1
    ]
    previous_contract = _load_json(paths.task_contract, "task contract", []) or {}
    for directory in (paths.paper_dir, paths.technical_dir, paths.audit_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for durable_log in (
        paths.coordination_log,
        paths.metadata_findings,
        paths.correction_requests,
    ):
        durable_log.touch(exist_ok=True)
    current_history = {
        _workspace_relative(workspace, path): {
            "size": len(path.read_bytes()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (paths.metadata_findings, paths.correction_requests)
    }
    previous_history = previous_contract.get("jsonl_history")
    jsonl_history = current_history
    if isinstance(previous_history, dict):
        history_valid = True
        for relative, expected in previous_history.items():
            if not isinstance(expected, dict):
                continue
            path = workspace / relative
            current = path.read_bytes() if path.is_file() else b""
            size = int(expected.get("size", 0))
            if len(current) < size or hashlib.sha256(current[:size]).hexdigest() != str(
                expected.get("sha256")
            ):
                history_valid = False
                break
        if not history_valid:
            # Preserve the last trusted baseline so a retry cannot legitimize
            # an earlier overwrite merely by starting a new attempt.
            jsonl_history = previous_history
    task_contract = {
        "schema_version": SCHEMA_VERSION,
        "producer": "slrharness",
        "task_id": paths.task_id,
        "topic_path": paths.topic_path,
        "research_line_id": research_line_id,
        "attempt": attempt,
        "paths": {
            "artifact_root": _workspace_relative(workspace, paths.artifact_root),
            "paper_directory": _workspace_relative(workspace, paths.paper_dir),
            "technical_directory": _workspace_relative(workspace, paths.technical_dir),
            "metadata_findings": _workspace_relative(
                workspace, paths.metadata_findings
            ),
            "correction_requests": _workspace_relative(
                workspace, paths.correction_requests
            ),
            "coordination_log": _workspace_relative(
                workspace, paths.coordination_log
            ),
            "synthesis": _workspace_relative(workspace, paths.synthesis),
        },
        "jsonl_history": jsonl_history,
    }
    _atomic_write_json(paths.task_contract, task_contract)
    state = {
        "schema_version": SCHEMA_VERSION,
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
        "research_line_id": research_line_id,
        "prioritization_warnings": list(prioritization_warnings or []),
        "jsonl_history": jsonl_history,
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
    research_line_id: str | None = None,
    prioritization_warnings: tuple[str, ...] = (),
) -> str:
    """Build the complete per-task contract for the top-level coordinator."""
    diagnostics = retry_diagnostics or []
    from slrharness.artifact_compiler import build_checkpoint_inventory

    checkpoint = build_checkpoint_inventory(paths)
    contract = load_scope_prioritization(workspace)
    line_id = research_line_id or "NOT_APPLICABLE"
    line = next(
        (
            item
            for item in contract.get("research_lines", [])
            if isinstance(item, dict) and item.get("line_id") == line_id
        ),
        {},
    )
    return f"""You are the single top-level Topic Coordinator for one bounded SLR task.

TASK ID: {paths.task_id}
ROUND: {round_num}
ATTEMPT: {attempt}
TASK: {description}
WORKSPACE: {workspace}
APPROVED SCOPE: {workspace / "SCOPE.md"}
APPROVED PRIORITIZATION CONTRACT: {workspace / PRIORITIZATION_PATH}
RESEARCH LINE ID: {line_id}
RESEARCH LINE NAME: {line.get("name", "Not available")}
PRIMARY GROUP: {line.get("group", "Not available")}
RANKING MODE: {contract.get("ranking_mode", "qualitative_fallback")}
COMPARISON DIMENSIONS: {
    json.dumps(contract.get("comparison_dimensions", []), ensure_ascii=False)
}
PAPER EVIDENCE ROLES: {json.dumps(contract.get("paper_roles", list(PAPER_ROLES)))}
PRIORITIZATION INPUT WARNINGS: {
    json.dumps(list(prioritization_warnings), ensure_ascii=False)
}
LEGACY-COMPATIBLE TOPIC SYNTHESIS: {paths.synthesis}
SUPPORTING ARTIFACT ROOT: {paths.artifact_root}
PROGRAM-OWNED TASK STATE: {paths.task_state}
PROGRAM-OWNED TASK CONTRACT: {paths.task_contract}
EXACT PAPER NOTE DIRECTORY: {paths.paper_dir}
CANONICAL PAPER NOTE DIRECTORY: {_workspace_relative(workspace, paths.paper_dir)}
EXACT TECHNICAL NOTE DIRECTORY: {paths.technical_dir}
CANONICAL TECHNICAL NOTE DIRECTORY: {
    _workspace_relative(workspace, paths.technical_dir)
}
EXACT METADATA FINDINGS FILE: {paths.metadata_findings}
EXACT CORRECTION REQUEST FILE: {paths.correction_requests}
EXACT TOPIC SYNTHESIS: {paths.synthesis}
CHECKPOINT INVENTORY: {json.dumps(checkpoint, ensure_ascii=False)}

Read the approved scope, the task, existing supporting artifacts, and the
preloaded `slr-topic-research` skill. Preserve valid files from earlier
attempts. Never modify SCOPE.md, SCOPE_ORIGINAL.md, TASKS.md, SUMMARY.md,
SLR_STATE.json, task.json, task_contract.json, another topic, Git state, or
harness configuration.

PATH EXAMPLES:
- Correct paper note: {paths.paper_dir / "2024-smith-example.md"}
- Incorrect paper notes: `2024-smith-example.md`,
  `{paths.artifact_root / "2024-smith-example.md"}`, and
  `{paths.paper_dir / "notes" / "2024-smith-example.md"}`.
- Correct technical note: {paths.technical_dir / "official-project-docs.md"}
- Incorrect technical note: `{paths.artifact_root / "official-project-docs.md"}`.

OUTER/INNER BOUNDARY:
- You are the only top-level Claude CLI process for this topic.
- When CHECKPOINT INVENTORY says their work is missing, use the ordinary Claude
  Code `Agent` tool with only these named subagent types:
  `{config.academic_agent}`, `{config.technical_agent}`, and
  `{config.metadata_agent}`. Do not create an Agent Team, team task list, or
  external Claude process.
- On a fresh attempt, start `{config.academic_agent}` and
  `{config.technical_agent}` concurrently as background subagents. On retry,
  do not relaunch a role whose checkpoint step is already complete. Their write
  trees do not overlap.
- Wait at most {config.message_wait_seconds}s per bounded coordination step.
- After the academic worker creates paper notes or an explicit no-result
  record, invoke `{config.metadata_agent}` only if metadata checking or
  corrections remain incomplete.
- Ordinary subagents cannot directly peer-message in the supported non-team
  mode. The checker writes `audits/correction_requests.jsonl`; you then invoke
  `{config.academic_agent}` again with only the pending corrections, and invoke
  the checker again. Stop after {config.max_correction_rounds} correction
  rounds. Requests are append-only `pending → applied → resolved` (or
  `unresolved`); pending and applied are both open. Never overwrite existing
  JSONL history. Record every handoff in `coordination_log.jsonl`.
- Maximum-turn budgets: coordinator {config.coordinator_max_turns}, academic
  {config.academic_max_turns}, technical {config.technical_max_turns}, metadata
  checker {config.metadata_max_turns}. Never wait or revise indefinitely.

ACADEMIC WORK CONTRACT:
- Every included note follows `{workspace / ".claude/templates/paper-note.md"}`.
- Soft target {config.target_papers} included papers from at most
  {config.max_paper_candidates} initial candidates.
- Bounded backward citation search: {config.enable_backward_citation_search}.
- Bounded forward citation search: {config.enable_forward_citation_search}.
- One stable `<year>-<first-author>-<short-title>.md` note per included paper,
  merging duplicate preprint/conference/journal versions.
- Produce only `papers/*.md`, or a clear `papers/NO_RESULTS.md` record. The
  Harness compiles indexes, IDs, counts, and canonical paths.

TECHNICAL WORK CONTRACT:
- Soft target {config.target_technical_sources} sources from at most
  {config.max_technical_candidates} candidates.
- Keep technical evidence explicitly non-peer-reviewed unless it truly is a
  paper. Produce only `technical_sources/*.md`, or a clear
  `technical_sources/NO_RESULTS.md`. The Harness compiles the index and IDs.

METADATA CHECK CONTRACT:
- Independently check only title, authors, year, venue/publication, DOI, arXiv
  ID, URL, version relationships, duplicates, and note-to-paper identity.
- Do not check methods, experiments, result numbers, conclusions, or analysis.
- Append one finding per paper to `audits/metadata_findings.jsonl`. Valid item
  statuses: PASS, CORRECTED, UNRESOLVED, NOT_CHECKED. The Harness calculates
  the audit summary, counts, and overall status. A first mismatch is UNRESOLVED
  plus a pending request. CORRECTED is valid only after the worker appends
  applied, the checker re-verifies it, appends resolved, and records the exact
  changed frontmatter fields in `corrected_frontmatter`.

REQUIRED FINAL OUTPUTS:
- Existing-compatible synthesis: `{paths.synthesis}`.
- Agent-owned supporting output is limited to notes/no-result records,
  metadata findings/correction requests, optional coordinator observations,
  and the coordination log. The Harness generates canonical indexes, audit,
  counts, normalization report, checkpoint, and manifest after this process.
- The synthesis must distinguish papers from technical sources, link claims to
  supporting note paths, and disclose PARTIAL/UNRESOLVED metadata.
- Add `## Scope-Driven Research-Line Assessment` to the synthesis with the
  Research Line ID/name/group, ranking applicability, locally proposed tier,
  confidence, missing ranking evidence, comparison-dimension values, priority
  factor assessments, and paper evidence roles. `not_applicable` is allowed
  with a reason. This is a local assessment only; never claim a globally final
  rank. Missing values remain unknown rather than zero.
- You may write optional `audits/coordinator_observations.json` containing only
  proposed tier, confidence, factor/comparison observations, missing evidence,
  and `paper_role_records` as a list of paper_id/research_line_id/role/reason
  objects. Malformed or absent observations are non-fatal.
- Do not calculate counts, create canonical indexes/audits/manifest, or fill
  program task IDs. Do not modify any file whose producer is `slrharness`.

MCP AND NETWORK FALLBACK:
- Academic and metadata roles use configured scholarly/arXiv tools first. For
  an arXiv operation returning HTTP 429, make at most two attempts in total,
  then switch to Tavily. Also switch to Tavily when scholarly/arXiv tools are
  absent or fail. Only if Tavily fails, use WebSearch/WebFetch. The metadata
  checker uses Tavily/WebSearch only for discovery or cross-confirmation; an
  ordinary search-result snippet alone is not authoritative metadata. Prefer
  arXiv, DOI/publisher, venue, then author/project official pages. If only a
  snippet is available, record UNRESOLVED or [UNVERIFIED]. Technical work
  prefers configured Tavily, then WebSearch/WebFetch. Missing
  MCPs, rate limits, empty results, inaccessible pages, and missing Tavily keys
  are non-fatal.
- If retrieval is unavailable, write explicit no-result/limited-access records.
  Never fabricate papers, metadata, access depth, sources, or results. Mark
  unverifiable content `[UNVERIFIED]`.

RETRY DIAGNOSTICS FROM THE HARNESS:
{json.dumps(diagnostics, ensure_ascii=False, indent=2)}

Use CHECKPOINT INVENTORY as authoritative resume state. Do not repeat completed
searches. Launch only roles needed for `missing_steps`, collect every background
Agent handle before finishing, and complete only missing work. Do not generate
the global Summary or final review.
"""


def build_stage_prompt(
    workspace: Path,
    paths: TopicPaths,
    role: str,
    staging: Path,
    description: str,
    config: TopicExecutionConfig,
    *,
    issue: dict[str, Any] | None = None,
) -> str:
    """Build a narrow content-only contract for one program-scheduled stage."""
    common = f"""PROGRAM-SCHEDULED TOPIC STAGE
WORKSPACE: {workspace}
TASK: {description}
APPROVED SCOPE CONTRACT: {workspace / 'artifacts/SCOPE_CONTRACT.json'}
RESEARCH-LINE CONTRACT: {workspace / PRIORITIZATION_PATH}
TOPIC SYNTHESIS: {paths.synthesis}
PAPER NOTES: {paths.paper_dir}
TECHNICAL NOTES: {paths.technical_dir}
INVOCATION STAGING: {staging}

The program owns task IDs, attempts, rounds, stages, status, timestamps, counts,
canonical IDs, canonical paths, issues, manifests, checkpoints, and finalization.
Ignore any such values found in model-authored content. Do not edit TASKS.md,
SLR_STATE.json, artifacts/ISSUES.jsonl, scope files, generated registries, or Git.
"""
    if issue is not None:
        return common + f"""
Apply exactly this program-dispatched metadata repair to the exact note:
{json.dumps(issue, ensure_ascii=False, indent=2)}
Modify only the named frontmatter field. Do not change body text or any other
frontmatter field and do not declare the issue resolved.
"""
    if role == config.academic_agent:
        return common + f"""
HARD BUDGET (stop when reached; do not keep searching):
- Include at most {config.target_papers} paper notes in the imported set.
- Inspect at most {config.max_paper_candidates} initial candidates.
- After the include budget is met, do not open new candidates or citation hops.
- Prefer 1-2 well-identified papers over a long candidate list.

Search and read academic papers for this topic. Write only research-content
paper Markdown (or NO_RESULTS.md) below {staging / 'papers'}. Filenames are
temporary; the program imports and names canonical notes. Follow the configured
MCP fallback and citation-chaining policy. Do not write control fields.
"""
    if role == config.technical_agent:
        return common + f"""
HARD BUDGET (stop when reached; do not keep searching):
- Include at most {config.target_technical_sources} technical notes.
- Inspect at most {config.max_technical_candidates} initial candidates.

Search and read technical sources for this topic. Write only research-content
technical Markdown (or NO_RESULTS.md) below {staging / 'technical'}. Filenames
are temporary; the program imports them. Keep non-peer-reviewed evidence clear.
"""
    if role == config.metadata_agent:
        return common + f"""
Independently inspect identity metadata in existing paper notes. Do not edit a
note and do not declare issue status. Write one JSON object per line to
{staging / 'metadata_observations.jsonl'} with note_path, checked_fields, and
remaining_uncertainties. Each checked field contains observed, verified,
source, and boolean match. No comments, arrays, counts, or lifecycle fields.
"""
    if role == config.coordinator_agent:
        return common + f"""
Act only as a topic content synthesizer. Read validated notes and write
{paths.synthesis}. Include strongest support, strongest contradiction, missing
evidence, topic-specific limitations, and the Scope-Driven Research-Line
Assessment. Do not launch agents or create any control/audit file.
"""
    raise ValueError(f"unsupported topic stage role: {role}")


def load_observations(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    observations: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"metadata observation line {number} is not an object")
        observations.append(value)
    return observations


def derive_topic_status(workspace: Path, paths: TopicPaths) -> tuple[str, list[str]]:
    """Derive topic terminal status solely from artifact predicates."""
    diagnostics: list[str] = []
    papers = [
        path
        for path in paths.paper_dir.glob("*.md")
        if path.name.lower() not in GENERATED_NOTE_NAMES
    ] if paths.paper_dir.is_dir() else []
    technical = [
        path
        for path in paths.technical_dir.glob("*.md")
        if path.name.lower() not in GENERATED_NOTE_NAMES
    ] if paths.technical_dir.is_dir() else []
    if not papers and not (paths.paper_dir / "NO_RESULTS.md").is_file():
        diagnostics.append("required paper notes or NO_RESULTS.md are missing")
    if not technical and not (paths.technical_dir / "NO_RESULTS.md").is_file():
        diagnostics.append("required technical notes or NO_RESULTS.md are missing")
    for note in papers:
        try:
            metadata, body = parse_frontmatter(note)
        except OSError as exc:
            diagnostics.append(f"cannot read paper note {note.name}: {exc}")
            continue
        identifiable = bool(metadata.get("title")) and bool(
            metadata.get("authors") or metadata.get("doi") or metadata.get("arxiv_id")
        )
        if not identifiable or not body.strip():
            diagnostics.append(f"paper note schema failed: {note.name}")
    if not paths.synthesis.is_file() or len(
        paths.synthesis.read_text(encoding="utf-8").strip()
    ) < 200:
        diagnostics.append("topic synthesis is missing or too short")
    blockers = blocking_issue_ids(
        workspace,
        target_prefix=paths.paper_dir.resolve().relative_to(workspace.resolve()).as_posix(),
    )
    if blockers:
        diagnostics.append("open topic blockers: " + ", ".join(blockers))
        return "AWAITING_INTERVENTION", diagnostics
    if diagnostics:
        return "FAILED", diagnostics
    warnings = [
        identifier
        for identifier, issue in current_issues(workspace).items()
        if issue.get("severity") != "blocking"
        and str(issue.get("target", "")).startswith(
            paths.paper_dir.resolve().relative_to(workspace.resolve()).as_posix()
        )
        and issue.get("status") != "verified_closed"
    ]
    return ("COMPLETE_WITH_WARNINGS" if warnings else "COMPLETE"), diagnostics


def _workspace_relative(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


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
    synthesis_text = ""
    if not paths.synthesis.is_file():
        errors.append(f"missing topic synthesis: {paths.synthesis}")
    elif not paths.synthesis.resolve().is_relative_to(
        (workspace / "topics").resolve()
    ):
        errors.append("topic synthesis path escapes the topics tree")
    else:
        synthesis_text = paths.synthesis.read_text(encoding="utf-8")
        if len(synthesis_text.strip()) < 200:
            errors.append("topic synthesis is empty or only a placeholder")

    manifest = _load_json(paths.manifest, "coordinator manifest", errors)
    if manifest is None:
        return CoordinatorValidation(False, "FAILED", tuple(errors), tuple(warnings))
    schema_errors, schema_warnings = check_schema_version(manifest, "manifest")
    errors.extend(schema_errors)
    warnings.extend(schema_warnings)
    if manifest.get("task_id") != paths.task_id:
        errors.append(
            f"manifest task_id mismatch: expected {paths.task_id}, "
            f"got {manifest.get('task_id')!r}"
        )
    harness_manifest = manifest.get("producer") == "slrharness"
    if harness_manifest:
        task_contract = _load_json(paths.task_contract, "task contract", errors)
        if task_contract and task_contract.get("task_id") != paths.task_id:
            errors.append("task contract task_id mismatch")
        if not paths.normalization_report.is_file():
            errors.append("missing artifact normalization report")
        if not paths.checkpoint.is_file():
            errors.append("missing checkpoint inventory")
    contract = load_scope_prioritization(workspace)
    known_lines = {
        str(item.get("line_id"))
        for item in contract.get("research_lines", [])
        if isinstance(item, dict) and item.get("line_id")
    }
    prioritization = manifest.get("prioritization")
    if not isinstance(prioritization, dict):
        warnings.append("prioritization: manifest assessment is missing")
    else:
        line_id = str(prioritization.get("research_line_id") or "").upper()
        if not line_id:
            warnings.append("prioritization: research_line_id is missing")
        elif line_id != "NOT_APPLICABLE" and line_id not in known_lines:
            warnings.append(f"prioritization: unknown Research Line ID {line_id}")
        applicability = str(
            prioritization.get("ranking_applicability") or ""
        ).lower().replace(" ", "_")
        if applicability and applicability not in {"applicable", "not_applicable"}:
            warnings.append("prioritization: invalid ranking_applicability")
        tier = str(prioritization.get("proposed_tier") or "").strip()
        if applicability != "not_applicable" and not tier:
            warnings.append("prioritization: proposed tier is missing")
        elif tier and tier.lower() not in {value.lower() for value in PRIORITY_TIERS}:
            warnings.append(f"prioritization: invalid proposed tier {tier!r}")
        roles = prioritization.get("paper_roles") or {}
        if isinstance(roles, dict):
            for paper_id, role in roles.items():
                if str(role).lower() not in PAPER_ROLES:
                    warnings.append(
                        f"prioritization: invalid paper role {role!r} for {paper_id}"
                    )
        elif roles:
            warnings.append("prioritization: paper_roles must be an object")
    if synthesis_text and not re.search(
        r"^##\s+Scope-Driven Research-Line Assessment\s*$",
        synthesis_text,
        re.MULTILINE | re.IGNORECASE,
    ):
        warnings.append("prioritization: synthesis line assessment is missing")
    if contract.get("compile_status") == "PARTIAL":
        warnings.append("prioritization: approved scope contract is PARTIAL")
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
    if correction_queue.is_file():
        latest_requests: dict[str, dict[str, Any]] = {}
        seen_pending: set[tuple[str, str, str]] = set()
        for line_number, line in enumerate(
            correction_queue.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith("//"):
                continue
            try:
                request = json.loads(stripped_line)
            except json.JSONDecodeError:
                errors.append(f"invalid correction request JSON on line {line_number}")
                continue
            # Older agents sometimes serialized an empty queue as `[]`.
            # Accept that one legacy representation without downgrading an
            # otherwise complete task; all new output follows strict JSONL.
            if request == []:
                continue
            if not isinstance(request, dict):
                warnings.append(
                    f"skipping non-object correction request on line {line_number} "
                    f"(got {type(request).__name__})"
                )
                continue
            request_id = str(request.get("request_id") or "")
            request_status = str(request.get("status") or "").lower()
            if request_status == "verified":
                request_status = "resolved"
            if not request_id or request_status not in {
                "pending",
                "applied",
                "resolved",
                "unresolved",
            }:
                errors.append(f"invalid correction request on line {line_number}")
                continue
            field_name = str(request.get("field") or "")
            if field_name and field_name not in CORRECTABLE_FRONTMATTER_FIELDS:
                warnings.append(
                    f"ignored non-frontmatter correction field {field_name!r} "
                    f"on line {line_number}"
                )
                continue
            request = {**request, "status": request_status}
            signature = (
                request_id,
                str(request.get("note_path") or ""),
                str(request.get("field") or ""),
            )
            if request_status in {"pending", "applied"} and signature in seen_pending:
                warnings.append(f"duplicate pending correction request: {request_id}")
            seen_pending.add(signature)
            latest_requests[request_id] = request
        pending_requests = [
            request_id
            for request_id, request in latest_requests.items()
            if str(request.get("status")).lower() in {"pending", "applied"}
        ]
        if pending_requests:
            message = f"unresolved correction requests: {sorted(pending_requests)}"
            if status == "COMPLETE" or not allow_partial:
                errors.append(message)
            else:
                warnings.append(message)
        if isinstance(config, TopicExecutionConfig):
            rounds = [
                int(request.get("round", 0))
                for request in latest_requests.values()
                if str(request.get("round", "")).isdigit()
            ]
            if rounds and max(rounds) > config.max_correction_rounds:
                errors.append("correction request exceeds max_correction_rounds")

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
    if paper_data:
        schema_errors, schema_warnings = check_schema_version(paper_data, "paper index")
        errors.extend(schema_errors)
        warnings.extend(schema_warnings)
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
    if technical_data:
        schema_errors, schema_warnings = check_schema_version(
            technical_data, "technical source index"
        )
        errors.extend(schema_errors)
        warnings.extend(schema_warnings)
    if technical_index and technical_index.suffix.lower() != ".json":
        no_result = paths.technical_dir / "NO_RESULTS.md"
        if technical_index != no_result.resolve():
            errors.append("technical index must be JSON unless it is NO_RESULTS.md")
    audit = _load_json(audit_path, "metadata audit", errors) if audit_path else None
    if audit and audit.get("task_id") != paths.task_id:
        errors.append("metadata audit task_id mismatch")
    if audit:
        schema_errors, schema_warnings = check_schema_version(audit, "metadata audit")
        errors.extend(schema_errors)
        warnings.extend(schema_warnings)

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
                allowed_root = (
                    paths.paper_dir.resolve()
                    if harness_manifest
                    else (workspace / "topics").resolve()
                )
                if (
                    note_path is None
                    or not note_path.is_file()
                    or not note_path.is_relative_to(allowed_root)
                    or note_path.suffix.lower() != ".md"
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
            "checked_count": sum(
                isinstance(item, dict) and item.get("status") != "NOT_CHECKED"
                for item in items
            ),
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
                allowed_root = (
                    paths.technical_dir.resolve()
                    if harness_manifest
                    else (workspace / "topics").resolve()
                )
                if (
                    note_path is None
                    or not note_path.is_file()
                    or not note_path.is_relative_to(allowed_root)
                    or note_path.suffix.lower() != ".md"
                ):
                    errors.append(f"invalid technical note path: {note_value!r}")

    substantive_warnings = [
        warning
        for warning in warnings
        if "schema_version" not in warning
        and not warning.startswith("prioritization:")
    ]
    effective_status = (
        "PARTIAL" if substantive_warnings or status == "PARTIAL" else status
    )
    if substantive_warnings and not allow_partial:
        errors.extend(substantive_warnings)
    accepted = not errors and effective_status in ("COMPLETE", "PARTIAL")
    return CoordinatorValidation(
        accepted,
        effective_status if accepted else "FAILED",
        tuple(errors),
        tuple(warnings),
        manifest,
    )
