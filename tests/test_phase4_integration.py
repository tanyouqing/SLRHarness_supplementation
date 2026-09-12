from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from slrharness.agent_backends import AgentExecutionResult
from slrharness.config import load_config
from slrharness.contracts import SCHEMA_VERSION, check_schema_version, workspace_path
from slrharness.diagnostics import validate_project
from slrharness.finalization import FinalizationConfig
from slrharness.orchestrator import run_finalization_pipeline, run_slr
from slrharness.project_lock import ProjectLock, ProjectLockedError
from slrharness.scope_workflow import (
    ScopeConfig,
    approve_scope,
    initialize_scope_project,
    run_scope_preparation,
)
from slrharness.topic_execution import TopicExecutionConfig
from slrharness.workspace_assets import deploy_claude_assets, required_asset_sources
from tests.test_finalization import FakeBackend, _valid_report, _workspace


def test_schema_contract_accepts_legacy_and_rejects_unknown() -> None:
    errors, warnings = check_schema_version({}, "legacy")
    assert not errors and warnings
    assert check_schema_version({"schema_version": 1}, "old") == ([], [])
    errors, _ = check_schema_version({"schema_version": "2.0"}, "future")
    assert errors


def test_workspace_path_and_project_lock_are_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    (workspace / ".git").mkdir(parents=True)
    assert workspace_path(workspace, "topics/a.md").is_relative_to(workspace)
    with pytest.raises(ValueError):
        workspace_path(workspace, "../escape")
    with (
        ProjectLock(workspace),
        pytest.raises(ProjectLockedError),
        ProjectLock(workspace),
    ):
        pass
    assert not (workspace / ".git/.slrharness.lock").exists()


def test_required_packaged_runtime_assets_exist() -> None:
    paths = required_asset_sources()
    assert paths
    assert all(path.is_file() for path in paths)
    assert any(path.name == "paper-note.md" for path in paths)
    assert any(path.name == "final-report.md" for path in paths)


def test_example_config_loads_defaults_and_rejects_unknown(tmp_path: Path) -> None:
    example = Path("plugins/config/example-config.json")
    config = load_config(example)
    assert config.topic_execution.mode == "topic_coordinator"
    assert config.finalization.max_prefinal_repair_rounds == 1
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version":"1.0","surprise":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown configuration"):
        load_config(invalid)


def test_read_only_validation_warns_for_legacy_and_rejects_future_schema(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "legacy"
    (workspace / ".git").mkdir(parents=True)
    (workspace / "topics").mkdir()
    state_path = workspace / "SLR_STATE.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "SCOPE_APPROVED",
                "formal_research_allowed": True,
                "revision": 0,
            }
        ),
        encoding="utf-8",
    )
    (workspace / "SCOPE.md").write_text("# Scope", encoding="utf-8")
    (workspace / "TASKS.md").write_text("# Tasks", encoding="utf-8")
    before = state_path.read_bytes()
    report = validate_project(workspace)
    assert report.ok
    assert any("approved_revision" in item for item in report.warnings)
    assert state_path.read_bytes() == before

    state_path.write_text(
        json.dumps({"schema_version": "9.0", "status": "SCOPE_APPROVED"}),
        encoding="utf-8",
    )
    assert not validate_project(workspace).ok


def test_offline_scope_to_complete_without_gap(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "review"
    initialize_scope_project("Memory", "Agent memory", workspace, ScopeConfig())

    def fake_scope(backend, agent_name, prompt, cwd, timeout):
        sections = [
            "Initial Research Direction",
            "Research Question",
            "Terminology",
            "Scope and Boundaries",
            "Literature Organization",
            "Inclusion Criteria",
            "Exclusion Criteria",
            "Search Strategy",
            "Comparison Dimensions",
            "Expected Deliverables",
            "Limitations",
            "Approval Checklist",
        ]
        (cwd / "SCOPE_PROPOSAL.md").write_text(
            "# Scope Proposal\n"
            + "\n".join(
                f"## {heading}\n" + "bounded evidence scope " * 12
                for heading in sections
            ),
            encoding="utf-8",
        )
        (cwd / "SCOPE_SOURCES.md").write_text(
            "# Sources\nNo network was used; fixture evidence only. " * 20,
            encoding="utf-8",
        )
        return AgentExecutionResult(0, "DONE", "")

    assert run_scope_preparation(workspace, FakeBackend(), 30, executor=fake_scope)
    assert approve_scope(workspace, 1)

    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    fixture = _workspace(fixture_root, ("topics/a/one", "topics/b/two"))
    second_note = fixture / "topics/b/two/papers/paper-2.md"
    second_note.write_text(
        second_note.read_text(encoding="utf-8").replace("LOCAL-2", "LOCAL-1"),
        encoding="utf-8",
    )
    second_index = fixture / "topics/b/two/papers/index.json"
    second_data = json.loads(second_index.read_text(encoding="utf-8"))
    second_data["papers"][0]["paper_id"] = "LOCAL-1"
    second_index.write_text(json.dumps(second_data), encoding="utf-8")
    for name in ("topics",):
        source = fixture / name
        destination = workspace / name
        if destination.exists():
            for child in source.iterdir():
                child.rename(destination / child.name)
    (workspace / "TASKS.md").write_text(
        "# Tasks\n- [x] topics/a/one -- first\n- [x] topics/b/two -- second\n",
        encoding="utf-8",
    )

    calls: list[str] = []

    def fake_agent(workspace, agent_name, prompt, run_name, timeout, backend):
        calls.append(run_name)
        if "finalize" in run_name:
            (workspace / "SUMMARY.md").write_text(
                _valid_report(workspace), encoding="utf-8"
            )
        return True

    monkeypatch.setattr("slrharness.orchestrator.run_named_agent", fake_agent)
    monkeypatch.setattr(
        "slrharness.orchestrator._commit_finalization_artifacts", lambda *args: None
    )
    config = replace(FinalizationConfig(), enable_prefinal_audit=False)
    assert run_finalization_pipeline(
        workspace, 1, 2, 30, FakeBackend(), TopicExecutionConfig(), config
    )
    state = json.loads((workspace / "SLR_STATE.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["finalization"]["phase"] == "COMPLETE"
    registry = json.loads(
        (workspace / "artifacts/SOURCE_REGISTRY.json").read_text(encoding="utf-8")
    )
    assert len(registry["papers"]) == 1
    assert len(registry["papers"][0]["note_paths"]) == 2
    assert state["finalization"]["repair_rounds_used"] == 0
    assert calls == ["manager-finalize-1"]


def test_bounded_gap_repair_reuses_topic_pipeline(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    calls: list[str] = []
    repair_batches: list[list[str]] = []

    def fake_agent(workspace, agent_name, prompt, run_name, timeout, backend):
        calls.append(run_name)
        if run_name == "manager-prefinal-audit":
            audit_path = workspace / "artifacts/audits/prefinal_audit.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            gap = {
                "gap_id": "GAP-ABCDEF1234",
                "category": "evidence",
                "severity": "high",
                "related_scope_item": "benchmark comparison",
                "related_topic": "topics/a/one",
                "missing_evidence": "cross-benchmark evidence",
                "recommended_task": "Collect cross-benchmark evidence",
                "blocking": True,
                "justification": "Section 4 requires comparable protocols",
            }
            audit.update(
                {
                    "status": "REPAIR_REQUIRED",
                    "evidence_gaps": [gap],
                    "recommended_repairs": [gap],
                    "repair_required": True,
                }
            )
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
        elif "finalize" in run_name:
            (workspace / "SUMMARY.md").write_text(
                _valid_report(workspace), encoding="utf-8"
            )
        return True

    def fake_repairs(workspace, tasks, *args):
        repair_batches.append([task.topic_path for task in tasks])
        return {task.topic_path: True for task in tasks}

    monkeypatch.setattr("slrharness.orchestrator.run_named_agent", fake_agent)
    monkeypatch.setattr("slrharness.orchestrator._run_repair_tasks", fake_repairs)
    monkeypatch.setattr("slrharness.orchestrator.run_manager", lambda *args: True)
    monkeypatch.setattr("slrharness.orchestrator.tag_round", lambda *args: None)
    monkeypatch.setattr(
        "slrharness.orchestrator._verify_phase_committed", lambda *args: True
    )
    monkeypatch.setattr(
        "slrharness.orchestrator._commit_finalization_artifacts", lambda *args: None
    )
    assert run_finalization_pipeline(
        workspace,
        1,
        2,
        30,
        FakeBackend(),
        TopicExecutionConfig(),
        FinalizationConfig(max_prefinal_repair_rounds=1),
    )
    state = json.loads((workspace / "SLR_STATE.json").read_text(encoding="utf-8"))
    assert state["finalization"]["repair_rounds_used"] == 1
    assert repair_batches == [["topics/gap-repair/gap-abcdef1234"]]
    assert calls.count("manager-gap-repair-plan") == 1
    assert calls.count("manager-prefinal-reaudit") == 1
    assert calls.count("manager-finalize-1") == 1


def test_offline_manager_two_coordinator_pipeline_to_complete(
    tmp_path: Path, monkeypatch
) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    fixture = _workspace(fixture_root, ("topics/a/one", "topics/b/two"))
    second_note = fixture / "topics/b/two/papers/paper-2.md"
    second_note.write_text(
        second_note.read_text(encoding="utf-8").replace("LOCAL-2", "LOCAL-1"),
        encoding="utf-8",
    )
    second_index = fixture / "topics/b/two/papers/index.json"
    second_data = json.loads(second_index.read_text(encoding="utf-8"))
    second_data["papers"][0]["paper_id"] = "LOCAL-1"
    second_index.write_text(json.dumps(second_data), encoding="utf-8")

    workspace = tmp_path / "review"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    for filename in ("SCOPE.md", "SCOPE_ORIGINAL.md", "SUMMARY.md", "TASKS.md"):
        shutil.copyfile(fixture / filename, workspace / filename)
    shutil.copyfile(fixture / "SLR_STATE.json", workspace / "SLR_STATE.json")
    deploy_claude_assets(workspace)

    def fake_manager(workspace, round_num, phase, timeout, backend):
        tasks = workspace / "TASKS.md"
        if phase == "plan":
            tasks.write_text(
                "# Tasks\n- [ ] topics/a/one -- first\n- [ ] topics/b/two -- second\n",
                encoding="utf-8",
            )
        else:
            tasks.write_text(
                tasks.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                encoding="utf-8",
            )
        return True

    def fake_coordinators(workspace, tasks, round_num, config, backend):
        for task in tasks:
            source_root = fixture / task.topic_path
            destination_root = workspace / task.topic_path
            shutil.copytree(source_root, destination_root)
            source_synthesis = fixture / f"{task.topic_path}.md"
            destination_synthesis = workspace / f"{task.topic_path}.md"
            destination_synthesis.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_synthesis, destination_synthesis)
        return {task.topic_path: True for task in tasks}

    def fake_named(workspace, agent_name, prompt, run_name, timeout, backend):
        if "finalize" in run_name:
            (workspace / "SUMMARY.md").write_text(
                _valid_report(workspace), encoding="utf-8"
            )
        return True

    monkeypatch.setattr("slrharness.orchestrator.run_manager", fake_manager)
    monkeypatch.setattr(
        "slrharness.orchestrator.run_topic_coordinators", fake_coordinators
    )
    monkeypatch.setattr("slrharness.orchestrator.run_named_agent", fake_named)
    monkeypatch.setattr(
        "slrharness.orchestrator._verify_phase_committed", lambda *args: True
    )
    monkeypatch.setattr("slrharness.orchestrator.tag_round", lambda *args: None)
    monkeypatch.setattr(
        "slrharness.orchestrator._commit_finalization_artifacts", lambda *args: None
    )
    monkeypatch.setattr(
        "slrharness.orchestrator.is_workspace_clean", lambda *args: True
    )
    run_slr(
        workspace,
        1,
        2,
        30,
        30,
        FakeBackend(),
        topic_config=TopicExecutionConfig(mode="topic_coordinator"),
        finalization_config=replace(FinalizationConfig(), enable_prefinal_audit=False),
    )
    state = json.loads((workspace / "SLR_STATE.json").read_text(encoding="utf-8"))
    assert state["finalization"]["phase"] == "COMPLETE"
    registry = json.loads(
        (workspace / "artifacts/SOURCE_REGISTRY.json").read_text(encoding="utf-8")
    )
    assert len(registry["papers"]) == 1
    assert len(registry["papers"][0]["topic_paths"]) == 2
