"""Topic-coordinator contracts, deterministic validation, retry, and resume."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from slrharness.orchestrator import (
    CoordinatorProcessResult,
    Task,
    parse_pending_tasks,
    run_topic_coordinators,
)
from slrharness.topic_execution import (
    TOPIC_COORDINATOR,
    TopicExecutionConfig,
    build_coordinator_prompt,
    task_id_for_topic_path,
    topic_paths,
    validate_coordinator_outputs,
)


class RecordingBackend:
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, Path]] = []

    def check_available(self) -> None:
        return None

    def build_command(
        self, agent_name: str | None, prompt: str, cwd: Path
    ) -> list[str]:
        self.calls.append((agent_name, prompt, cwd))
        return ["fake-agent", "--agent", str(agent_name)]


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_valid_topic(
    workspace: Path,
    topic_path: str = "topics/memory/retrieval",
    *,
    status: str = "COMPLETE",
    unresolved: bool = False,
    no_results: bool = False,
) -> None:
    paths = topic_paths(workspace, topic_path)
    paths.artifact_root.mkdir(parents=True, exist_ok=True)
    paths.coordination_log.write_text('{"event":"completed"}\n', encoding="utf-8")
    paths.audit_dir.mkdir(parents=True, exist_ok=True)
    (paths.audit_dir / "correction_requests.jsonl").write_text("", encoding="utf-8")
    disclosure = " Unresolved metadata is disclosed." if unresolved else ""
    paths.synthesis.write_text(
        "# Retrieval\n\n## Summary\n" + ("Evidence. " * 40) + disclosure,
        encoding="utf-8",
    )
    if no_results:
        paper_index = paths.paper_dir / "NO_RESULTS.md"
        technical_index = paths.technical_dir / "NO_RESULTS.md"
        paper_index.parent.mkdir(parents=True, exist_ok=True)
        technical_index.parent.mkdir(parents=True, exist_ok=True)
        paper_index.write_text(
            "No reliable papers; providers attempted.", encoding="utf-8"
        )
        technical_index.write_text(
            "No reliable technical sources; providers attempted.", encoding="utf-8"
        )
        paper_count = source_count = 0
        items: list[dict[str, object]] = []
    else:
        paper_note = paths.paper_dir / "2024-smith-retrieval.md"
        paper_note.parent.mkdir(parents=True, exist_ok=True)
        paper_note.write_text("# Paper note\n", encoding="utf-8")
        paper_index = paths.paper_dir / "index.json"
        _write_json(
            paper_index,
            {
                "task_id": paths.task_id,
                "paper_count": 1,
                "papers": [
                    {
                        "note_path": paper_note.relative_to(workspace).as_posix(),
                        "title": "Retrieval",
                    }
                ],
            },
        )
        technical_note = paths.technical_dir / "project-docs.md"
        technical_note.parent.mkdir(parents=True, exist_ok=True)
        technical_note.write_text("# Technical note\n", encoding="utf-8")
        technical_index = paths.technical_dir / "index.json"
        _write_json(
            technical_index,
            {
                "task_id": paths.task_id,
                "source_count": 1,
                "sources": [
                    {"note_path": technical_note.relative_to(workspace).as_posix()}
                ],
            },
        )
        paper_count = source_count = 1
        items = [
            {
                "note_path": paper_note.relative_to(workspace).as_posix(),
                "status": "UNRESOLVED" if unresolved else "PASS",
            }
        ]
    audit = paths.audit_dir / "metadata_check.json"
    _write_json(
        audit,
        {
            "task_id": paths.task_id,
            "overall_status": "PARTIAL" if unresolved else "PASS",
            "paper_count": paper_count,
            "checked_count": paper_count,
            "passed_count": 0 if unresolved else paper_count,
            "corrected_count": 0,
            "unresolved_count": paper_count if unresolved else 0,
            "items": items,
        },
    )
    _write_json(
        paths.manifest,
        {
            "task_id": paths.task_id,
            "status": status,
            "topic_synthesis": paths.synthesis.relative_to(workspace).as_posix(),
            "academic_worker": {
                "index_path": paper_index.relative_to(workspace).as_posix(),
                "paper_count": paper_count,
            },
            "technical_worker": {
                "index_path": technical_index.relative_to(workspace).as_posix(),
                "source_count": source_count,
            },
            "metadata_checker": {
                "audit_path": audit.relative_to(workspace).as_posix(),
                "checked_count": paper_count,
            },
        },
    )


def test_stable_task_id_and_safe_paths(tmp_path: Path) -> None:
    assert task_id_for_topic_path("topics/a/b") == task_id_for_topic_path("topics/a/b")
    with pytest.raises(ValueError):
        topic_paths(tmp_path, "topics/../escape")


def test_prompt_names_roles_parallel_and_file_corrections(tmp_path: Path) -> None:
    paths = topic_paths(tmp_path, "topics/a/b")
    prompt = build_coordinator_prompt(
        tmp_path, paths, "question", 2, 1, TopicExecutionConfig(), ["prior error"]
    )
    assert "academic-paper-worker" in prompt
    assert "technical-source-worker" in prompt
    assert "academic-metadata-checker" in prompt
    assert "concurrently" in prompt
    assert "correction_requests.jsonl" in prompt
    assert "prior error" in prompt
    assert "Agent Team" in prompt


def test_valid_complete_and_zero_result_topics(tmp_path: Path) -> None:
    write_valid_topic(tmp_path)
    assert validate_coordinator_outputs(
        tmp_path, topic_paths(tmp_path, "topics/memory/retrieval")
    ).accepted
    write_valid_topic(tmp_path, "topics/memory/empty", no_results=True)
    assert validate_coordinator_outputs(
        tmp_path, topic_paths(tmp_path, "topics/memory/empty")
    ).accepted


@pytest.mark.parametrize(
    "missing",
    ["synthesis", "manifest", "paper", "technical", "audit"],
)
def test_missing_required_artifact_is_rejected(tmp_path: Path, missing: str) -> None:
    write_valid_topic(tmp_path)
    paths = topic_paths(tmp_path, "topics/memory/retrieval")
    target = {
        "synthesis": paths.synthesis,
        "manifest": paths.manifest,
        "paper": paths.paper_dir / "index.json",
        "technical": paths.technical_dir / "index.json",
        "audit": paths.audit_dir / "metadata_check.json",
    }[missing]
    target.unlink()
    assert not validate_coordinator_outputs(tmp_path, paths).accepted


def test_task_mismatch_escape_and_failed_status_are_rejected(tmp_path: Path) -> None:
    write_valid_topic(tmp_path)
    paths = topic_paths(tmp_path, "topics/memory/retrieval")
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    manifest["task_id"] = "wrong"
    manifest["academic_worker"]["index_path"] = "SCOPE.md"
    manifest["status"] = "FAILED"
    _write_json(paths.manifest, manifest)
    result = validate_coordinator_outputs(tmp_path, paths)
    assert not result.accepted
    assert any("mismatch" in error for error in result.errors)
    assert any("escapes" in error for error in result.errors)


def test_partial_unresolved_requires_disclosure_and_permission(tmp_path: Path) -> None:
    write_valid_topic(tmp_path, status="PARTIAL", unresolved=True)
    paths = topic_paths(tmp_path, "topics/memory/retrieval")
    assert validate_coordinator_outputs(tmp_path, paths).accepted
    assert not validate_coordinator_outputs(tmp_path, paths, False).accepted
    paths.synthesis.write_text("x" * 250, encoding="utf-8")
    assert not validate_coordinator_outputs(tmp_path, paths).accepted


def test_count_mismatch_downgrades_to_partial(tmp_path: Path) -> None:
    write_valid_topic(tmp_path)
    paths = topic_paths(tmp_path, "topics/memory/retrieval")
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    manifest["technical_worker"]["source_count"] = 2
    _write_json(paths.manifest, manifest)
    result = validate_coordinator_outputs(tmp_path, paths)
    assert result.accepted
    assert result.effective_status == "PARTIAL"
    assert result.warnings


def test_corrected_metadata_status_is_accepted(tmp_path: Path) -> None:
    write_valid_topic(tmp_path)
    paths = topic_paths(tmp_path, "topics/memory/retrieval")
    audit_path = paths.audit_dir / "metadata_check.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["items"][0]["status"] = "CORRECTED"
    audit["passed_count"] = 0
    audit["corrected_count"] = 1
    _write_json(audit_path, audit)
    assert validate_coordinator_outputs(tmp_path, paths).accepted


def test_mode_marker_is_backward_compatible(tmp_path: Path) -> None:
    tasks = tmp_path / "TASKS.md"
    tasks.write_text(
        "- [ ] topics/a/b -- [mode=topic_coordinator] inspect evidence\n"
        "- [ ] topics/c/d -- old task\n",
        encoding="utf-8",
    )
    parsed = parse_pending_tasks(tasks)
    assert parsed[0].execution_mode == TOPIC_COORDINATOR
    assert parsed[0].description == "inspect evidence"
    assert parsed[1].execution_mode is None


def test_runner_uses_one_named_coordinator_and_resumes(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    backend = RecordingBackend()
    task = Task("topics/memory/retrieval", "research retrieval")
    attempts: list[int] = []

    def fake_batch(workspace, invocations, timeout):
        assert timeout == 3600
        for invocation in invocations:
            attempts.append(invocation.attempt)
            write_valid_topic(workspace, invocation.task.topic_path)
        return {
            invocation.task.topic_path: CoordinatorProcessResult(True, 0, False, pid=42)
            for invocation in invocations
        }

    config = replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR)
    result = run_topic_coordinators(
        tmp_path, [task], 1, config, backend, batch_executor=fake_batch
    )
    assert result == {task.topic_path: True}
    assert attempts == [1]
    assert len(backend.calls) == 1
    assert backend.calls[0][0] == "topic-coordinator"

    result = run_topic_coordinators(
        tmp_path, [task], 1, config, backend, batch_executor=fake_batch
    )
    assert result == {task.topic_path: True}
    assert attempts == [1]
    assert len(backend.calls) == 1


def test_runner_retries_without_deleting_supporting_files(tmp_path: Path) -> None:
    backend = RecordingBackend()
    task = Task("topics/a/b", "question")
    calls = 0

    def flaky_batch(workspace, invocations, timeout):
        nonlocal calls
        calls += 1
        paths = topic_paths(workspace, task.topic_path)
        preserved = paths.paper_dir / "preserved.md"
        if calls == 1:
            preserved.write_text("keep", encoding="utf-8")
            return {task.topic_path: CoordinatorProcessResult(True, 7, False)}
        assert preserved.read_text(encoding="utf-8") == "keep"
        write_valid_topic(workspace, task.topic_path)
        return {task.topic_path: CoordinatorProcessResult(True, 0, False)}

    config = replace(
        TopicExecutionConfig(), mode=TOPIC_COORDINATOR, coordinator_retries=1
    )
    assert run_topic_coordinators(
        tmp_path, [task], 3, config, backend, batch_executor=flaky_batch
    )[task.topic_path]
    state = json.loads(
        topic_paths(tmp_path, task.topic_path).task_state.read_text(encoding="utf-8")
    )
    assert calls == 2
    assert state["attempt"] == 2
    assert state["status"] == "COMPLETE"


def test_batch_contract_keeps_topics_in_distinct_artifact_roots(tmp_path: Path) -> None:
    backend = RecordingBackend()
    tasks = [Task("topics/a/one", "one"), Task("topics/a/two", "two")]

    def fake_batch(workspace, invocations, timeout):
        assert len(invocations) == 2
        roots = {
            topic_paths(workspace, invocation.task.topic_path).artifact_root
            for invocation in invocations
        }
        assert len(roots) == 2
        for invocation in invocations:
            write_valid_topic(workspace, invocation.task.topic_path)
        return {
            invocation.task.topic_path: CoordinatorProcessResult(True, 0, False)
            for invocation in invocations
        }

    config = replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR)
    results = run_topic_coordinators(
        tmp_path, tasks, 1, config, backend, batch_executor=fake_batch
    )
    assert all(results.values())
    assert [call[0] for call in backend.calls] == [
        "topic-coordinator",
        "topic-coordinator",
    ]
