# ruff: noqa: E501
"""Canonical notes, source aggregation, pre-final audit, and final validation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from slrharness.finalization import (
    FINAL_AUDIT_PATH,
    FinalizationConfig,
    build_finalizer_prompt,
    delivery_statistics,
    ensure_stable_repair_tasks,
    run_structural_prefinal_audit,
    validate_final_report,
)
from slrharness.orchestrator import (
    Task,
    _run_repair_tasks,
    run_finalization_pipeline,
)
from slrharness.scope_workflow import initialize_direct_approved_state
from slrharness.prioritization import write_scope_prioritization
from slrharness.source_registry import (
    REGISTRY_PATH,
    _deduplicate_papers,
    aggregate_sources,
    stable_paper_id,
    validate_paper_note,
)
from slrharness.topic_execution import TOPIC_COORDINATOR, TopicExecutionConfig
from slrharness.workspace_assets import deploy_claude_assets


class FakeBackend:
    name = "fake"

    def check_available(self):
        return None

    def build_command(self, agent_name, prompt, cwd):
        return ["fake", str(agent_name), prompt]


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _note(
    path: Path,
    *,
    paper_id: str = "P-DOI-EXAMPLE",
    title: str = "A Reliable Memory Method",
    doi: str = "10.1000/example",
    arxiv: str = "2401.00001",
    access: str = "full_text",
    metadata_status: str = "PASS",
    numeric_with_locator: bool = True,
) -> None:
    result = (
        "42% on Bench against Base [E1, Table 2]" if numeric_with_locator else "42%"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
paper_id: "{paper_id}"
title: "{title}"
authors: "Smith, A.; Lee, B."
year: "2024"
venue: "Example Conference"
doi: "{doi}"
arxiv_id: "{arxiv}"
public_url: "https://doi.org/{doi}"
version: "conference"
version_group: "memory-method"
access: {access}
reading_status: depth_read
metadata_status: {metadata_status}
discovery_route: keyword_search
note_status: complete
---

# {title}

## Identity and access
Sections/pages read: Sections 1-6.

## Problem and motivation
Persistent memory is difficult under long interaction histories.

## Research objective and assumptions
The paper studies bounded retrieval under a fixed model.

## Core method and contribution
Reported by the paper: a retrieval and update mechanism. Reviewer interpretation
is labelled separately.

## Data, experiments, and quantitative results
| Dataset / version / split | Model / baseline | Configuration / resources | Metric | Reported result | Evidence ID / locator |
|---|---|---|---|---|---|
| Bench / v1 / test | Model / Base | fixed prompt | accuracy | {result} | E1 / Table 2 |

## Reported conclusions
The paper reports improved accuracy [E1].

## Reviewer interpretation
The mechanism may trade storage for retrieval quality.

## Limitations
### Reported limitations
Limited benchmark coverage.
### Inferred limitations
Cross-model transfer is uncertain.

## Evidence
| Evidence ID | Evidence type | Page / section / table / figure | Supporting-text paraphrase | Source URL | Verification status |
|---|---|---|---|---|---|
| E1 | reported result | Table 2 | Accuracy under the stated setup | https://doi.org/{doi} | verified |

## Landscape relationships and scope relevance
Directly relevant to the approved memory scope.
""",
        encoding="utf-8",
    )


def _workspace(tmp_path: Path, topics: tuple[str, ...] = ("topics/a/one",)) -> Path:
    workspace = tmp_path
    (workspace / "SCOPE.md").write_text(
        "# Approved Scope\nMemory systems", encoding="utf-8"
    )
    (workspace / "SCOPE_ORIGINAL.md").write_text(
        "# Approved Scope\nMemory systems", encoding="utf-8"
    )
    (workspace / "SUMMARY.md").write_text("# Draft\n", encoding="utf-8")
    task_lines = [f"- [x] {topic} -- completed" for topic in topics]
    (workspace / "TASKS.md").write_text("\n".join(task_lines), encoding="utf-8")
    initialize_direct_approved_state(workspace, "Memory")
    deploy_claude_assets(workspace)
    for index, topic in enumerate(topics, start=1):
        root = workspace / topic
        synthesis = workspace / f"{topic}.md"
        synthesis.parent.mkdir(parents=True, exist_ok=True)
        synthesis.write_text(
            "# Topic\nSupporting papers/index.json, technical_sources/index.json, "
            "and audits/metadata_check.json.\n" + "Evidence. " * 30,
            encoding="utf-8",
        )
        note_rel = f"{topic}/papers/paper-{index}.md"
        _note(workspace / note_rel, paper_id=f"LOCAL-{index}")
        paper_index = {
            "task_id": f"task-{index}",
            "paper_count": 1,
            "papers": [
                {
                    "note_path": note_rel,
                    "paper_id": f"LOCAL-{index}",
                    "title": "A Reliable Memory Method",
                    "authors": "Smith, A.; Lee, B.",
                    "year": "2024",
                    "doi": "10.1000/example",
                    "arxiv_id": "2401.00001",
                    "metadata_check_status": "PASS",
                    "inclusion_status": "included",
                    "access_level": "full_text",
                    "evidence_ids": ["E1"],
                }
            ],
        }
        _json(root / "papers/index.json", paper_index)
        technical_rel = f"{topic}/technical_sources/docs-{index}.md"
        (workspace / technical_rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / technical_rel).write_text(
            "# Official docs\nContent", encoding="utf-8"
        )
        _json(
            root / "technical_sources/index.json",
            {
                "task_id": f"task-{index}",
                "source_count": 1,
                "sources": [
                    {
                        "source_id": f"LOCAL-T-{index}",
                        "note_path": technical_rel,
                        "title": "Official Memory Documentation",
                        "organization": "Example Org",
                        "resource_type": "official_documentation",
                        "url": "https://example.org/docs",
                        "verification_status": "PASS",
                        "inclusion_status": "included",
                    }
                ],
            },
        )
        _json(
            root / "audits/metadata_check.json",
            {
                "task_id": f"task-{index}",
                "overall_status": "PASS",
                "paper_count": 1,
                "checked_count": 1,
                "passed_count": 1,
                "corrected_count": 0,
                "unresolved_count": 0,
                "items": [{"note_path": note_rel, "status": "PASS"}],
            },
        )
        _json(
            root / "coordinator_manifest.json",
            {
                "task_id": f"task-{index}",
                "status": "COMPLETE",
                "topic_synthesis": f"{topic}.md",
                "academic_worker": {
                    "index_path": f"{topic}/papers/index.json",
                    "paper_count": 1,
                },
                "technical_worker": {
                    "index_path": f"{topic}/technical_sources/index.json",
                    "source_count": 1,
                },
                "metadata_checker": {
                    "audit_path": f"{topic}/audits/metadata_check.json"
                },
            },
        )
    return workspace


def _valid_report(workspace: Path) -> str:
    registry = json.loads((workspace / REGISTRY_PATH).read_text(encoding="utf-8"))
    paper_id = registry["papers"][0]["source_id"]
    technical_id = registry["technical_sources"][0]["source_id"]
    line = registry["research_lines"][0]
    line_id = line["line_id"]
    line_group = line["group"]
    delivery = delivery_statistics(workspace, registry)
    delivery_lines = "\n".join(f"- {key}: {value}" for key, value in delivery.items())
    filler = (
        "This evidence-backed discussion explains boundaries and applicability. " * 5
    )
    return f"""# Literature Review: Memory

## Section 1 — Academic Terminology and Problem Boundaries
Memory means persisted agent information ({paper_id}) [{paper_id}]. {filler}

## Section 2 — Background, Importance, and Broader Significance
The problem matters for reliable systems [{paper_id}]. {filler}

## Section 3 — Existing Research: Motivations, Methodologies, and Findings
### Organization and Prioritization Policy
Research lines, not papers, are ranked qualitatively; this is not a paper-quality ranking.
### Scope-Driven Research-Line Prioritization
| Order | Research Line ID | Research Line | Group | Tier/Score | Evidence basis | Representative papers |
|---|---|---|---|---|---|---|
| 1 | {line_id} | {line["name"]} | {line_group} | Insufficient Evidence | one topic | [{paper_id}] |
### Findings by Research Line
Work is organized by approved mechanism group {line_group}. {filler}
| Work | Motivation | Method | Benchmark | Finding | Limitation |
|---|---|---|---|---|---|
| Smith (2024) [{paper_id}] | persistence | retrieval | Bench | improvement | narrow data |

## Section 4 — Research Landscape: Consensus, Differences, and Experimental Practice
### Consensus
Independent evidence is limited, so no broad consensus is claimed [{paper_id}].
The model, dataset, benchmark, baseline, metric, and evaluation protocol are reported.
### Differences and contradictions
Framework and model-version differences prevent direct comparison [{paper_id}]. {filler}

## Section 5 — Evidence-Backed Research Opportunities
| Opportunity | Evidence gap | Motivating papers | Why unresolved | Possible validation |
|---|---|---|---|---|
| transfer tests | narrow benchmark | [{paper_id}] | absent cross-model data | held-out models |
The risk is protocol sensitivity and the direction is a reviewer inference.

## Coverage and Limitations
### Prioritization Limitations
The qualitative fallback contains agent judgment and is not a paper-quality ranking.
One paper was depth-read. Retrieval and metadata limits are disclosed. {filler}

## Sources and Provenance
Academic notes, topic synthesis, registry, audits, and official docs [{technical_id}]
are persisted under workspace-relative artifact paths. {filler}

## References
- [{paper_id}] Smith, A.; Lee, B. (2024). A Reliable Memory Method.
- [{technical_id}] Example Org. Official Memory Documentation.

## Delivery Status
{delivery_lines}
Scope approved; one topic, one paper, one technical source, no unresolved metadata.
Final validation is program controlled. {filler}
"""


def test_templates_deploy_and_academic_agent_references_canonical_path(
    tmp_path: Path,
) -> None:
    deploy_claude_assets(tmp_path)
    assert (tmp_path / ".claude/templates/paper-note.md").is_file()
    assert (tmp_path / ".claude/templates/final-report.md").is_file()
    agent = (tmp_path / ".claude/agents/academic-paper-worker.md").read_text(
        encoding="utf-8"
    )
    assert ".claude/templates/paper-note.md" in agent


def test_paper_note_validation_accepts_contract_and_not_reported(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.md"
    _note(note)
    assert validate_paper_note(tmp_path, note).valid
    text = note.read_text(encoding="utf-8").replace(
        "| Bench / v1 / test | Model / Base | fixed prompt | accuracy | 42% on Bench against Base [E1, Table 2] | E1 / Table 2 |",
        "| Not reported | Not applicable | Not reported | Not reported | Not reported | Not applicable |",
    )
    note.write_text(text, encoding="utf-8")
    assert validate_paper_note(tmp_path, note).valid


def test_note_validation_rejects_missing_title_and_invalid_access(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.md"
    _note(note, access="secret")
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            'title: "A Reliable Memory Method"\n', ""
        ),
        encoding="utf-8",
    )
    result = validate_paper_note(tmp_path, note)
    assert not result.valid
    assert any("title" in error for error in result.errors)
    assert any("access" in error for error in result.errors)


def test_abstract_only_and_numeric_warning(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    _note(note, access="abstract_only", numeric_with_locator=False)
    text = note.read_text(encoding="utf-8").replace(" | E1 / Table 2 |", " | none |")
    note.write_text(text, encoding="utf-8")
    result = validate_paper_note(tmp_path, note)
    assert result.valid
    assert result.warnings


def test_paper_note_research_line_roles_are_optional_warnings(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    note = workspace / "topics/a/one/papers/paper-1.md"
    legacy = validate_paper_note(workspace, note)
    assert legacy.valid
    assert any("research_line_ids" in warning for warning in legacy.warnings)
    text = note.read_text(encoding="utf-8").replace(
        "note_status: complete",
        'note_status: complete\nresearch_line_ids: ["RL-A-ONE"]\nprimary_evidence_role: "contradictory"',
    ).replace(
        "## Landscape relationships and scope relevance",
        "## Scope-Driven Positioning",
    )
    note.write_text(text, encoding="utf-8")
    valid = validate_paper_note(workspace, note)
    assert valid.valid
    assert not any("invalid evidence role" in warning for warning in valid.warnings)
    note.write_text(text.replace('"contradictory"', '"winner"'), encoding="utf-8")
    invalid = validate_paper_note(workspace, note)
    assert invalid.valid
    assert any("invalid evidence role" in warning for warning in invalid.warnings)


def test_registry_merges_topics_by_line_and_preserves_uncovered_scope_line(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, ("topics/a/one", "topics/b/two"))
    scope = """# Scope
## Proposed Literature Organization
| Research Line ID | Research Line | Primary Group | Definition | Main Scope Question |
|---|---|---|---|---|
| RL-SHARED | Shared line | Methods | shared | question |
| RL-UNCOVERED | Uncovered line | Methods | uncovered | question |
## Research-Line Prioritization and Synthesis Policy
### Ranking Unit
research_line
### Primary Grouping
Methods
### Ranking Mode
ordinal
### Priority Tiers
Core, Supporting, Peripheral, Insufficient Evidence
### Missing-Data Policy
unknown_not_zero
"""
    (workspace / "SCOPE.md").write_text(scope, encoding="utf-8")
    write_scope_prioritization(workspace)
    (workspace / "TASKS.md").write_text(
        "- [x] topics/a/one -- [line=RL-SHARED] complete\n"
        "- [x] topics/b/two -- [line=RL-SHARED] complete\n",
        encoding="utf-8",
    )
    registry = aggregate_sources(workspace).registry
    lines = {line["line_id"]: line for line in registry["research_lines"]}
    assert len(lines["RL-SHARED"]["topic_paths"]) == 2
    assert lines["RL-UNCOVERED"]["ranking_status"] == "INSUFFICIENT_EVIDENCE"
    assert registry["papers"][0]["research_line_ids"] == ["RL-SHARED"]


def test_aggregation_deduplicates_doi_and_is_idempotent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, ("topics/a/one", "topics/b/two"))
    first = aggregate_sources(workspace)
    second = aggregate_sources(workspace)
    assert len(first.registry["papers"]) == 1
    assert len(first.registry["technical_sources"]) == 1
    assert len(first.registry["papers"][0]["note_paths"]) == 2
    assert (
        first.registry["papers"][0]["source_id"]
        == second.registry["papers"][0]["source_id"]
    )
    assert set(first.registry["topic_coverage"]) == {
        "topics/a/one.md",
        "topics/b/two.md",
    }
    assert first.dedup_audit["duplicates"]
    assert first.note_audit["status"] == "FAILED"
    assert any("same identifier" in error for error in first.note_audit["errors"])


def test_unresolved_metadata_is_collected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    note = workspace / "topics/a/one/papers/paper-1.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "metadata_status: PASS", "metadata_status: UNRESOLVED"
        ),
        encoding="utf-8",
    )
    registry = aggregate_sources(workspace).registry
    assert registry["unresolved_metadata"] == [registry["papers"][0]["source_id"]]


def test_arxiv_dedup_and_distinct_dois(tmp_path: Path) -> None:
    assert stable_paper_id({"arxiv_id": "2401.1v2"}) == stable_paper_id(
        {"arxiv_id": "2401.1"}
    )
    assert stable_paper_id({"doi": "10.1/a", "title": "Same"}) != stable_paper_id(
        {"doi": "10.1/b", "title": "Same"}
    )
    distinct, _ = _deduplicate_papers(
        [
            {"doi": "10.1/a", "title": "Same", "note_path": "a.md"},
            {"doi": "10.1/b", "title": "Same", "note_path": "b.md"},
        ],
        {},
    )
    assert len(distinct) == 2
    versions, _ = _deduplicate_papers(
        [
            {
                "doi": "10.1/a",
                "title": "Preprint",
                "version_group": "work-1",
                "note_path": "a.md",
            },
            {
                "doi": "10.1/b",
                "title": "Conference",
                "version_group": "work-1",
                "note_path": "b.md",
            },
        ],
        {},
    )
    assert len(versions) == 1


def test_corrected_metadata_must_match_note(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    _note(note, metadata_status="CORRECTED")
    audit_item = {
        "status": "CORRECTED",
        "checked_fields": {"title": {"verified": "A Different Corrected Title"}},
    }
    result = validate_paper_note(tmp_path, note, audit_item=audit_item)
    assert not result.valid
    assert any("corrected metadata" in error for error in result.errors)


def test_prefinal_pass_and_missing_synthesis_stable_gap(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    aggregate_sources(workspace)
    config = FinalizationConfig()
    passed = run_structural_prefinal_audit(workspace, config)
    assert passed["status"] == "PASS_WITH_LIMITATIONS"
    assert any(
        gap["category"] == "prioritization"
        for gap in passed["coverage_gaps"]
    )
    (workspace / "topics/a/one.md").unlink()
    failed1 = run_structural_prefinal_audit(workspace, config)
    failed2 = run_structural_prefinal_audit(workspace, config)
    assert failed1["status"] == "REPAIR_REQUIRED"
    assert (
        failed1["structural_issues"][0]["gap_id"]
        == failed2["structural_issues"][0]["gap_id"]
    )
    limited = run_structural_prefinal_audit(workspace, config, repair_rounds_used=1)
    assert limited["status"] == "PASS_WITH_LIMITATIONS"


def test_finalizer_prompt_has_explicit_inputs_and_no_search(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    aggregate_sources(workspace)
    prompt = build_finalizer_prompt(
        workspace, FinalizationConfig(), ["missing Section 4"]
    )
    for path in (
        "SCOPE.md",
        "SCOPE_PRIORITIZATION.json",
        "SOURCE_REGISTRY.json",
        "PAPER_LIST.md",
        "REFERENCES.md",
        "prefinal_audit.json",
        "SUMMARY.md",
    ):
        assert path in prompt
    assert "Do not search the web" in prompt
    assert "missing Section 4" in prompt


def test_valid_final_report_and_unverified_count(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    aggregate_sources(workspace)
    (workspace / "SUMMARY.md").write_text(
        _valid_report(workspace) + "\n[UNVERIFIED]\n", encoding="utf-8"
    )
    result = validate_final_report(workspace, FinalizationConfig())
    assert result.accepted
    assert result.audit["unverified_claim_markers"] == 1


def test_legacy_workspace_lazily_receives_compiled_contract(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    contract_path = workspace / "artifacts/SCOPE_PRIORITIZATION.json"
    assert not contract_path.exists()
    aggregate_sources(workspace)
    assert contract_path.is_file()
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["ranking_mode"] == "qualitative_fallback"
    assert contract["missing_data_policy"] == "unknown_not_zero"


def test_final_report_rejects_unknown_line_and_hidden_contradiction(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    aggregate_sources(workspace)
    report = _valid_report(workspace)
    (workspace / "SUMMARY.md").write_text(
        report.replace("RL-A-ONE", "RL-NOT-IN-REGISTRY"), encoding="utf-8"
    )
    assert any(
        "unknown Research Line" in error
        for error in validate_final_report(workspace, FinalizationConfig()).errors
    )

    registry_path = workspace / REGISTRY_PATH
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    paper_id = registry["papers"][0]["source_id"]
    registry["research_lines"][0]["paper_roles"]["contradictory"] = [paper_id]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    hidden = _valid_report(workspace).replace(
        f"so no broad consensus is claimed [{paper_id}].",
        "so no broad consensus is claimed.",
    ).replace(
        f"Framework and model-version differences prevent direct comparison [{paper_id}].",
        "Framework and model-version differences prevent direct comparison.",
    )
    (workspace / "SUMMARY.md").write_text(hidden, encoding="utf-8")
    assert any(
        "contradictory papers" in error
        for error in validate_final_report(workspace, FinalizationConfig()).errors
    )
    assert (workspace / FINAL_AUDIT_PATH).is_file()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda text: text.replace("## Section 4", "## Missing Section 4"), "heading"),
        (lambda text: text.replace("|---|---|---|---|---|---|", "no table"), "table"),
        (lambda text: text.replace("Consensus", "Agreement"), "consensus"),
        (lambda text: text.replace("[P", "[PUNKNOWN"), "unknown"),
        (lambda text: text.replace("[T", "[TUNKNOWN"), "unknown"),
        (lambda text: text + "\nTODO\n", "placeholder"),
        (
            lambda text: (
                text.replace(
                    "## Section 1 — Academic Terminology and Problem Boundaries",
                    "## TEMP",
                )
                .replace(
                    "## Section 2 — Background, Importance, and Broader Significance",
                    "## Section 1 — Academic Terminology and Problem Boundaries",
                )
                .replace(
                    "## TEMP",
                    "## Section 2 — Background, Importance, and Broader Significance",
                )
            ),
            "order",
        ),
    ],
)
def test_final_report_failures(tmp_path: Path, mutation, expected: str) -> None:
    workspace = _workspace(tmp_path)
    aggregate_sources(workspace)
    (workspace / "SUMMARY.md").write_text(
        mutation(_valid_report(workspace)), encoding="utf-8"
    )
    result = validate_final_report(workspace, FinalizationConfig())
    assert not result.accepted
    assert expected.lower() in " ".join(result.errors).lower()


def test_warning_completion_is_configurable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    aggregate_sources(workspace)
    report = _valid_report(workspace).replace("one paper", "42% coverage")
    (workspace / "SUMMARY.md").write_text(report, encoding="utf-8")
    assert validate_final_report(workspace, FinalizationConfig()).accepted
    strict = replace(FinalizationConfig(), allow_complete_with_warnings=False)
    assert not validate_final_report(workspace, strict).accepted


def test_duplicate_reference_and_mixed_language_warning(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    aggregate_sources(workspace)
    report = _valid_report(workspace)
    registry = json.loads((workspace / REGISTRY_PATH).read_text(encoding="utf-8"))
    paper_id = registry["papers"][0]["source_id"]
    report = report.replace(
        "## Delivery Status", f"- [{paper_id}] duplicate\n\n## Delivery Status"
    )
    (workspace / "SUMMARY.md").write_text(report, encoding="utf-8")
    assert not validate_final_report(workspace, FinalizationConfig()).accepted

    report = _valid_report(workspace).replace(
        "This evidence-backed discussion", "这是明显混合语言内容" * 20
    )
    (workspace / "SUMMARY.md").write_text(report, encoding="utf-8")
    result = validate_final_report(workspace, FinalizationConfig())
    assert any("English" in warning for warning in result.warnings)


def test_repair_tasks_force_existing_coordinator_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    seen = []

    def fake_coordinators(workspace, tasks, round_num, config, backend):
        seen.append((tasks, config.mode))
        return {task.topic_path: True for task in tasks}

    monkeypatch.setattr(
        "slrharness.orchestrator.run_topic_coordinators", fake_coordinators
    )
    results = _run_repair_tasks(
        tmp_path,
        [Task("topics/repair/gap", "GAP-1")],
        2,
        1,
        TopicExecutionConfig(),
        FakeBackend(),
    )
    assert results["topics/repair/gap"]
    assert seen[0][1] == TOPIC_COORDINATOR


def test_stable_repair_task_is_not_duplicated(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    gap = {
        "gap_id": "GAP-ABCDEF1234",
        "blocking": True,
        "recommended_task": "Collect missing experimental evidence",
    }
    audit = {"recommended_repairs": [gap]}
    assert ensure_stable_repair_tasks(workspace, audit) == ["GAP-ABCDEF1234"]
    assert ensure_stable_repair_tasks(workspace, audit) == []
    content = (workspace / "TASKS.md").read_text(encoding="utf-8")
    assert content.count("GAP-ABCDEF1234") == 1
    assert "[mode=topic_coordinator]" in content


def test_fake_backend_end_to_end_finalization_and_complete_resume(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _workspace(tmp_path)
    calls = []

    def fake_agent(workspace, agent_name, prompt, run_name, timeout, backend):
        calls.append((agent_name, run_name))
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
        workspace, 1, 1, 30, FakeBackend(), TopicExecutionConfig(), config
    )
    state = json.loads((workspace / "SLR_STATE.json").read_text(encoding="utf-8"))
    assert state["finalization"]["phase"] == "COMPLETE"
    assert calls == [("slr-manager", "manager-finalize-1")]
    assert run_finalization_pipeline(
        workspace, 1, 1, 30, FakeBackend(), TopicExecutionConfig(), config
    )
    assert calls == [("slr-manager", "manager-finalize-1")]


def test_failed_validation_retries_only_finalizer(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    calls = []

    def fake_agent(workspace, agent_name, prompt, run_name, timeout, backend):
        calls.append(run_name)
        if run_name.endswith("-1"):
            (workspace / "SUMMARY.md").write_text("# invalid draft", encoding="utf-8")
        else:
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
        workspace, 1, 1, 30, FakeBackend(), TopicExecutionConfig(), config
    )
    assert calls == ["manager-finalize-1", "manager-finalize-2"]
    state = json.loads((workspace / "SLR_STATE.json").read_text(encoding="utf-8"))
    assert state["finalization"]["phase"] == "COMPLETE"
    assert state["finalization"]["finalizer_attempt"] == 2
    assert (workspace / "artifacts/final_drafts/SUMMARY.before-attempt-2.md").is_file()


def test_invalid_final_report_never_marks_complete(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)

    def invalid_agent(workspace, agent_name, prompt, run_name, timeout, backend):
        (workspace / "SUMMARY.md").write_text("# invalid", encoding="utf-8")
        return True

    monkeypatch.setattr("slrharness.orchestrator.run_named_agent", invalid_agent)
    monkeypatch.setattr(
        "slrharness.orchestrator._commit_finalization_artifacts", lambda *args: None
    )
    config = replace(
        FinalizationConfig(), enable_prefinal_audit=False, finalizer_retries=0
    )
    assert not run_finalization_pipeline(
        workspace, 1, 1, 30, FakeBackend(), TopicExecutionConfig(), config
    )
    state = json.loads((workspace / "SLR_STATE.json").read_text(encoding="utf-8"))
    assert state["finalization"]["phase"] == "AWAITING_INTERVENTION"
    assert state["finalization"]["phase"] != "COMPLETE"
