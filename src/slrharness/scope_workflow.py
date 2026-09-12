"""Program-owned lifecycle for preliminary scope preparation and approval."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from slrharness.agent_backends import (
    AgentBackend,
    AgentExecutionResult,
    execute_agent,
)
from slrharness.contracts import (
    SCHEMA_VERSION,
    atomic_write_json,
    atomic_write_text,
    check_schema_version,
)
from slrharness.workspace_assets import deploy_claude_assets

STATE_FILENAME = "SLR_STATE.json"
PROPOSAL_FILENAME = "SCOPE_PROPOSAL.md"
SOURCES_FILENAME = "SCOPE_SOURCES.md"
REVISION_DIRNAME = "scope_revisions"

SCOPE_NOT_STARTED = "SCOPE_NOT_STARTED"
SCOPE_PREPARING = "SCOPE_PREPARING"
AWAITING_SCOPE_APPROVAL = "AWAITING_SCOPE_APPROVAL"
SCOPE_APPROVED = "SCOPE_APPROVED"
SCOPE_REJECTED = "SCOPE_REJECTED"
SCOPE_REVISION_REQUESTED = "SCOPE_REVISION_REQUESTED"

SCOPE_TRANSITIONS = {
    SCOPE_NOT_STARTED: {SCOPE_PREPARING},
    SCOPE_PREPARING: {AWAITING_SCOPE_APPROVAL, SCOPE_REVISION_REQUESTED},
    AWAITING_SCOPE_APPROVAL: {
        SCOPE_APPROVED,
        SCOPE_REJECTED,
        SCOPE_REVISION_REQUESTED,
    },
    SCOPE_REJECTED: {SCOPE_REVISION_REQUESTED},
    SCOPE_REVISION_REQUESTED: {SCOPE_PREPARING},
    SCOPE_APPROVED: set(),
}

WORKSPACE_GITIGNORE = """\
# Workspace .gitignore
.kiro/
__pycache__/
*.pyc
.DS_Store
.venv/
"""

EMPTY_TASKS_MD = """\
# Tasks: {theme}

## Round 1

### Pending

(Manager agent will populate this during the first plan pass.)

## Completed

## Backlog
"""

EMPTY_SUMMARY_MD = """\
# Summary: {theme}

(This file will be populated by the manager agent as the review progresses.)

## Comparison Table

## Key Findings

## Open Questions

## References
"""


@dataclass(frozen=True)
class ScopeConfig:
    """Small, backwards-compatible configuration surface for scope research."""

    enabled: bool = True
    require_approval: bool = True
    target_surveys: int = 3
    max_initial_candidates: int = 5
    minimum_successful_surveys: int = 0
    allow_model_knowledge_fallback: bool = True
    optional_search_providers: tuple[str, ...] = (
        "scholarly",
        "arxiv",
        "claude_web",
        "tavily",
    )

    def __post_init__(self) -> None:
        if self.target_surveys < 0 or self.max_initial_candidates < 0:
            raise ValueError("Scope search targets must be non-negative")
        if self.target_surveys > 100 or self.max_initial_candidates > 1000:
            raise ValueError("Scope search targets exceed v1 safety limits")
        if self.minimum_successful_surveys != 0:
            raise ValueError("minimum_successful_surveys must remain 0")


ScopeExecutor = Callable[
    [AgentBackend, str | None, str, Path, int], AgentExecutionResult
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def require_scope_transition(current: object, target: str) -> None:
    if target == current:
        return
    if target not in SCOPE_TRANSITIONS.get(str(current), set()):
        raise ValueError(f"Illegal scope transition: {current!r} -> {target!r}")


def state_path(workspace: Path) -> Path:
    return workspace / STATE_FILENAME


def load_scope_state(workspace: Path) -> dict[str, object]:
    path = state_path(workspace)
    if not path.is_file():
        raise FileNotFoundError(f"Scope state file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid scope state: {path}")
    errors, _ = check_schema_version(data, "project state")
    if errors:
        raise ValueError("; ".join(errors))
    return data


def save_scope_state(workspace: Path, state: dict[str, object]) -> None:
    """Atomically persist harness-owned state."""
    atomic_write_json(state_path(workspace), state)


def _new_state(
    topic: str, config: ScopeConfig, theme: str | None = None
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": SCOPE_NOT_STARTED,
        "theme": theme or topic,
        "initial_topic": topic,
        "proposal_path": PROPOSAL_FILENAME,
        "sources_path": SOURCES_FILENAME,
        "revision": 0,
        "generated_at": None,
        "approved_at": None,
        "approved_revision": None,
        "scope_agent_status": "NOT_STARTED",
        "failure": None,
        "formal_research_allowed": False,
        "config": asdict(config),
        "feedback_history": [],
        "history": [
            {
                "at": utc_now(),
                "event": "scope_project_created",
                "revision": 0,
            }
        ],
    }


def initialize_direct_approved_state(
    workspace: Path, topic: str, config: ScopeConfig | None = None
) -> None:
    """Record an explicitly supplied, already-approved formal scope."""
    state = _new_state(topic, config or ScopeConfig(), topic)
    approved_at = utc_now()
    state.update(
        {
            "status": SCOPE_APPROVED,
            "revision": 0,
            "generated_at": approved_at,
            "approved_at": approved_at,
            "approved_revision": 0,
            "scope_agent_status": "SKIPPED_EXPLICIT_APPROVAL",
            "formal_research_allowed": True,
            "proposal_path": "SCOPE.md",
            "sources_path": None,
        }
    )
    history = list(state["history"])
    history.append(
        {
            "at": approved_at,
            "event": "user_scope_explicitly_approved",
            "revision": 0,
        }
    )
    state["history"] = history
    save_scope_state(workspace, state)


def _run_git(
    workspace: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=check,
        capture_output=True,
        text=True,
    )


def _commit_if_dirty(workspace: Path, message: str) -> None:
    status = _run_git(workspace, "status", "--porcelain").stdout.strip()
    if not status:
        return
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "-m", message)


def initialize_scope_project(
    theme: str,
    topic: str,
    workspace: Path,
    config: ScopeConfig,
    draft_scope: Path | None = None,
) -> None:
    """Create a Git workspace that contains no formal-review artifacts yet."""
    workspace.mkdir(parents=True)
    (workspace / ".gitignore").write_text(WORKSPACE_GITIGNORE, encoding="utf-8")
    (workspace / "INITIAL_TOPIC.md").write_text(
        f"# Initial Research Direction\n\n{topic.strip()}\n", encoding="utf-8"
    )
    if draft_scope is not None:
        shutil.copyfile(draft_scope, workspace / "SCOPE_DRAFT.md")
    deploy_claude_assets(workspace)
    save_scope_state(workspace, _new_state(topic, config, theme))
    _run_git(workspace, "init", "-b", "main")
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "-m", "scope-0: initialize scope project")


def build_scope_prompt(
    workspace: Path,
    state: dict[str, object],
    revision: int,
) -> str:
    """Build the per-invocation contract for the scope specialist."""
    config = state["config"]
    assert isinstance(config, dict)
    feedback = state.get("feedback_history", [])
    draft_note = (
        "Read SCOPE_DRAFT.md as a user-provided draft to improve."
        if (workspace / "SCOPE_DRAFT.md").is_file()
        else "No complete draft was supplied."
    )
    return textwrap.dedent(f"""\
        Prepare revision {revision} of a literature-review scope proposal.

        WORKSPACE: {workspace}
        USER'S ORIGINAL RESEARCH DIRECTION:
        {state["initial_topic"]}

        REQUIRED METHOD:
        - The `slr-scoping` skill is preloaded; apply it explicitly.
        - {draft_note}
        - Read the previous {PROPOSAL_FILENAME} and {SOURCES_FILENAME} when they
          exist, and incorporate the latest user feedback below.
        - Aim for about {config["target_surveys"]} useful surveys/reviews and
          normally inspect no more than {config["max_initial_candidates"]}
          initial candidates. Both are soft targets, never completion gates.
        - Search fallback order: configured scholarly tools, configured arXiv
          tools, built-in WebSearch/WebFetch, then optional Tavily tools.
        - Any missing, empty, failed, or rate-limited provider is non-fatal.
          Zero successfully read surveys is allowed. If necessary, use limited
          model knowledge and mark every such claim `[UNVERIFIED]`.
        - No search result never means that no related literature exists.

        USER FEEDBACK HISTORY:
        {json.dumps(feedback, ensure_ascii=False, indent=2)}

        WRITE EXACTLY THESE TWO CONTENT OUTPUTS:
        1. {workspace / PROPOSAL_FILENAME}
        2. {workspace / SOURCES_FILENAME}

        {PROPOSAL_FILENAME} must be a substantive, reviewable document covering:
        1. Initial Research Direction (original input, interpretation, ambiguities)
        2. Research Questions (one primary question and concrete subquestions)
        3. Terminology and Synonyms (including confusable distinctions)
        4. Proposed Scope and Boundaries (In Scope, Out of Scope, Borderline Cases)
        5. Proposed Literature Organization (categories and relationships)
        6. Inclusion Criteria
        7. Exclusion Criteria
        8. Preliminary Search Strategy (queries, sources, citation chasing,
           deduplication and version-merging rules)
        9. Proposed Evidence and Comparison Dimensions
        10. Expected Deliverables
        11. Known Limitations and Uncertainties
        12. Approval Checklist

        {SOURCES_FILENAME} must remain useful even when no source was accessible.
        For every used or attempted source record title, author/institution, year,
        venue when known, URL/DOI/arXiv ID, resource type, discovery provider,
        access status, content actually read, influence on the proposal, adoption
        decision, rejection/failure reason, and verification status. Explicitly
        record provider and access failures. Never invent source metadata.

        PROTECTED HARNESS STATE:
        - Do not modify {STATE_FILENAME}, lifecycle state, revision, approval,
          budgets, configuration, Git files, or any existing revision archive.
        - Do not create SCOPE.md, SCOPE_ORIGINAL.md, TASKS.md, SUMMARY.md,
          topics/, assets/, paper notes, topic tasks, or a final review.
        - Scope preparation ends after writing the two requested files. It does
          not begin the manager/worker literature-review pipeline.

        SELF-CHECK BEFORE EXITING:
        - Both files exist, are non-empty, and contain substantive content.
        - All twelve proposal areas are covered.
        - Unverified/model-knowledge claims are marked `[UNVERIFIED]`.
        - Retrieval limitations and failed attempts are disclosed.
        - No formal-review artifact or protected-state edit was made.
    """)


def _validate_scope_outputs(workspace: Path) -> list[str]:
    errors: list[str] = []
    proposal = workspace / PROPOSAL_FILENAME
    sources = workspace / SOURCES_FILENAME
    if not proposal.is_file():
        errors.append(f"missing {PROPOSAL_FILENAME}")
    else:
        text = proposal.read_text(encoding="utf-8").strip()
        if len(text) < 800:
            errors.append(f"{PROPOSAL_FILENAME} is too short to be substantive")
        required = (
            "initial research direction",
            "research question",
            "terminology",
            "scope and boundaries",
            "literature organization",
            "inclusion criteria",
            "exclusion criteria",
            "search strategy",
            "comparison dimensions",
            "expected deliverables",
            "limitations",
            "approval checklist",
        )
        lowered = text.lower()
        missing = [section for section in required if section not in lowered]
        if missing:
            errors.append("proposal missing sections: " + ", ".join(missing))

    if not sources.is_file():
        errors.append(f"missing {SOURCES_FILENAME}")
    elif len(sources.read_text(encoding="utf-8").strip()) < 80:
        errors.append(f"{SOURCES_FILENAME} is empty or only a placeholder")

    forbidden_files = (
        "SCOPE.md",
        "SCOPE_ORIGINAL.md",
        "TASKS.md",
        "SUMMARY.md",
    )
    for filename in forbidden_files:
        if (workspace / filename).exists():
            errors.append(f"scope agent created forbidden artifact: {filename}")
    for dirname in ("topics", "assets"):
        if (workspace / dirname).exists():
            errors.append(f"scope agent created forbidden directory: {dirname}/")
    return errors


def _archive_revision(workspace: Path, revision: int) -> None:
    revision_dir = workspace / REVISION_DIRNAME / f"revision-{revision}"
    revision_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(workspace / PROPOSAL_FILENAME, revision_dir / PROPOSAL_FILENAME)
    shutil.copyfile(workspace / SOURCES_FILENAME, revision_dir / SOURCES_FILENAME)


def _quarantine_forbidden_artifacts(workspace: Path, revision: int) -> None:
    """Move scope-agent formal-review artifacts aside without losing evidence."""
    forbidden = [
        workspace / "SCOPE.md",
        workspace / "SCOPE_ORIGINAL.md",
        workspace / "TASKS.md",
        workspace / "SUMMARY.md",
        workspace / "topics",
        workspace / "assets",
    ]
    present = [path for path in forbidden if path.exists()]
    if not present:
        return
    revision_dir = workspace / REVISION_DIRNAME / f"revision-{revision}"
    attempt = 1
    while (revision_dir / f"rejected-artifacts-{attempt}").exists():
        attempt += 1
    destination = revision_dir / f"rejected-artifacts-{attempt}"
    destination.mkdir(parents=True)
    for path in present:
        shutil.move(str(path), destination / path.name)


def run_scope_preparation(
    workspace: Path,
    backend: AgentBackend,
    timeout: int,
    executor: ScopeExecutor = execute_agent,
) -> bool:
    """Run or retry the scope agent and persist a deterministic transition."""
    state = load_scope_state(workspace)
    status = state.get("status")
    if status == SCOPE_APPROVED:
        return True
    if status == AWAITING_SCOPE_APPROVAL:
        raise ValueError("Scope already awaits approval; approve or revise it")
    if status == SCOPE_REJECTED:
        raise ValueError("Scope was rejected; request a revision before retrying")

    if status == SCOPE_PREPARING:
        revision = int(state["revision"])
    elif status in (SCOPE_NOT_STARTED, SCOPE_REVISION_REQUESTED):
        revision = int(state["revision"]) + 1
    else:
        raise ValueError(f"Cannot prepare scope from status {status!r}")

    expected = deepcopy(state)
    require_scope_transition(status, SCOPE_PREPARING)
    expected.update(
        {
            "status": SCOPE_PREPARING,
            "revision": revision,
            "scope_agent_status": "RUNNING",
            "failure": None,
            "formal_research_allowed": False,
        }
    )
    history = list(expected.get("history", []))
    history.append(
        {"at": utc_now(), "event": "scope_preparation_started", "revision": revision}
    )
    expected["history"] = history
    deploy_claude_assets(workspace)
    save_scope_state(workspace, expected)
    _commit_if_dirty(workspace, f"scope-{revision}: start preparation")

    prompt = build_scope_prompt(workspace, expected, revision)
    result = executor(backend, "slr-scoper", prompt, workspace, timeout)

    # The model is not trusted to update harness state. Restore the complete
    # program-owned snapshot before applying the next deterministic transition.
    save_scope_state(workspace, expected)
    if result.timed_out or result.exit_code != 0:
        reason = (
            f"scope agent timed out after {timeout}s"
            if result.timed_out
            else f"scope agent exited with code {result.exit_code}"
        )
        if result.stderr.strip():
            reason += f": {result.stderr.strip()[-1000:]}"
        expected.update({"scope_agent_status": "FAILED", "failure": reason})
        history = list(expected["history"])
        history.append(
            {
                "at": utc_now(),
                "event": "scope_preparation_failed",
                "revision": revision,
                "reason": reason,
            }
        )
        expected["history"] = history
        save_scope_state(workspace, expected)
        _commit_if_dirty(workspace, f"scope-{revision}: record preparation failure")
        return False

    errors = _validate_scope_outputs(workspace)
    if errors:
        reason = "; ".join(errors)
        _quarantine_forbidden_artifacts(workspace, revision)
        expected.update({"scope_agent_status": "FAILED", "failure": reason})
        history = list(expected["history"])
        history.append(
            {
                "at": utc_now(),
                "event": "scope_output_validation_failed",
                "revision": revision,
                "reason": reason,
            }
        )
        expected["history"] = history
        save_scope_state(workspace, expected)
        _commit_if_dirty(workspace, f"scope-{revision}: record invalid output")
        return False

    _archive_revision(workspace, revision)
    generated_at = utc_now()
    require_scope_transition(SCOPE_PREPARING, AWAITING_SCOPE_APPROVAL)
    expected.update(
        {
            "status": AWAITING_SCOPE_APPROVAL,
            "scope_agent_status": "COMPLETED",
            "generated_at": generated_at,
            "failure": None,
            "formal_research_allowed": False,
        }
    )
    history = list(expected["history"])
    history.append(
        {"at": generated_at, "event": "scope_proposal_generated", "revision": revision}
    )
    expected["history"] = history
    save_scope_state(workspace, expected)
    _commit_if_dirty(workspace, f"scope-{revision}: generate proposal")
    return True


def request_scope_revision(workspace: Path, feedback: str) -> int:
    """Persist user feedback and open a new auditable proposal revision."""
    state = load_scope_state(workspace)
    if state.get("status") == SCOPE_APPROVED:
        raise ValueError("Approved scopes cannot be revised in this lifecycle")
    if state.get("status") not in (
        AWAITING_SCOPE_APPROVAL,
        SCOPE_REJECTED,
        SCOPE_PREPARING,
    ):
        raise ValueError(f"Cannot revise scope from status {state.get('status')!r}")
    feedback = feedback.strip()
    if not feedback:
        raise ValueError("Revision feedback must not be empty")
    require_scope_transition(state.get("status"), SCOPE_REVISION_REQUESTED)
    next_revision = int(state["revision"]) + 1
    feedback_history = list(state.get("feedback_history", []))
    feedback_history.append(
        {"at": utc_now(), "for_revision": next_revision, "feedback": feedback}
    )
    history = list(state.get("history", []))
    history.append(
        {
            "at": utc_now(),
            "event": "scope_revision_requested",
            "revision": next_revision,
        }
    )
    state.update(
        {
            "status": SCOPE_REVISION_REQUESTED,
            "scope_agent_status": "NOT_STARTED",
            "failure": None,
            "feedback_history": feedback_history,
            "history": history,
            "formal_research_allowed": False,
        }
    )
    save_scope_state(workspace, state)
    _commit_if_dirty(workspace, f"scope-{next_revision}: record revision request")
    return next_revision


def approve_scope(workspace: Path, revision: int) -> bool:
    """Approve exactly the current proposal and materialize the formal workspace."""
    state = load_scope_state(workspace)
    current = int(state["revision"])
    if state.get("status") == SCOPE_APPROVED:
        if state.get("approved_revision") == revision == current:
            return False
        raise ValueError("A different scope revision is already approved")
    if state.get("status") != AWAITING_SCOPE_APPROVAL:
        raise ValueError("Scope is not awaiting approval")
    if revision != current:
        raise ValueError(
            f"Revision mismatch: current proposal is {current}, requested {revision}"
        )
    require_scope_transition(state.get("status"), SCOPE_APPROVED)
    errors = _validate_scope_outputs(workspace)
    if errors:
        raise ValueError("Cannot approve invalid proposal: " + "; ".join(errors))

    proposal = (workspace / PROPOSAL_FILENAME).read_text(encoding="utf-8")
    atomic_write_text(workspace / "SCOPE.md", proposal)
    atomic_write_text(workspace / "SCOPE_ORIGINAL.md", proposal)
    theme = str(state.get("theme", state["initial_topic"]))
    atomic_write_text(workspace / "TASKS.md", EMPTY_TASKS_MD.format(theme=theme))
    atomic_write_text(workspace / "SUMMARY.md", EMPTY_SUMMARY_MD.format(theme=theme))
    (workspace / "topics").mkdir()
    (workspace / "assets").mkdir()
    (workspace / "topics" / ".gitkeep").touch()
    (workspace / "assets" / ".gitkeep").touch()

    approved_at = utc_now()
    history = list(state.get("history", []))
    history.append({"at": approved_at, "event": "scope_approved", "revision": revision})
    state.update(
        {
            "status": SCOPE_APPROVED,
            "approved_at": approved_at,
            "approved_revision": revision,
            "formal_research_allowed": True,
            "history": history,
        }
    )
    save_scope_state(workspace, state)
    _commit_if_dirty(workspace, f"round-0: approve scope revision {revision}")
    _run_git(workspace, "tag", "-f", "round-0")
    return True


def reject_scope(workspace: Path, revision: int) -> bool:
    """Reject exactly the current proposal without enabling formal research."""
    state = load_scope_state(workspace)
    current = int(state["revision"])
    if state.get("status") == SCOPE_REJECTED and revision == current:
        return False
    if state.get("status") != AWAITING_SCOPE_APPROVAL:
        raise ValueError("Scope is not awaiting approval")
    if revision != current:
        raise ValueError(
            f"Revision mismatch: current proposal is {current}, requested {revision}"
        )
    require_scope_transition(state.get("status"), SCOPE_REJECTED)
    history = list(state.get("history", []))
    history.append({"at": utc_now(), "event": "scope_rejected", "revision": revision})
    state.update(
        {
            "status": SCOPE_REJECTED,
            "formal_research_allowed": False,
            "history": history,
        }
    )
    save_scope_state(workspace, state)
    _commit_if_dirty(workspace, f"scope-{revision}: reject proposal")
    return True


def formal_research_block_reason(workspace: Path) -> str | None:
    """Return why formal research is blocked, preserving legacy workspaces."""
    path = state_path(workspace)
    if not path.is_file():
        return None
    state = load_scope_state(workspace)
    if (
        state.get("status") == SCOPE_APPROVED
        and state.get("formal_research_allowed") is True
    ):
        return None
    return (
        f"Scope status is {state.get('status')}; explicit approval of the current "
        "proposal revision is required before manager/worker research can run."
    )
