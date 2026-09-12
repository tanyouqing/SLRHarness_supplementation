"""Scope preparation lifecycle tests. No real agent or network is used."""

from __future__ import annotations

from pathlib import Path

import pytest

from slrharness.agent_backends import AgentExecutionResult
from slrharness.orchestrator import run_slr
from slrharness.scope import init_workspace
from slrharness.scope_workflow import (
    AWAITING_SCOPE_APPROVAL,
    SCOPE_APPROVED,
    SCOPE_PREPARING,
    SCOPE_REJECTED,
    ScopeConfig,
    approve_scope,
    formal_research_block_reason,
    initialize_scope_project,
    load_scope_state,
    reject_scope,
    request_scope_revision,
    run_scope_preparation,
    save_scope_state,
)


class RecordingBackend:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, Path]] = []

    def check_available(self) -> None:
        return None

    def build_command(
        self, agent_name: str | None, prompt: str, cwd: Path
    ) -> list[str]:
        self.calls.append((agent_name, prompt, cwd))
        return ["fake-agent", prompt]


def _proposal(topic: str = "Agent memory") -> str:
    padding = "This boundary is justified and remains open to user correction. " * 8
    return f"""# Scope Proposal: {topic}

## 1. Initial Research Direction
The initial direction is {topic}. {padding}

## 2. Research Questions
Primary question and three concrete subquestions.

## 3. Terminology and Synonyms
Canonical terms, synonyms, acronyms, and confusable concepts.

## 4. Proposed Scope and Boundaries
### In Scope
Persistent mechanisms.
### Out of Scope
Unrelated mechanisms.
### Borderline Cases
Hybrid systems require manual judgment.

## 5. Proposed Literature Organization
Architectures, storage, retrieval, update, and utilization.

## 6. Inclusion Criteria
Objective relevance, evidence, date, language, and accessibility rules.

## 7. Exclusion Criteria
Incidental mentions and unrelated systems.

## 8. Preliminary Search Strategy
Boolean synonyms, multiple sources, citation chasing, deduplication, and
version merging.

## 9. Proposed Evidence and Comparison Dimensions
Motivation, architecture, datasets, models, baselines, metrics, results,
limitations, and reproducibility.

## 10. Expected Deliverables
Per-paper notes, topic synthesis, global synthesis, and final review.

## 11. Known Limitations and Uncertainties
[UNVERIFIED] Coverage may be incomplete when retrieval providers are unavailable.

## 12. Approval Checklist
- [ ] Confirm boundaries.
- [ ] Confirm comparison dimensions.
"""


def _sources(zero: bool = False) -> str:
    if zero:
        return """# Scope Sources

No survey was successfully retrieved. Web and MCP access were unavailable.
The proposal therefore marks model-knowledge content `[UNVERIFIED]` and does
not interpret the failed search as evidence that no literature exists.
"""
    return """# Scope Sources

## Example Survey
- Authors: Example Institution
- Year: 2025
- Venue: metadata not verified
- Identifier: unavailable
- Type: survey
- Discovered via: scholarly
- Access: abstract only
- Read: abstract and taxonomy summary
- Influence: suggested the architecture categories
- Adopted: yes
- Failure reason: full text unavailable
- Verification: [UNVERIFIED]
"""


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "SLRHarness Tests")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tests@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "SLRHarness Tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "tests@example.invalid")


def _project(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    initialize_scope_project("Agent Memory", "Agent memory", workspace, ScopeConfig())
    return workspace


def _executor(
    backend: RecordingBackend,
    agent_name: str | None,
    prompt: str,
    cwd: Path,
    timeout: int,
) -> AgentExecutionResult:
    backend.build_command(agent_name, prompt, cwd)
    (cwd / "SCOPE_PROPOSAL.md").write_text(_proposal(), encoding="utf-8")
    (cwd / "SCOPE_SOURCES.md").write_text(_sources(), encoding="utf-8")
    return AgentExecutionResult(0, "DONE\n", "")


def test_initial_topic_runs_scoper_and_waits_for_approval(tmp_path: Path) -> None:
    workspace = _project(tmp_path)
    backend = RecordingBackend()
    assert run_scope_preparation(workspace, backend, 30, _executor)
    state = load_scope_state(workspace)
    assert state["status"] == AWAITING_SCOPE_APPROVAL
    assert state["revision"] == 1
    assert state["formal_research_allowed"] is False
    assert backend.calls[0][0] == "slr-scoper"
    assert (workspace / "SCOPE_PROPOSAL.md").is_file()
    assert (workspace / "SCOPE_SOURCES.md").is_file()
    assert not (workspace / "TASKS.md").exists()
    assert formal_research_block_reason(workspace)


def test_approve_materializes_formal_scope_and_is_idempotent(tmp_path: Path) -> None:
    workspace = _project(tmp_path)
    run_scope_preparation(workspace, RecordingBackend(), 30, _executor)
    assert approve_scope(workspace, 1)
    state = load_scope_state(workspace)
    assert state["status"] == SCOPE_APPROVED
    assert state["approved_revision"] == 1
    assert state["formal_research_allowed"] is True
    assert (workspace / "SCOPE.md").read_text(encoding="utf-8") == (
        workspace / "SCOPE_PROPOSAL.md"
    ).read_text(encoding="utf-8")
    assert (workspace / "TASKS.md").is_file()
    assert approve_scope(workspace, 1) is False


def test_wrong_revision_cannot_be_approved(tmp_path: Path) -> None:
    workspace = _project(tmp_path)
    run_scope_preparation(workspace, RecordingBackend(), 30, _executor)
    with pytest.raises(ValueError, match="Revision mismatch"):
        approve_scope(workspace, 2)


def test_revision_preserves_history_and_waits_again(tmp_path: Path) -> None:
    workspace = _project(tmp_path)
    backend = RecordingBackend()
    run_scope_preparation(workspace, backend, 30, _executor)
    request_scope_revision(workspace, "Exclude ordinary transient RAG.")
    assert run_scope_preparation(workspace, backend, 30, _executor)
    state = load_scope_state(workspace)
    assert state["status"] == AWAITING_SCOPE_APPROVAL
    assert state["revision"] == 2
    assert state["feedback_history"][-1]["feedback"] == (
        "Exclude ordinary transient RAG."
    )
    assert (workspace / "scope_revisions/revision-1/SCOPE_PROPOSAL.md").is_file()
    assert (workspace / "scope_revisions/revision-2/SCOPE_PROPOSAL.md").is_file()


def test_reject_blocks_formal_research(tmp_path: Path) -> None:
    workspace = _project(tmp_path)
    run_scope_preparation(workspace, RecordingBackend(), 30, _executor)
    assert reject_scope(workspace, 1)
    assert load_scope_state(workspace)["status"] == SCOPE_REJECTED
    assert formal_research_block_reason(workspace)
    assert reject_scope(workspace, 1) is False


def test_nonzero_agent_exit_is_retryable(tmp_path: Path) -> None:
    workspace = _project(tmp_path)

    def fail(*args: object) -> AgentExecutionResult:
        return AgentExecutionResult(7, "", "provider failed")

    assert not run_scope_preparation(workspace, RecordingBackend(), 30, fail)
    state = load_scope_state(workspace)
    assert state["status"] == SCOPE_PREPARING
    assert state["scope_agent_status"] == "FAILED"
    assert "code 7" in state["failure"]
    assert run_scope_preparation(workspace, RecordingBackend(), 30, _executor)
    assert load_scope_state(workspace)["revision"] == 1


@pytest.mark.parametrize("missing", ["proposal", "sources"])
def test_missing_required_output_fails_validation(tmp_path: Path, missing: str) -> None:
    workspace = _project(tmp_path)

    def incomplete(
        backend: RecordingBackend,
        agent_name: str | None,
        prompt: str,
        cwd: Path,
        timeout: int,
    ) -> AgentExecutionResult:
        if missing != "proposal":
            (cwd / "SCOPE_PROPOSAL.md").write_text(_proposal(), encoding="utf-8")
        if missing != "sources":
            (cwd / "SCOPE_SOURCES.md").write_text(_sources(), encoding="utf-8")
        return AgentExecutionResult(0, "", "")

    assert not run_scope_preparation(workspace, RecordingBackend(), 30, incomplete)
    state = load_scope_state(workspace)
    assert state["status"] == SCOPE_PREPARING
    assert missing.upper() in state["failure"].upper()


def test_zero_successful_surveys_is_not_a_gate(tmp_path: Path) -> None:
    workspace = _project(tmp_path)

    def zero_sources(
        backend: RecordingBackend,
        agent_name: str | None,
        prompt: str,
        cwd: Path,
        timeout: int,
    ) -> AgentExecutionResult:
        (cwd / "SCOPE_PROPOSAL.md").write_text(_proposal(), encoding="utf-8")
        (cwd / "SCOPE_SOURCES.md").write_text(_sources(zero=True), encoding="utf-8")
        return AgentExecutionResult(0, "", "")

    assert run_scope_preparation(workspace, RecordingBackend(), 30, zero_sources)


def test_agent_cannot_persist_protected_state_changes(tmp_path: Path) -> None:
    workspace = _project(tmp_path)

    def tamper(
        backend: RecordingBackend,
        agent_name: str | None,
        prompt: str,
        cwd: Path,
        timeout: int,
    ) -> AgentExecutionResult:
        state = load_scope_state(cwd)
        state["status"] = SCOPE_APPROVED
        state["revision"] = 999
        state["formal_research_allowed"] = True
        save_scope_state(cwd, state)
        (cwd / "SCOPE_PROPOSAL.md").write_text(_proposal(), encoding="utf-8")
        (cwd / "SCOPE_SOURCES.md").write_text(_sources(), encoding="utf-8")
        return AgentExecutionResult(0, "", "")

    assert run_scope_preparation(workspace, RecordingBackend(), 30, tamper)
    state = load_scope_state(workspace)
    assert state["status"] == AWAITING_SCOPE_APPROVAL
    assert state["revision"] == 1
    assert state["formal_research_allowed"] is False


def test_approved_project_does_not_run_scoper_again(tmp_path: Path) -> None:
    workspace = _project(tmp_path)
    backend = RecordingBackend()
    run_scope_preparation(workspace, backend, 30, _executor)
    approve_scope(workspace, 1)
    call_count = len(backend.calls)
    assert run_scope_preparation(workspace, backend, 30, _executor)
    assert len(backend.calls) == call_count


def test_timeout_is_persisted_and_retryable(tmp_path: Path) -> None:
    workspace = _project(tmp_path)

    def timeout(*args: object) -> AgentExecutionResult:
        return AgentExecutionResult(None, "partial", "", timed_out=True)

    assert not run_scope_preparation(workspace, RecordingBackend(), 17, timeout)
    state = load_scope_state(workspace)
    assert state["status"] == SCOPE_PREPARING
    assert "timed out after 17s" in state["failure"]


def test_formal_artifacts_created_by_scoper_are_quarantined(tmp_path: Path) -> None:
    workspace = _project(tmp_path)

    def overreach(
        backend: RecordingBackend,
        agent_name: str | None,
        prompt: str,
        cwd: Path,
        timeout: int,
    ) -> AgentExecutionResult:
        (cwd / "SCOPE_PROPOSAL.md").write_text(_proposal(), encoding="utf-8")
        (cwd / "SCOPE_SOURCES.md").write_text(_sources(), encoding="utf-8")
        (cwd / "TASKS.md").write_text("# forbidden\n", encoding="utf-8")
        (cwd / "topics").mkdir()
        return AgentExecutionResult(0, "", "")

    assert not run_scope_preparation(workspace, RecordingBackend(), 30, overreach)
    assert not (workspace / "TASKS.md").exists()
    assert not (workspace / "topics").exists()
    quarantine = workspace / "scope_revisions/revision-1/rejected-artifacts-1"
    assert (quarantine / "TASKS.md").is_file()
    assert (quarantine / "topics").is_dir()


def test_workspace_contains_discoverable_agents_and_skill(tmp_path: Path) -> None:
    workspace = _project(tmp_path)
    assert (workspace / ".claude/agents/slr-scoper.md").is_file()
    assert (workspace / ".claude/agents/slr-manager.md").is_file()
    assert (workspace / ".claude/agents/slr-worker.md").is_file()
    assert (workspace / ".claude/agents/topic-coordinator.md").is_file()
    assert (workspace / ".claude/agents/academic-paper-worker.md").is_file()
    assert (workspace / ".claude/agents/academic-metadata-checker.md").is_file()
    assert (workspace / ".claude/agents/technical-source-worker.md").is_file()
    assert (workspace / ".claude/skills/slr-scoping/SKILL.md").is_file()
    assert (workspace / ".claude/skills/slr-topic-research/SKILL.md").is_file()
    assert (workspace / ".claude/templates/paper-note.md").is_file()
    assert (workspace / ".claude/templates/final-report.md").is_file()


def test_unapproved_workspace_never_calls_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _project(tmp_path)
    called = False

    def manager(*args: object, **kwargs: object) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr("slrharness.orchestrator.run_manager", manager)
    with pytest.raises(SystemExit, match="Formal research is blocked"):
        run_slr(workspace, 1, 1, 1, 1, RecordingBackend())
    assert not called


def test_approved_workspace_enters_existing_manager_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _project(tmp_path)
    run_scope_preparation(workspace, RecordingBackend(), 30, _executor)
    approve_scope(workspace, 1)
    called = False

    def manager(*args: object, **kwargs: object) -> bool:
        nonlocal called
        called = True
        return False

    monkeypatch.setattr("slrharness.orchestrator.run_manager", manager)
    run_slr(workspace, 1, 1, 1, 1, RecordingBackend())
    assert called


def test_explicit_approved_scope_preserves_legacy_initialization(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scope.md"
    source.write_text("# Approved Scope\n", encoding="utf-8")
    workspace = init_workspace("Approved", source, tmp_path / "workspaces")
    state = load_scope_state(workspace)
    assert state["status"] == SCOPE_APPROVED
    assert state["scope_agent_status"] == "SKIPPED_EXPLICIT_APPROVAL"
    assert formal_research_block_reason(workspace) is None
