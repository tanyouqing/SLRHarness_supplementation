from __future__ import annotations

import json
from pathlib import Path

import pytest

from slrharness.control_plane import (
    apply_verification_observation,
    current_issues,
    ingest_metadata_observation,
    migrate_legacy_tasks,
    register_tasks,
    transition_issue,
)
from slrharness.orchestrator import Task
from slrharness.orchestrator import run_topic_coordinators
from slrharness.scope_workflow import ScopeConfig, _new_state, save_scope_state
from slrharness.topic_execution import TopicExecutionConfig, topic_paths


def _workspace(path: Path) -> None:
    save_scope_state(path, _new_state("topic", ScopeConfig()))
    (path / "TASKS.md").write_text("# Tasks\n", encoding="utf-8")


def test_state_renders_tasks_and_legacy_is_loaded_once(tmp_path: Path) -> None:
    _workspace(tmp_path)
    register_tasks(tmp_path, [Task("topics/a/b", "research")], 2)
    rendered = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    assert "Program-rendered view" in rendered
    assert "task_id=a--b-" in rendered

    # Once state owns the view, later descriptive Markdown cannot create work.
    (tmp_path / "TASKS.md").write_text(
        "- [ ] topics/untrusted/new -- should not import\n", encoding="utf-8"
    )
    assert migrate_legacy_tasks(tmp_path) == 0
    assert "untrusted" not in json.dumps(current_issues(tmp_path))


def test_checker_observation_cannot_close_issue(tmp_path: Path) -> None:
    _workspace(tmp_path)
    note = tmp_path / "topics/a/b/papers/paper.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: Old\n---\nBody\n", encoding="utf-8")
    mismatch = {
        "note_path": note.relative_to(tmp_path).as_posix(),
        "status": "verified_closed",  # ignored agent control field
        "checked_fields": {
            "title": {
                "observed": "Old",
                "verified": "New",
                "source": "https://example.invalid",
                "match": False,
            }
        },
    }
    issue = ingest_metadata_observation(tmp_path, mismatch, "checker-1")[0]
    assert current_issues(tmp_path)[issue.issue_id]["status"] == "open"
    transition_issue(tmp_path, issue.issue_id, "repair_dispatched")
    transition_issue(tmp_path, issue.issue_id, "applied")
    verified = {**mismatch, "checked_fields": {"title": {**mismatch["checked_fields"]["title"], "observed": "New", "match": True}}}
    assert apply_verification_observation(tmp_path, verified) == [issue.issue_id]
    assert current_issues(tmp_path)[issue.issue_id]["status"] == "verified_closed"


def test_illegal_issue_close_is_rejected(tmp_path: Path) -> None:
    _workspace(tmp_path)
    note = tmp_path / "topics/a/b/papers/paper.md"
    note.parent.mkdir(parents=True)
    note.write_text("x", encoding="utf-8")
    issue = ingest_metadata_observation(
        tmp_path,
        {
            "note_path": note.relative_to(tmp_path).as_posix(),
            "checked_fields": {
                "authors": {
                    "observed": "A",
                    "verified": "B",
                    "source": "source",
                    "match": False,
                }
            },
        },
        "checker",
    )[0]
    with pytest.raises(ValueError):
        transition_issue(tmp_path, issue.issue_id, "verified_closed")


def test_program_scheduler_resumes_without_repeating_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace(tmp_path)
    calls: list[str] = []

    def fake_batch(workspace, invocations, backend):
        results = {}
        for invocation_id, _role, prompt, _timeout in invocations:
            calls.append(invocation_id)
            staging = Path(
                next(line for line in prompt.splitlines() if line.startswith("INVOCATION STAGING:"))
                .split(":", 1)[1]
                .strip()
            )
            if "-academic-" in invocation_id:
                output = staging / "papers/note.md"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    "---\nartifact_type: academic_paper_note\ntitle: Retrieval\nauthors: Smith\nyear: 2024\ndoi: 10.1000/test\naccess: full_text\nreading_status: depth_read\n---\n# Retrieval\n\nEvidence body.\n",
                    encoding="utf-8",
                )
            elif "-technical-" in invocation_id:
                output = staging / "technical/docs.md"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    "---\nartifact_type: technical_source_note\ntitle: Docs\norganization: Org\nresource_type: official_documentation\nurl: https://example.invalid\nverification_status: verified\n---\n# Docs\n\nTechnical evidence.\n",
                    encoding="utf-8",
                )
            elif "metadata" in invocation_id:
                note = topic_paths(workspace, "topics/a/b").paper_dir / "2024-smith-retrieval.md"
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "metadata_observations.jsonl").write_text(
                    json.dumps(
                        {
                            "note_path": note.relative_to(workspace).as_posix(),
                            "checked_fields": {
                                "title": {
                                    "observed": "Retrieval",
                                    "verified": "Retrieval",
                                    "source": "https://example.invalid",
                                    "match": True,
                                }
                            },
                            "remaining_uncertainties": [],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif "-synthesis-" in invocation_id:
                synthesis = topic_paths(workspace, "topics/a/b").synthesis
                synthesis.parent.mkdir(parents=True, exist_ok=True)
                synthesis.write_text(
                    "# Topic\n\n## Summary\n" + "Evidence synthesis. " * 30,
                    encoding="utf-8",
                )
            results[invocation_id] = True
        return results

    monkeypatch.setattr("slrharness.orchestrator._run_program_stage_batch", fake_batch)

    class Backend:
        def build_command(self, agent_name, prompt, cwd):
            return ["unused"]

    task = Task("topics/a/b", "research")
    assert run_topic_coordinators(
        tmp_path, [task], 1, TopicExecutionConfig(), Backend()
    ) == {"topics/a/b": True}
    first_calls = list(calls)
    assert any("-academic-" in value for value in first_calls)
    assert not topic_paths(tmp_path, task.topic_path).task_state.exists()
    assert not topic_paths(tmp_path, task.topic_path).manifest.exists()
    assert run_topic_coordinators(
        tmp_path, [task], 1, TopicExecutionConfig(), Backend()
    ) == {"topics/a/b": True}
    assert calls == first_calls
