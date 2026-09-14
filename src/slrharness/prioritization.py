"""Research-line prioritization contracts compiled from approved Markdown scope."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slrharness.contracts import atomic_write_json

PRIORITIZATION_PATH = "artifacts/SCOPE_PRIORITIZATION.json"
PRIORITIZATION_SCHEMA_VERSION = "1.0"
VALID_RANKING_MODES = {
    "ordinal",
    "numeric_dimension",
    "weighted_composite",
    "qualitative_fallback",
}
PAPER_ROLES = (
    "anchor",
    "representative",
    "supporting",
    "contradictory",
    "peripheral",
    "unassigned",
)
PRIORITY_TIERS = ("Core", "Supporting", "Peripheral", "Insufficient Evidence")
VALID_LINE_ID = re.compile(r"^RL-[A-Z0-9]+(?:-[A-Z0-9]+)*$")


@dataclass(frozen=True)
class PrioritizationConfig:
    enabled: bool = True
    ranking_unit: str = "research_line"
    default_mode: str = "ordinal"
    missing_value_policy: str = "unknown_not_zero"
    incomplete_topic_assessment_is_fatal: bool = False
    missing_paper_role_is_fatal: bool = False
    require_final_prioritization_section: bool = True
    surface_contradictory_evidence: bool = True
    allow_qualitative_fallback: bool = True

    def __post_init__(self) -> None:
        if self.ranking_unit != "research_line":
            raise ValueError("prioritization.ranking_unit must be 'research_line'")
        if self.default_mode not in {
            "ordinal",
            "numeric_dimension",
            "weighted_composite",
        }:
            raise ValueError("prioritization.default_mode is invalid")
        if self.missing_value_policy != "unknown_not_zero":
            raise ValueError(
                "prioritization.missing_value_policy must be 'unknown_not_zero'"
            )


def stable_research_line_id(value: str) -> str:
    """Return a readable, deterministic Research Line ID."""
    raw = value.strip().replace("\\", "/")
    if raw.lower().startswith("topics/"):
        raw = raw[7:]
    slug = re.sub(r"[^A-Z0-9]+", "-", raw.upper()).strip("-")
    if slug.startswith("RL-"):
        candidate = slug
    else:
        candidate = f"RL-{slug}" if slug else ""
    if VALID_LINE_ID.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()
    return f"RL-LINE-{digest}"


def fallback_research_line_id(topic_path: str) -> str:
    return stable_research_line_id(topic_path)


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+(?:\d+[.)]\s*)?{re.escape(heading)}\s*$\r?\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _subsection(text: str, heading: str) -> str:
    match = re.search(
        rf"^###\s+(?:[\d.)]+\s+)?{re.escape(heading)}"
        rf"(?:\s*\([^)]*\))?\s*$\r?\n(.*?)(?=^##{{2,3}}\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _subsection_with_heading_label(text: str, heading: str) -> str:
    """Read a subsection whose compound heading contains the requested label."""
    match = re.search(
        rf"^###\s+(?:[\d.)]+\s+)?[^\n]*\b{re.escape(heading)}\b[^\n]*$"
        rf"\r?\n(.*?)(?=^##{{2,3}}\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _table(section: str) -> list[dict[str, str]]:
    rows = [
        line.strip()
        for line in section.splitlines()
        if line.strip().startswith("|")
    ]
    if len(rows) < 2:
        return []
    headers = [_clean_markdown(cell) for cell in rows[0].strip("|").split("|")]
    output: list[dict[str, str]] = []
    for row in rows[1:]:
        cells = [_clean_markdown(cell) for cell in row.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) != len(headers):
            continue
        output.append(dict(zip(headers, cells, strict=True)))
    return output


def _table_groups(section: str) -> list[list[list[str]]]:
    groups: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in section.splitlines():
        if line.strip().startswith("|"):
            current.append(
                [_clean_markdown(cell) for cell in line.strip().strip("|").split("|")]
            )
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _transposed_line_rows(section: str) -> list[dict[str, str]]:
    """Parse wide tables whose columns are research-line IDs."""
    rows: list[dict[str, str]] = []
    for group in _table_groups(section):
        if len(group) < 3:
            continue
        header = group[0]
        line_ids = [
            (index, cell.upper())
            for index, cell in enumerate(header[1:], start=1)
            if VALID_LINE_ID.fullmatch(cell.upper())
        ]
        if not line_ids:
            continue
        fields: dict[str, dict[str, str]] = {}
        for line in group[1:]:
            if len(line) < 2:
                continue
            key = _label(line[0])
            for col, line_id in line_ids:
                if col < len(line):
                    fields.setdefault(key, {})[line_id] = line[col]
        for _, line_id in line_ids:
            bound_fields = fields
            bound_line_id = line_id

            def cell(*keys: str, _fields=bound_fields, _line_id=bound_line_id) -> str:
                for key in keys:
                    value = _fields.get(_label(key), {}).get(_line_id)
                    if value:
                        return value
                return ""

            rows.append(
                {
                    "Line ID": line_id,
                    "Research Line": cell("Definition", "Name") or line_id,
                    "Primary Group": cell("Primary Group", "Group"),
                    "Definition": cell("Definition"),
                    "Priority Tier": cell(
                        "Base tier scope gate",
                        "Expected tier",
                        "Priority Tier",
                        "Tier",
                    ),
                }
            )
    return rows


def _prose_factors(policy: str) -> list[dict[str, Any]]:
    """Parse PF bullets such as '**PF1 — Scope centrality.** ...'."""
    section = (
        _subsection(policy, "Priority Factors")
        or _subsection_with_heading_label(policy, "Priority Factors")
        or policy
    )
    factors: list[dict[str, Any]] = []
    for match in re.finditer(
        r"\*\*(PF\d+)\s*[—–-]\s*([^.*]+?)\.?\*\*\s*(.*?)(?=\*\*PF\d+|\Z)",
        section,
        re.DOTALL | re.IGNORECASE,
    ):
        factor_id, name, body = match.group(1), match.group(2).strip(), match.group(3)
        levels = re.findall(
            r"`(High|Medium|Low|Unknown)`\s*:\s*([^\n]+)",
            body,
            re.IGNORECASE,
        )
        rubric = "; ".join(f"{level}: {desc.strip()}" for level, desc in levels)
        factors.append(
            {
                "factor_id": factor_id.upper(),
                "name": name,
                "meaning": name,
                "rubric": rubric or name,
                "evidence_required": "",
            }
        )
    return factors


def _rl_ids_from_text(text: str) -> list[str]:
    seen: list[str] = []
    for match in re.finditer(r"\b(RL-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b", text):
        value = match.group(1).upper()
        if value not in seen:
            seen.append(value)
    return seen


def _column(row: dict[str, str], *names: str) -> str:
    normalized = {_label(key): value for key, value in row.items()}
    wanted_labels = [_label(name) for name in names]
    for wanted in wanted_labels:
        if wanted in normalized:
            return _clean_markdown(normalized[wanted])
    for wanted in wanted_labels:
        value = next(
            (
                candidate
                for key, candidate in normalized.items()
                if key.startswith(wanted + " ")
                and not (key.endswith(" id") and not wanted.endswith(" id"))
            ),
            None,
        )
        if value is not None:
            return _clean_markdown(value)
    return ""


def _clean_markdown(value: Any) -> str:
    text = re.sub(r"[`*]+", "", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _label(value: Any) -> str:
    text = _clean_markdown(value).lower()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    text = text.replace("one-line", "").replace("one line", "")
    text = text.replace("expected tier", "priority tier")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _contract_table(policy: str) -> dict[str, str]:
    section = _subsection_with_heading_label(
        policy, "Prioritization Policy at a Glance"
    )
    rows = _table(section)
    return {
        _label(_column(row, "Contract element", "Element")): _column(
            row, "Proposed value", "Value"
        )
        for row in rows
        if _column(row, "Contract element", "Element")
    }


def _contract_value(contract: dict[str, str], label: str) -> str:
    wanted = _label(label)
    return next(
        (
            value
            for key, value in contract.items()
            if key == wanted or key.startswith(wanted)
        ),
        "",
    )


def _ranking_mode(value: str) -> str:
    normalized = _label(value).replace(" ", "_")
    for token, canonical in (
        ("ordinal_tiers", "ordinal"),
        ("ordinal", "ordinal"),
        ("numeric_dimension", "numeric_dimension"),
        ("weighted_composite", "weighted_composite"),
        ("qualitative_fallback", "qualitative_fallback"),
    ):
        if normalized.startswith(token):
            return canonical
    return ""


def _first_value(section: str) -> str:
    for line in section.splitlines():
        value = line.strip().strip("`*")
        if value and not value.startswith(("#", "<")):
            return value
    return ""


def compile_scope_prioritization(
    scope_text: str, source: str = "SCOPE.md"
) -> dict[str, Any]:
    """Compile a tolerant, deterministic contract from a human-readable scope."""
    warnings: list[str] = []
    policy = _section(scope_text, "Research-Line Prioritization and Synthesis Policy")
    legacy = _section(scope_text, "Ranking & Grouping Criteria")
    organization = _section(scope_text, "Proposed Literature Organization")
    comparison = _section(scope_text, "Proposed Evidence and Comparison Dimensions")
    if not comparison:
        comparison = _section(scope_text, "Comparison Dimensions")

    if not policy:
        warnings.append(
            "approved scope has no Research-Line Prioritization and Synthesis Policy; "
            "using qualitative fallback"
        )
        policy = legacy
    contract_table = _contract_table(policy)
    ranking_unit = _contract_value(contract_table, "Ranking Unit") or _first_value(
        _subsection(policy, "Ranking Unit")
    )
    ranking_unit = (
        "research_line" if "research line" in _label(ranking_unit) else ranking_unit
    )
    if not ranking_unit or "contract name" in _label(ranking_unit):
        unit_body = _subsection(policy, "Ranking Unit") or _subsection_with_heading_label(
            policy, "Ranking Unit"
        )
        if "research line" in unit_body.lower() or not ranking_unit:
            ranking_unit = "research_line"
    if not ranking_unit:
        ranking_unit = "research_line"
    primary_grouping = _contract_value(
        contract_table, "Primary Grouping"
    ) or _first_value(_subsection(policy, "Primary Grouping"))
    if not primary_grouping and legacy:
        primary_grouping = "Use the approved legacy Ranking & Grouping Criteria."
        warnings.append("legacy grouping was interpreted at research-line/topic level")
    ranking_mode = _ranking_mode(
        _contract_value(contract_table, "Ranking Mode")
        or _first_value(_subsection(policy, "Ranking Mode"))
    )
    if not ranking_mode:
        mode_body = _subsection(policy, "Ranking Mode") or _subsection_with_heading_label(
            policy, "Ranking Mode"
        )
        ranking_mode = _ranking_mode(mode_body) or _ranking_mode(policy)
    if ranking_mode not in VALID_RANKING_MODES:
        ranking_mode = "qualitative_fallback"
        warnings.append(
            "ranking mode is missing or unsupported; using qualitative fallback"
        )

    # Prefer the dedicated RL table (often under "### Ranking Unit").
    # Section 5 organization tables may list groups only and must not win.
    ranking_unit_section = _subsection(policy, "Ranking Unit") or (
        _subsection_with_heading_label(policy, "Ranking Unit")
    )
    line_rows: list[dict[str, str]] = []
    for candidate in (ranking_unit_section, organization, policy):
        rows = _table(candidate)
        if any(
            _column(row, "Research Line ID", "Line ID", "RL ID", "RL- ID")
            or _column(row, "Research Line", "Line")
            for row in rows
        ):
            line_rows = rows
            break
    if not line_rows:
        for candidate in (policy, organization, ranking_unit_section, scope_text):
            rows = _transposed_line_rows(candidate)
            if rows:
                line_rows = rows
                break
    research_lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_line_ids = False
    for row in line_rows:
        name = _column(row, "Research Line", "Line", "Name", "Method Family")
        raw_id = _column(row, "Research Line ID", "Line ID", "RL ID", "RL- ID")
        if not name and not raw_id:
            continue
        line_id = (
            raw_id.upper()
            if VALID_LINE_ID.fullmatch(raw_id.upper())
            else stable_research_line_id(name or raw_id)
        )
        line_warnings: list[str] = []
        if not raw_id or raw_id.upper() != line_id:
            line_warnings.append("stable fallback ID generated from research-line name")
            warnings.append(f"{name or line_id}: missing or invalid Research Line ID")
        if line_id in seen:
            duplicate_line_ids = True
            warnings.append(f"duplicate Research Line ID ignored: {line_id}")
            continue
        seen.add(line_id)
        research_lines.append(
            {
                "line_id": line_id,
                "name": name or line_id,
                "group": _column(row, "Primary Group", "Group"),
                "definition": _column(row, "Definition"),
                "scope_question": _column(
                    row, "Main Scope Question", "Scope Question"
                ),
                "priority_tier": _column(
                    row, "Priority Tier", "Tier", "Expected tier"
                )
                or None,
                "warnings": line_warnings,
            }
        )

    if not research_lines:
        # Last resort: recover explicit RL- IDs so scope is not blocked by table shape.
        for line_id in _rl_ids_from_text(policy or scope_text):
            if line_id in seen:
                continue
            seen.add(line_id)
            research_lines.append(
                {
                    "line_id": line_id,
                    "name": line_id,
                    "group": "",
                    "definition": "",
                    "scope_question": "",
                    "priority_tier": None,
                    "warnings": ["recovered from inline RL- identifier"],
                }
            )
            warnings.append(f"{line_id}: recovered from inline RL- identifier")

    # Soft defaults keep PARTIAL contracts usable at the scope gate.
    for line in research_lines:
        if not line.get("group"):
            line["group"] = "UNSPECIFIED"
            line.setdefault("warnings", []).append("group defaulted to UNSPECIFIED")
        if not line.get("definition"):
            line["definition"] = str(line.get("name") or line["line_id"])
            line.setdefault("warnings", []).append("definition defaulted from name")

    dimensions = []
    for row in _table(comparison):
        name = _column(row, "Dimension", "Comparison Dimension")
        if name:
            dimensions.append(
                {
                    "dimension_id": _column(row, "Dimension ID")
                    or stable_research_line_id(name).replace("RL-", "DIM-", 1),
                    "name": name,
                    "description": _column(row, "Description"),
                    "value_type": _column(row, "Value Type", "Type") or "text",
                    "applies_to": _column(row, "Applies To") or "research_line",
                }
            )

    factors = []
    factor_section = _subsection(policy, "Priority Factors") or (
        _subsection_with_heading_label(policy, "Priority Factors")
    )
    for row in _table(factor_section):
        name = _column(row, "Factor", "Name")
        if not name:
            continue
        factor: dict[str, Any] = {
            "factor_id": (
                _column(row, "Factor ID", "ID")
                or stable_research_line_id(name).replace("RL-", "F-", 1)
            ).upper(),
            "name": name,
            "meaning": _column(row, "Meaning"),
            "rubric": _column(row, "Operational rubric", "Rubric"),
            "evidence_required": _column(row, "Evidence required"),
        }
        for key, aliases in {
            "weight": ("Weight",),
            "minimum": ("Minimum", "Min"),
            "maximum": ("Maximum", "Max"),
        }.items():
            raw = _column(row, *aliases)
            if raw:
                try:
                    value = float(raw)
                    if math.isfinite(value):
                        factor[key] = value
                    else:
                        raise ValueError
                except ValueError:
                    warnings.append(f"factor {factor['factor_id']} has invalid {key}")
        factors.append(factor)

    if not factors:
        factors = _prose_factors(policy)
        if factors:
            warnings.append("priority factors parsed from prose definitions")
    if not factors:
        warnings.append("priority factor rubric is incomplete")

    if ranking_mode == "weighted_composite":
        weights = [factor.get("weight") for factor in factors]
        if not factors or any(not isinstance(weight, float) for weight in weights):
            warnings.append(
                "weighted_composite factors do not define parseable weights"
            )
        elif not math.isclose(sum(weights), 1.0, abs_tol=0.01):
            warnings.append("weighted_composite weights do not sum to approximately 1")
        if any(
            "minimum" not in factor
            or "maximum" not in factor
            or not str(factor.get("rubric") or "").strip()
            for factor in factors
        ):
            warnings.append(
                "weighted_composite factors require ranges and scoring rubrics"
            )

    tier_text = _contract_value(contract_table, "Priority Tiers") or _subsection(
        policy, "Primary Ordering"
    ) or _subsection_with_heading_label(policy, "Priority Tiers")
    primary_ordering = re.findall(
        r"Core|Supporting|Peripheral|Insufficient Evidence",
        tier_text,
        re.IGNORECASE,
    )
    canonical_tiers = {tier.lower(): tier for tier in PRIORITY_TIERS}
    deduped_ordering: list[str] = []
    for item in primary_ordering:
        tier = canonical_tiers[item.lower()]
        if tier not in deduped_ordering:
            deduped_ordering.append(tier)
    primary_ordering = deduped_ordering
    if primary_ordering != list(PRIORITY_TIERS):
        if primary_ordering:
            warnings.append(
                "priority tier ordering defaulted to "
                "Core > Supporting > Peripheral > Insufficient Evidence"
            )
        primary_ordering = list(PRIORITY_TIERS)

    missing_policy_text = _contract_value(
        contract_table, "Missing-Data Policy"
    ) or _subsection(policy, "Missing-Data Policy")
    missing_policy = "unknown_not_zero"
    if not missing_policy_text:
        warnings.append("missing-data policy was defaulted to unknown_not_zero")

    # Format-shaped gaps are warnings-only; only empty lines block COMPLETE.
    completeness = {
        "research-line table is empty": not research_lines,
        "research-line IDs are not unique": duplicate_line_ids,
        "priority factors are missing": not factors,
    }
    soft_diagnostics = []
    if ranking_unit != "research_line":
        soft_diagnostics.append("ranking unit is not research_line")
    if ranking_mode == "qualitative_fallback":
        soft_diagnostics.append("ranking mode fell back to qualitative_fallback")
    if not missing_policy_text:
        soft_diagnostics.append("missing-data policy was defaulted to unknown_not_zero")
    if any(not factor.get("rubric") for factor in factors):
        soft_diagnostics.append("one or more priority factors lack a rubric")
    diagnostics = [message for message, failed in completeness.items() if failed]
    warnings.extend(message for message in diagnostics if message not in warnings)
    warnings.extend(message for message in soft_diagnostics if message not in warnings)
    status = "COMPLETE" if policy and not diagnostics else "PARTIAL"
    if ranking_mode == "weighted_composite" and any(
        "weighted_composite" in warning for warning in warnings
    ):
        status = "PARTIAL"
    return {
        "prioritization_schema_version": PRIORITIZATION_SCHEMA_VERSION,
        "ranking_unit": ranking_unit,
        "ranking_mode": ranking_mode,
        "numeric_dimension": _first_value(
            _subsection(policy, "Numeric Dimension")
            or _subsection(policy, "Ranking Dimension")
        ),
        "numeric_direction": (
            _first_value(_subsection(policy, "Direction")) or "descending"
        ).lower(),
        "primary_grouping": primary_grouping,
        "primary_ordering": primary_ordering,
        "secondary_ordering": _contract_value(
            contract_table, "Secondary Ordering"
        ) or _first_value(_subsection(policy, "Secondary Ordering")),
        "missing_data_policy": missing_policy,
        "missing_data_policy_text": missing_policy_text,
        "contradictory_evidence_policy": "always_surface",
        "research_lines": research_lines,
        "comparison_dimensions": dimensions,
        "factors": factors,
        "paper_roles": list(PAPER_ROLES),
        "compile_status": status,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "source": source,
    }


def write_scope_prioritization(
    workspace: Path, scope_text: str | None = None
) -> dict[str, Any]:
    text = (
        scope_text
        if scope_text is not None
        else (workspace / "SCOPE.md").read_text(encoding="utf-8")
    )
    contract = compile_scope_prioritization(text)
    atomic_write_json(workspace / PRIORITIZATION_PATH, contract)
    return contract


def load_scope_prioritization(workspace: Path) -> dict[str, Any]:
    path = workspace / PRIORITIZATION_PATH
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        scope = workspace / "SCOPE.md"
        if scope.is_file():
            return compile_scope_prioritization(scope.read_text(encoding="utf-8"))
        return compile_scope_prioritization("")
    return value if isinstance(value, dict) else compile_scope_prioritization("")


def rank_research_lines(
    contract: dict[str, Any], lines: list[dict[str, Any]]
) -> dict[str, Any]:
    """Produce deterministic preliminary ordering without inventing missing values."""
    mode = str(contract.get("ranking_mode") or "qualitative_fallback").lower()
    warnings: list[str] = []
    ordered: list[tuple[float, tuple[Any, ...], str]] = []
    unranked: list[str] = []
    scores: dict[str, float] = {}

    def secondary_key(line: dict[str, Any]) -> tuple[Any, ...]:
        """Apply supported approved tie-breakers, then a stable name fallback."""
        rule = str(contract.get("secondary_ordering") or "").lower()
        parts: list[Any] = []
        if "year" in rule:
            try:
                year = int(str(line.get("latest_year") or ""))
            except ValueError:
                year = -1
            parts.append(-year if "descending" in rule else year)
        if "name" in rule or "alphabet" in rule:
            parts.append(str(line.get("name") or "").casefold())
        return (*parts, str(line.get("name") or "").casefold())

    if mode in {"ordinal", "qualitative_fallback"}:
        order = contract.get("primary_ordering") or list(PRIORITY_TIERS)
        tier_index = {str(tier).lower(): index for index, tier in enumerate(order)}
        for line in lines:
            line_id = str(line.get("line_id"))
            assessments = line.get("proposed_tiers") or []
            tiers = {
                str(item.get("tier") if isinstance(item, dict) else item)
                for item in assessments
                if str(item.get("tier") if isinstance(item, dict) else item).strip()
            }
            if len(tiers) > 1:
                warnings.append(
                    f"{line_id}: conflicting local tiers require finalizer calibration"
                )
                unranked.append(line_id)
                continue
            tier = next(iter(tiers), str(line.get("priority_tier") or ""))
            if tier.lower() not in tier_index:
                unranked.append(line_id)
            else:
                ordered.append(
                    (float(tier_index[tier.lower()]), secondary_key(line), line_id)
                )
    elif mode == "numeric_dimension":
        dimension = str(contract.get("numeric_dimension") or "")
        descending = (
            str(contract.get("numeric_direction") or "descending").lower()
            != "ascending"
        )
        for line in lines:
            line_id = str(line.get("line_id"))
            raw = (line.get("comparison_values") or {}).get(dimension)
            if isinstance(raw, list) and raw:
                raw = raw[0].get("value") if isinstance(raw[0], dict) else raw[0]
            try:
                value = float(raw)
                if not math.isfinite(value):
                    raise ValueError
            except (TypeError, ValueError):
                unranked.append(line_id)
                continue
            scores[line_id] = value
            ordered.append(
                (-value if descending else value, secondary_key(line), line_id)
            )
    elif mode == "weighted_composite":
        factors = [
            factor
            for factor in contract.get("factors", [])
            if isinstance(factor, dict)
        ]
        weights = [factor.get("weight") for factor in factors]
        valid_factors = factors and all(
            isinstance(value, (int, float))
            and math.isfinite(float(value))
            and isinstance(factor.get("minimum"), (int, float))
            and isinstance(factor.get("maximum"), (int, float))
            and bool(str(factor.get("rubric") or "").strip())
            for factor, value in zip(factors, weights, strict=True)
        )
        if not valid_factors or not math.isclose(
            sum(float(value) for value in weights), 1.0, abs_tol=0.01
        ):
            warnings.append(
                "weighted composite is not computable from the approved factor weights"
            )
            unranked.extend(str(line.get("line_id")) for line in lines)
        else:
            for line in lines:
                line_id = str(line.get("line_id"))
                assessments = line.get("factor_assessments") or {}
                total = 0.0
                missing = False
                for factor in factors:
                    factor_id = str(factor.get("factor_id"))
                    raw = assessments.get(factor_id)
                    if isinstance(raw, list) and raw:
                        raw = (
                            raw[0].get("value")
                            if isinstance(raw[0], dict)
                            else raw[0]
                        )
                    if isinstance(raw, dict):
                        raw = raw.get("score")
                    try:
                        score = float(raw)
                        if not math.isfinite(score):
                            raise ValueError
                    except (TypeError, ValueError):
                        missing = True
                        break
                    minimum = factor.get("minimum")
                    maximum = factor.get("maximum")
                    if isinstance(minimum, (int, float)) and score < float(minimum):
                        missing = True
                    if isinstance(maximum, (int, float)) and score > float(maximum):
                        missing = True
                    total += float(factor["weight"]) * score
                if missing:
                    unranked.append(line_id)
                    continue
                scores[line_id] = total
                ordered.append((-total, secondary_key(line), line_id))
                claimed = line.get("composite_score")
                if isinstance(claimed, (int, float)) and not math.isclose(
                    float(claimed), total, abs_tol=1e-6
                ):
                    warnings.append(
                        f"{line_id}: agent composite replaced by program value"
                    )
    else:
        warnings.append(
            f"unsupported ranking mode {mode!r}; no preliminary order produced"
        )
        unranked.extend(str(line.get("line_id")) for line in lines)
    return {
        "mode": mode,
        "ordered_line_ids": [item[-1] for item in sorted(ordered)],
        "unranked_line_ids": sorted(set(unranked)),
        "computed_scores": scores,
        "warnings": warnings,
    }
