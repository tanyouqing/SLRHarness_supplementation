from __future__ import annotations

import json
from pathlib import Path

from slrharness.report_sections import (
    PACKET_PATH,
    SECTION_PATHS,
    assemble_report,
    validate_section,
)
from slrharness.source_registry import REFERENCES_PATH


def _section_one(valid: bool = True) -> str:
    terms = "\n".join(f"| Term {i} | Definition {i} | [P1] |" for i in range(10))
    synonyms = "\n".join(f"| Term {i} | Alias {i} | related |" for i in range(6))
    taxonomy = "| RL-A | Line A | Group | included |"
    scope = "| Domain | included | excluded | borderline |"
    return f"""## Section 1 — Academic Terminology and Problem Boundaries
### Core Terminology
| Term | Definition | Source |
|---|---|---|
{terms if valid else ''}
### Synonyms and Related Terms
| Canonical | Related | Relationship |
|---|---|---|
{synonyms}
### Scope Boundaries
| Boundary | Included | Excluded | Borderline |
|---|---|---|---|
{scope}
### Field Taxonomy
| Research Line ID | Research Line | Group | Mapping |
|---|---|---|---|
{taxonomy}
"""


def test_section_one_rejects_missing_glossary(tmp_path: Path) -> None:
    path = tmp_path / SECTION_PATHS[0]
    path.parent.mkdir(parents=True)
    path.write_text(_section_one(False), encoding="utf-8")
    (tmp_path / PACKET_PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / PACKET_PATH).write_text(
        json.dumps({"ordered_research_lines": [{"line_id": "RL-A", "name": "Line A"}]}),
        encoding="utf-8",
    )
    valid, errors = validate_section(tmp_path, 1)
    assert not valid
    assert any("10" in error for error in errors)


def test_five_sections_are_assembled_in_fixed_order(tmp_path: Path) -> None:
    (tmp_path / PACKET_PATH).parent.mkdir(parents=True)
    (tmp_path / PACKET_PATH).write_text(
        json.dumps({"ordered_research_lines": [{"line_id": "RL-A", "name": "Line A"}]}),
        encoding="utf-8",
    )
    contents = [
        _section_one(),
        "## Section 2\n\n### Significance\n" + "Motivating problems and unresolved foundational questions. " * 8,
        "## Section 3\n\n### Organization and Prioritization Policy\nText.\n### Scope-Driven Research-Line Prioritization\n| A | B |\n|---|---|\n| RL-A | Core |\n### Findings by Research Line\n| A | B |\n|---|---|\n| Line A | Finding |\n" + "Evidence. " * 20,
        "## Section 4\n\n### Experimental Configuration Matrix\n| Model | Dataset |\n|---|---|\n| M | D |\n### Consensus\nConsensus.\n### Differences and Contradictions\nContradiction.\n### Comparability Limitations\n" + "Comparison. " * 20,
        "## Section 5\n\n### Future Directions\n| Research Question | Validation | Risk |\n|---|---|---|\n| Q | V | R |\n" + "Research question validation risk. " * 12,
    ]
    for relative, content in zip(SECTION_PATHS, contents, strict=True):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    references = tmp_path / REFERENCES_PATH
    references.parent.mkdir(parents=True, exist_ok=True)
    references.write_text("# References\n\n- [P1] Reference.\n", encoding="utf-8")
    output = assemble_report(tmp_path, "SUMMARY.md", {"planned_topics": 1})
    text = output.read_text(encoding="utf-8")
    assert text.index("Section 1") < text.index("Section 5") < text.index("References")
    assert "| planned_topics | 1 |" in text
