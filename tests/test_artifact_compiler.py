"""Focused recovery tests for program-owned topic control artifacts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from slrharness.artifact_compiler import compile_topic_artifacts
from slrharness.orchestrator import (
    CoordinatorProcessResult,
    Task,
    run_topic_coordinators,
)
from slrharness.source_registry import aggregate_sources
from slrharness.topic_execution import (
    TOPIC_COORDINATOR,
    TopicExecutionConfig,
    initialize_topic_attempt,
    topic_paths,
)


class _Backend:
    name = "fake"

    def check_available(self) -> None:
        return None

    def build_command(self, agent_name, prompt, cwd):
        return ["fake", str(agent_name), prompt]


def _paper(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
artifact_type: academic_paper_note
title: "Recoverable Paper"
authors: "Smith, A."
year: "2024"
doi: "10.1000/recoverable"
access: full_text
reading_status: depth_read
metadata_status: PASS
research_line_ids: ["RL-RECOVERY"]
primary_evidence_role: "not-a-role"
---
# Recoverable Paper
## Problem and motivation
Reliable control artifacts should not depend on agent bookkeeping.
## Core method and contribution
The method is described with evidence.
## Evidence
| Evidence ID | Locator |
|---|---|
| E1 | Section 2 |
""",
        encoding="utf-8",
    )


def _replace_frontmatter_value(path: Path, old: str, new: str) -> None:
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )


def _technical(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
artifact_type: technical_source_note
title: "Official Documentation"
organization: "Example"
resource_type: official_documentation
url: "https://example.invalid/docs"
verification_status: verified
research_line_ids: ["RL-RECOVERY"]
support_role: implementation_detail
---
# Official Documentation
## Technical content
Implementation details.
""",
        encoding="utf-8",
    )


def _synthesis(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Recovery\n\n"
        "## Scope-Driven Research-Line Assessment\n"
        "Research line RL-RECOVERY has locally supported evidence.\n\n"
        + "Evidence-backed synthesis. " * 20,
        encoding="utf-8",
    )


def _initialize(workspace: Path):
    paths = topic_paths(workspace, "topics/recovery/example")
    initialize_topic_attempt(
        paths,
        "recover malformed artifacts",
        1,
        1,
        replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR),
        research_line_id="RL-RECOVERY",
    )
    return paths


def test_compiler_repairs_nested_paths_counts_manifest_and_roles(
    tmp_path: Path,
) -> None:
    (tmp_path / "SCOPE.md").write_text("# Legacy scope\n", encoding="utf-8")
    paths = _initialize(tmp_path)
    _paper(paths.paper_dir / "notes" / "paper.md")
    _replace_frontmatter_value(
        paths.paper_dir / "notes" / "paper.md",
        'title: "Recoverable Paper"',
        'paper_id: "P-AGENT"\ntitle: "Recoverable Paper"',
    )
    _technical(paths.technical_dir / "notes" / "docs.md")
    _synthesis(paths.synthesis)
    paths.metadata_findings.write_text(
        json.dumps(
            {
                "note_path": "paper.md",
                "status": "PASS",
                "checked_fields": {"title": {"verified": "Recoverable Paper"}},
                "remaining_uncertainties": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paths.manifest.write_text(
        json.dumps(
            {
                "task_id": "wrong",
                "paper_count": "many",
                "prioritization": {
                    "paper_role_records": [
                        {
                            "paper_id": "P-AGENT",
                            "research_line_id": "RL-RECOVERY",
                            "role": "anchor",
                            "reason": "representative method",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    preview = compile_topic_artifacts(
        tmp_path,
        paths,
        replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR),
        None,
        dry_run=True,
    )
    assert preview.recovered_paths
    assert (paths.paper_dir / "notes" / "paper.md").is_file()
    assert not paths.normalization_report.exists()

    result = compile_topic_artifacts(
        tmp_path,
        paths,
        replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR),
        CoordinatorProcessResult(True, 0, False),
    )

    assert result.compiled
    assert result.status == "PARTIAL"
    assert (paths.paper_dir / "paper.md").is_file()
    assert (paths.technical_dir / "docs.md").is_file()
    assert result.paper_count == result.manifest["academic_worker"]["paper_count"] == 1
    assert result.technical_source_count == 1
    assert result.manifest["producer"] == "slrharness"
    assert result.manifest["task_id"] == paths.task_id
    assert result.manifest["prioritization"]["paper_roles"] == {
        "P-AGENT": "anchor"
    }
    paper_index = json.loads((paths.paper_dir / "index.json").read_text())
    assert paper_index["papers"][0]["paper_id"] != "P-AGENT"
    assert paper_index["papers"][0]["primary_evidence_role"] == "unassigned"
    assert paper_index["papers"][0]["note_path"].endswith("/papers/paper.md")
    audit = json.loads((paths.audit_dir / "metadata_check.json").read_text())
    assert audit["overall_status"] == "PASS"
    assert audit["checked_count"] == audit["paper_count"] == 1
    assert paths.normalization_report.is_file()
    assert (paths.audit_dir / "agent_manifest_input.json").is_file()
    registry = aggregate_sources(tmp_path).registry
    assert len(registry["papers"]) == 1
    assert len(registry["technical_sources"]) == 1


def test_timeout_with_complete_artifacts_is_accepted_as_partial(
    tmp_path: Path,
) -> None:
    paths = _initialize(tmp_path)
    _paper(paths.paper_dir / "paper.md")
    _technical(paths.technical_dir / "docs.md")
    _synthesis(paths.synthesis)
    paths.metadata_findings.write_text(
        '{"note_path":"paper.md","status":"PASS"}\n', encoding="utf-8"
    )
    result = compile_topic_artifacts(
        tmp_path,
        paths,
        replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR),
        CoordinatorProcessResult(False, None, True),
    )
    assert result.compiled
    assert result.status == "PARTIAL"
    assert result.manifest["process"]["timed_out"] is True

    rebuilt = compile_topic_artifacts(
        tmp_path,
        paths,
        replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR),
        None,
    )
    assert rebuilt.status == "PARTIAL"
    assert rebuilt.manifest["process"]["timed_out"] is True


def test_runner_accepts_valid_timeout_artifacts_as_partial(tmp_path: Path) -> None:
    task = Task(
        "topics/recovery/example",
        "recover",
        research_line_id="RL-RECOVERY",
    )

    def batch(workspace, invocations, timeout):
        paths = topic_paths(workspace, task.topic_path)
        _paper(paths.paper_dir / "paper.md")
        _technical(paths.technical_dir / "docs.md")
        _synthesis(paths.synthesis)
        paths.metadata_findings.write_text(
            '{"note_path":"paper.md","status":"PASS"}\n',
            encoding="utf-8",
        )
        return {task.topic_path: CoordinatorProcessResult(False, None, True)}

    config = replace(
        TopicExecutionConfig(), mode=TOPIC_COORDINATOR, coordinator_retries=0
    )
    result = run_topic_coordinators(
        tmp_path, [task], 1, config, _Backend(), batch_executor=batch
    )
    paths = topic_paths(tmp_path, task.topic_path)
    state = json.loads(paths.task_state.read_text(encoding="utf-8"))
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert result[task.topic_path]
    assert state["status"] == "PARTIAL"
    assert manifest["status"] == "PARTIAL"
    assert manifest["process"]["timed_out"] is True


def test_retry_prompt_uses_checkpoint_and_only_missing_synthesis(
    tmp_path: Path,
) -> None:
    task = Task(
        "topics/recovery/example",
        "recover",
        research_line_id="RL-RECOVERY",
    )
    calls = 0

    def batch(workspace, invocations, timeout):
        nonlocal calls
        calls += 1
        paths = topic_paths(workspace, task.topic_path)
        if calls == 1:
            preserved = paths.paper_dir / "preserved.md"
            preserved.write_text("keep", encoding="utf-8")
            _paper(paths.paper_dir / "paper.md")
            _technical(paths.technical_dir / "docs.md")
            paths.metadata_findings.write_text(
                '{"note_path":"paper.md","status":"PASS"}\n',
                encoding="utf-8",
            )
            return {task.topic_path: CoordinatorProcessResult(True, 7, False)}
        prompt = invocations[0].prompt
        assert '"topic_synthesis"' in prompt
        assert '"academic_research"' not in prompt
        assert '"technical_research"' not in prompt
        assert (paths.paper_dir / "preserved.md").read_text(
            encoding="utf-8"
        ) == "keep"
        _synthesis(paths.synthesis)
        return {task.topic_path: CoordinatorProcessResult(True, 0, False)}

    config = replace(
        TopicExecutionConfig(), mode=TOPIC_COORDINATOR, coordinator_retries=1
    )
    result = run_topic_coordinators(
        tmp_path, [task], 1, config, _Backend(), batch_executor=batch
    )
    assert result[task.topic_path]
    assert calls == 2
    assert (topic_paths(tmp_path, task.topic_path).paper_dir / "preserved.md").is_file()
