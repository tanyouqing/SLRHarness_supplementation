# ruff: noqa: E501
"""Offline research-line contract, ranking, and task-marker tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slrharness.config import load_config
from slrharness.orchestrator import parse_pending_tasks
from slrharness.prioritization import (
    PRIORITIZATION_PATH,
    PrioritizationConfig,
    compile_scope_prioritization,
    fallback_research_line_id,
    rank_research_lines,
    write_scope_prioritization,
)


def _scope(mode: str = "ordinal", factor_rows: str = "") -> str:
    factors = factor_rows or "| REL | Scope relevance | Directness | ordinal rubric | topic evidence |"
    return f"""# Scope

## Proposed Literature Organization
| Research Line ID | Research Line | Primary Group | Definition | Main Scope Question | Priority Tier |
|---|---|---|---|---|---|
| RL-EXTERNAL-MEMORY | External memory | Memory architecture | External stores | How is context retained? | Core |
| RL-REFLECTION | Reflection | Memory processing | Reflective summaries | How is experience consolidated? | Supporting |

## Proposed Evidence and Comparison Dimensions
| Dimension ID | Dimension | Description | Value Type | Applies To |
|---|---|---|---|---|
| DIM-LATENCY | Retrieval latency | Reported latency | numeric | research line |

## Research-Line Prioritization and Synthesis Policy
### Ranking Unit
research_line
### Primary Grouping
Group by memory mechanism.
### Ranking Mode
{mode}
### Priority Factors
| Factor ID | Factor | Meaning | Operational rubric | Evidence required |
|---|---|---|---|---|
{factors}
### Priority Tiers
Core, Supporting, Peripheral, Insufficient Evidence
### Primary Ordering
Core, Supporting, Peripheral, Insufficient Evidence
### Secondary Ordering
Scope relevance, then year descending
### Missing-Data Policy
Unknown values remain unknown, not zero.
### Contradictory-Evidence Policy
Always surface in Section 4.
### Paper Evidence Roles
anchor, representative, supporting, contradictory, peripheral, unassigned
"""


def test_ordinal_scope_compiles_stable_lines_and_dimensions() -> None:
    contract = compile_scope_prioritization(_scope())
    assert contract["compile_status"] == "COMPLETE"
    assert contract["ranking_unit"] == "research_line"
    assert [line["line_id"] for line in contract["research_lines"]] == [
        "RL-EXTERNAL-MEMORY",
        "RL-REFLECTION",
    ]
    assert contract["comparison_dimensions"][0]["dimension_id"] == "DIM-LATENCY"


def test_compound_ranking_heading_prefers_its_research_line_table() -> None:
    scope = _scope().replace(
        "| Research Line ID | Research Line | Primary Group | Definition | "
        "Main Scope Question | Priority Tier |\n"
        "|---|---|---|---|---|---|\n"
        "| RL-EXTERNAL-MEMORY | External memory | Memory architecture | "
        "External stores | How is context retained? | Core |\n"
        "| RL-REFLECTION | Reflection | Memory processing | Reflective "
        "summaries | How is experience consolidated? | Supporting |",
        "| Group | Description |\n|---|---|\n| Memory | Group summary only |",
    ).replace(
        "### Ranking Unit\nresearch_line",
        "### 10.1 Ranking Unit and Primary Grouping\n"
        "| Line ID | Name | Group | Expected tier |\n"
        "|---|---|---|---|\n"
        "| `RL-ONE` | First line | Memory | Core |\n"
        "| `RL-TWO` | Second line | Memory | Supporting |",
    )
    contract = compile_scope_prioritization(scope)
    assert [line["line_id"] for line in contract["research_lines"]] == [
        "RL-ONE",
        "RL-TWO",
    ]


def test_policy_at_a_glance_and_decorated_headers_compile() -> None:
    scope = _scope().replace(
        "### Ranking Unit\nresearch_line",
        "### 10.0 Prioritization Policy at a Glance\n"
        "| Contract element | Proposed value |\n|---|---|\n"
        "| **Ranking Unit** | the research line (`RL-<NAME>`) |\n"
        "| **Primary Grouping** | modification locus |\n"
        "| **Ranking Mode** | `ordinal_tiers` (default) |\n"
        "| **Priority Tiers** | Core → Supporting → Peripheral → Insufficient Evidence |\n"
        "| **Primary Ordering** | tier |\n"
        "| **Secondary Ordering (tiebreakers)** | year desc |\n"
        "| **Missing-Data Policy** | unknown is not zero |\n\n"
        "### 10.1 Ranking Unit\nresearch_line",
    ).replace(
        "| Factor ID | Factor | Meaning | Operational rubric | Evidence required |",
        "| ID | Factor | Meaning | Operational rubric (low → high) | Evidence required |",
    ).replace(
        "| Research Line ID | Research Line | Primary Group | Definition | Main Scope Question | Priority Tier |",
        "| Line ID | Name | Group | One-line definition | Main Scope Question | Expected tier (provisional) |",
    )
    contract = compile_scope_prioritization(scope)
    assert contract["compile_status"] == "COMPLETE"
    assert contract["ranking_mode"] == "ordinal"
    assert len(contract["research_lines"]) == 2
    assert all(line["definition"] for line in contract["research_lines"])
    assert contract["factors"][0]["factor_id"] == "REL"
    assert contract["factors"][0]["rubric"] == "ordinal rubric"


def test_legacy_scope_degrades_without_zero_values() -> None:
    contract = compile_scope_prioritization(
        "## Ranking & Grouping Criteria\nGroup topics by mechanism and order qualitatively."
    )
    assert contract["compile_status"] == "PARTIAL"
    assert contract["ranking_mode"] == "qualitative_fallback"
    assert contract["missing_data_policy"] == "unknown_not_zero"


def test_research_line_fallback_id_is_stable() -> None:
    assert fallback_research_line_id("topics/memory/external") == "RL-MEMORY-EXTERNAL"
    assert fallback_research_line_id("topics/memory/external") == fallback_research_line_id(
        "topics/memory/external"
    )


def test_weighted_scope_and_malformed_weights() -> None:
    rows = (
        "| REL | Scope relevance | Directness | 0-1 rubric | evidence | 0.6 | 0 | 1 |\n"
        "| EVD | Evidence | Strength | 0-1 rubric | notes | 0.4 | 0 | 1 |"
    )
    weighted = _scope("weighted_composite", rows).replace(
        "| Factor ID | Factor | Meaning | Operational rubric | Evidence required |",
        "| Factor ID | Factor | Meaning | Operational rubric | Evidence required | Weight | Minimum | Maximum |",
    ).replace(
        "|---|---|---|---|---|\n" + rows,
        "|---|---|---|---|---|---|---|---|\n" + rows,
        1,
    )
    complete = compile_scope_prioritization(weighted)
    assert complete["compile_status"] == "COMPLETE"
    malformed = compile_scope_prioritization(_scope("weighted_composite"))
    assert malformed["compile_status"] == "PARTIAL"
    assert any("weights" in warning for warning in malformed["warnings"])


def test_numeric_and_weighted_ranking_leave_missing_unranked() -> None:
    numeric = {
        "ranking_mode": "numeric_dimension",
        "numeric_dimension": "latency",
        "numeric_direction": "ascending",
    }
    lines = [
        {"line_id": "RL-A", "comparison_values": {"latency": 3}},
        {"line_id": "RL-B", "comparison_values": {}},
        {"line_id": "RL-C", "comparison_values": {"latency": 1}},
    ]
    ranked = rank_research_lines(numeric, lines)
    assert ranked["ordered_line_ids"] == ["RL-C", "RL-A"]
    assert ranked["unranked_line_ids"] == ["RL-B"]

    weighted = {
        "ranking_mode": "weighted_composite",
        "factors": [
            {
                "factor_id": "REL",
                "weight": 0.5,
                "minimum": 0,
                "maximum": 1,
                "rubric": "0-1",
            },
            {
                "factor_id": "EVD",
                "weight": 0.5,
                "minimum": 0,
                "maximum": 1,
                "rubric": "0-1",
            },
        ],
    }
    result = rank_research_lines(
        weighted,
        [
            {"line_id": "RL-A", "factor_assessments": {"REL": 1, "EVD": 0.5}},
            {"line_id": "RL-B", "factor_assessments": {"REL": 1}},
        ],
    )
    assert result["computed_scores"]["RL-A"] == pytest.approx(0.75)
    assert result["unranked_line_ids"] == ["RL-B"]


def test_conflicting_ordinal_assessments_are_preserved_for_finalizer() -> None:
    contract = {
        "ranking_mode": "ordinal",
        "primary_ordering": ["Core", "Supporting"],
    }
    result = rank_research_lines(
        contract,
        [
            {
                "line_id": "RL-A",
                "proposed_tiers": [
                    {"topic_path": "topics/a", "tier": "Core"},
                    {"topic_path": "topics/b", "tier": "Supporting"},
                ],
            }
        ],
    )
    assert result["ordered_line_ids"] == []
    assert result["unranked_line_ids"] == ["RL-A"]
    assert "finalizer calibration" in result["warnings"][0]


def test_approved_secondary_year_order_breaks_ties_deterministically() -> None:
    contract = {
        "ranking_mode": "ordinal",
        "primary_ordering": ["Core", "Supporting"],
        "secondary_ordering": "year descending, then name alphabetically",
    }
    lines = [
        {
            "line_id": "RL-OLDER",
            "name": "Alpha",
            "priority_tier": "Core",
            "latest_year": 2022,
        },
        {
            "line_id": "RL-NEWER",
            "name": "Zulu",
            "priority_tier": "Core",
            "latest_year": 2025,
        },
    ]
    assert rank_research_lines(contract, lines)["ordered_line_ids"] == [
        "RL-NEWER",
        "RL-OLDER",
    ]


def test_contract_write_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "SCOPE.md").write_text(_scope(), encoding="utf-8")
    first = write_scope_prioritization(tmp_path)
    second = write_scope_prioritization(tmp_path)
    persisted = json.loads((tmp_path / PRIORITIZATION_PATH).read_text(encoding="utf-8"))
    assert first == second == persisted


def test_task_markers_are_order_insensitive_and_legacy_gets_fallback(tmp_path: Path) -> None:
    (tmp_path / "SCOPE.md").write_text(_scope(), encoding="utf-8")
    write_scope_prioritization(tmp_path)
    tasks = tmp_path / "TASKS.md"
    tasks.write_text(
        "# Tasks\n"
        "- [ ] topics/a/one -- [line=RL-EXTERNAL-MEMORY] [mode=topic_coordinator] first\n"
        "- [ ] topics/a/two -- [mode=topic_coordinator] [line=RL-REFLECTION] second\n"
        "- [ ] topics/a/legacy -- legacy\n",
        encoding="utf-8",
    )
    parsed = parse_pending_tasks(tasks)
    assert [task.research_line_id for task in parsed] == [
        "RL-EXTERNAL-MEMORY",
        "RL-REFLECTION",
        "RL-A-LEGACY",
    ]
    assert all(task.execution_mode == "topic_coordinator" for task in parsed[:2])


def test_unknown_line_marker_is_retained_with_warning(tmp_path: Path) -> None:
    (tmp_path / "SCOPE.md").write_text(_scope(), encoding="utf-8")
    write_scope_prioritization(tmp_path)
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("- [ ] topics/a/x -- [line=RL-EMERGENT] inspect\n", encoding="utf-8")
    task = parse_pending_tasks(tasks)[0]
    assert task.research_line_id == "RL-EMERGENT"
    assert task.prioritization_warnings


def test_prioritization_config_defaults_and_invalid_enums(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"schema_version":"1.0"}', encoding="utf-8")
    assert load_config(config_path).prioritization == PrioritizationConfig()


def test_transposed_research_line_table_and_prose_factors_compile() -> None:
    scope = """# Scope

## Research-Line Prioritization and Synthesis Policy
### 10.1 Ranking Unit
research_line
### 10.3 Proposed Research Lines
| Field | `RL-MEM-WRITE` | `RL-MEM-RETRIEVE` |
|---|---|---|
| **Primary Group** | G1 | G1 |
| **Definition** | Write path | Retrieve path |
| **Base tier (scope gate)** | Core | Supporting |
### 10.5 Priority Tiers (ordinal)
Core, then Supporting, then Peripheral, then Insufficient Evidence
### 10.6 Priority Factors
**PF1 — Scope centrality.**
- `High`: directly about agent memory.
- `Medium`: adjacent component.
- `Low`: boundary only.
- `Unknown`: not assessed.
**PF2 — Evidence strength.**
- `High`: multiple evaluated systems.
- `Medium`: some evaluation.
- `Low`: none.
- `Unknown`: not collected.
### Missing-Data Policy
Unknown is never zero.
"""
    contract = compile_scope_prioritization(scope)
    assert contract["compile_status"] == "COMPLETE"
    assert [line["line_id"] for line in contract["research_lines"]] == [
        "RL-MEM-WRITE",
        "RL-MEM-RETRIEVE",
    ]
    assert contract["research_lines"][0]["group"] == "G1"
    assert {factor["factor_id"] for factor in contract["factors"]} >= {"PF1", "PF2"}
    assert contract["primary_ordering"] == [
        "Core",
        "Supporting",
        "Peripheral",
        "Insufficient Evidence",
    ]


def test_inline_rl_ids_recovered_when_no_table() -> None:
    scope = """## Research-Line Prioritization and Synthesis Policy
### Ranking Unit
The ranking unit is the research line.
### Ranking Mode
ordinal
### Priority Tiers
Core / Supporting / Peripheral / Insufficient Evidence
Lines discussed: RL-ALPHA-MEMORY and RL-BETA-TOOLS.
"""
    contract = compile_scope_prioritization(scope)
    ids = [line["line_id"] for line in contract["research_lines"]]
    assert "RL-ALPHA-MEMORY" in ids
    assert "RL-BETA-TOOLS" in ids

    with pytest.raises(ValueError, match="default_mode"):
        PrioritizationConfig(default_mode="invented")
