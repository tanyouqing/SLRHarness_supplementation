"""Program-owned report packets, section validation, and deterministic assembly."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from slrharness.contracts import SCHEMA_VERSION, atomic_write_json, atomic_write_text
from slrharness.control_plane import current_issues
from slrharness.prioritization import load_scope_prioritization
from slrharness.source_registry import REFERENCES_PATH, REGISTRY_PATH

SECTION_HEADINGS = (
    "## Section 1 — Academic Terminology and Problem Boundaries",
    "## Section 2 — Background, Importance, and Broader Significance",
    "## Section 3 — Existing Research: Motivations, Methodologies, and Findings",
    "## Section 4 — Research Landscape: Consensus, Differences, and Experimental Practice",
    "## Section 5 — Evidence-Backed Research Opportunities",
)
SECTION_PATHS = tuple(f"sections/0{number}-{slug}.md" for number, slug in (
    (1, "terminology-scope"),
    (2, "importance"),
    (3, "existing-research"),
    (4, "consensus-differences"),
    (5, "future-directions"),
))
PACKET_PATH = "artifacts/REPORT_PACKET.json"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_report_packet(
    workspace: Path, delivery_statistics: dict[str, Any]
) -> dict[str, Any]:
    """Freeze the exact shared input supplied to all five section writers."""
    state = _load(workspace / "SLR_STATE.json") or {}
    registry = _load(workspace / REGISTRY_PATH) or {}
    scope = _load(workspace / "artifacts/SCOPE_CONTRACT.json") or {}
    tasks = (state.get("control") or {}).get("topic_tasks") or {}
    syntheses = [
        str(item.get("topic_path")) + ".md"
        for item in tasks.values()
        if isinstance(item, dict)
        and item.get("status") in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}
    ]
    packet = {
        "schema_version": SCHEMA_VERSION,
        "producer": "slrharness",
        "approved_scope_contract": scope,
        "prioritization": load_scope_prioritization(workspace),
        "ordered_research_lines": registry.get("research_lines", []),
        "topic_syntheses": sorted(syntheses),
        "source_registry_path": REGISTRY_PATH,
        "issues": list(current_issues(workspace).values()),
        "known_contradictions": [
            paper.get("source_id")
            for paper in registry.get("papers", [])
            if "contradictory" in (paper.get("evidence_roles") or {}).values()
        ],
        "unresolved_gaps": [
            issue
            for issue in current_issues(workspace).values()
            if issue.get("status") != "verified_closed"
        ],
        "delivery_statistics": delivery_statistics,
    }
    atomic_write_json(workspace / PACKET_PATH, packet)
    return packet


def build_section_prompt(workspace: Path, section_number: int) -> str:
    if section_number not in range(1, 6):
        raise ValueError("section number must be 1..5")
    dependencies = {
        1: [], 2: [], 3: [],
        4: [SECTION_PATHS[2]],
        5: [SECTION_PATHS[2], SECTION_PATHS[3]],
    }[section_number]
    return f"""Write only report Section {section_number} in English.
CANONICAL PACKET: {workspace / PACKET_PATH}
SECTION TEMPLATE: {workspace / '.claude/templates' / f'report-section-0{section_number}.md'}
TOPIC SYNTHESIS INPUTS: read only the paths listed in the packet
PRIOR SECTIONS: {json.dumps(dependencies)}
OUTPUT: {workspace / SECTION_PATHS[section_number - 1]}

Use only canonical research-line names and source IDs from the packet/registry.
Do not search, calculate counts, write references, repeat global disclaimers,
or modify any other file. Topic-specific limitations belong in the content.
Program-owned IDs, paths, counts, status, ordering, metadata, and table facts
must not be invented or changed. Follow the template closely; prefer the
template headings, but equivalent wording is acceptable. Print DONE.
"""


def _tables(text: str) -> list[list[list[str]]]:
    groups: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            current.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [group for group in groups if len(group) >= 3]


def _has_any(lower: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in lower for phrase in phrases)


def validate_section(workspace: Path, section_number: int) -> tuple[bool, list[str]]:
    """Return (usable_for_assembly, hard_errors).

    Style/phrase misses do not fail a substantive draft. Only empty/short
    sections are hard failures.
    """
    path = workspace / SECTION_PATHS[section_number - 1]
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    errors: list[str] = []
    if len(text.strip()) < 200:
        errors.append("section is missing or too short")
        return False, errors
    lower = text.lower()
    tables = _tables(text)
    if section_number == 1:
        # Prefer template headings, accept close variants.
        heading_ok = (
            _has_any(lower, ("core terminology", "terminology", "key terms"))
            and _has_any(lower, ("synonym", "related term", "variants"))
            and _has_any(lower, ("boundar", "in scope", "included"))
            and _has_any(lower, ("taxonomy", "research line", "research-line"))
        )
        if not heading_ok and len(tables) < 1 and len(text) < 800:
            errors.append("Section 1 is too thin to stand in for terminology/boundaries")
    elif section_number == 2:
        if not _has_any(lower, ("motivat", "significan", "importance", "why")):
            if len(text) < 600:
                errors.append("Section 2 is too thin for background/significance")
    elif section_number == 3:
        if not tables and len(text) < 800:
            errors.append("Section 3 lacks comparison structure and is too short")
    elif section_number == 4:
        if not _has_any(
            lower, ("consensus", "difference", "contradiction", "disagree", "inconsisten")
        ) and len(text) < 600:
            errors.append("Section 4 is too thin for consensus/differences")
    else:
        if not tables and len(text) < 600:
            errors.append("Section 5 lacks a direction table and is too short")
    return not errors, errors


def _section_or_stub(
    workspace: Path, number: int, heading: str
) -> tuple[str, list[str]]:
    valid, errors = validate_section(workspace, number)
    path = workspace / SECTION_PATHS[number - 1]
    raw = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not valid:
        note = "; ".join(errors) if errors else "empty"
        body = (
            f"{heading}\n\n"
            f"_Section {number} was not fully validated ({note}). "
            "Partial or missing content is disclosed here so the five-section "
            "structure remains complete._\n"
        )
        return body, errors
    text = raw.strip()
    text = re.sub(r"^#{1,2}\s+.*?\r?\n", "", text, count=1)
    return heading + "\n\n" + text.strip(), []


def assemble_report(
    workspace: Path,
    output: str,
    delivery_statistics: dict[str, Any],
) -> Path:
    """Assemble the canonical five-section report.

    Substantive section drafts are kept even when wording diverges from the
    template. Empty sections become disclosure stubs so assembly does not
    abort the whole pipeline.
    """
    bodies: list[str] = []
    section_issues: list[str] = []
    for number, heading in enumerate(SECTION_HEADINGS, start=1):
        body, errors = _section_or_stub(workspace, number, heading)
        if errors:
            section_issues.extend(f"section {number}: {item}" for item in errors)
        bodies.append(body)
    if len(bodies) != 5:
        raise ValueError("report assembly requires five section bodies")
    references_path = workspace / REFERENCES_PATH
    references = (
        references_path.read_text(encoding="utf-8")
        if references_path.is_file()
        else "_No references registry was available._\n"
    )
    references = re.sub(r"^#\s+References\s*", "", references, count=1).strip()
    issues = [
        issue for issue in current_issues(workspace).values()
        if issue.get("status") != "verified_closed"
    ]
    limitation_rows = [
        f"| {issue.get('kind')}:{issue.get('field')} | {issue.get('target')} | "
        f"{issue.get('severity')} | {issue.get('status')} |"
        for issue in issues
    ] or ["| No open ledger issue | Global | None recorded | closed |"]
    if section_issues:
        limitation_rows.extend(
            f"| report-section | {item.split(':', 1)[0]} | format | disclosed |"
            for item in section_issues
        )
    delivery_rows = [f"| {key} | {value} |" for key, value in delivery_statistics.items()]
    suffix = "\n\n".join(
        (
            "## Coverage and Limitations\n\n### Prioritization Limitations\n\n"
            "Research-line ordering controls narrative emphasis and is not a paper-quality ranking. "
            "Missing values are unknown rather than zero.\n\n"
            "## Limitations and Provenance\n\n| Issue | Affected Scope | Impact | Status |\n|---|---|---|---|\n"
            + "\n".join(limitation_rows),
            "## Sources and Provenance\n\nAcademic notes and explicitly non-peer-reviewed technical sources were compiled through the program-owned source registry. Provider outages and access limits are reflected in the issue ledger.",
            "## References\n\n" + references,
            "## Delivery Status\n\n| Field | Value |\n|---|---|\n" + "\n".join(delivery_rows),
        )
    )
    report = "# Systematic Literature Review\n\n" + "\n\n".join(bodies) + "\n\n" + suffix + "\n"
    target = workspace / output
    atomic_write_text(target, report)
    return target
