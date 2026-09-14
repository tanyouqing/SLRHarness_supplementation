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
        if self.default_mode not in {"ordinal", "numeric_dimension", "weighted_composite"}:
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
        rf"^###\s+(?:[\d.)]+\s+)?{re.escape(heading)}(?:\s*\([^)]*\))?\s*$\r?\n(.*?)(?=^##{{2,3}}\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _table(section: str) -> list[dict[str, str]]:
    rows = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(rows) < 2:
        return []
    headers = [cell.strip() for cell in rows[0].strip("|").split("|")]
    output: list[dict[str, str]] = []
    for row in rows[1:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) != len(headers):
            continue
        output.append(dict(zip(headers, cells, strict=True)))
    return output


def _column(row: dict[str, str], *names: str) -> str:
    normalized = {re.sub(r"[^a-z0-9]", "", key.lower()): value for key, value in row.items()}
    for name in names:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if value is not None:
            return value.strip()
    return ""


def _first_value(section: str) -> str:
    for line in section.splitlines():
        value = line.strip().strip("`*")
        if value and not value.startswith(("#", "<")):
            return value
    return ""


def compile_scope_prioritization(scope_text: str, source: str = "SCOPE.md") -> dict[str, Any]:
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
    ranking_unit = _first_value(_subsection(policy, "Ranking Unit")) or "research_line"
    primary_grouping = _first_value(_subsection(policy, "Primary Grouping"))
    if not primary_grouping and legacy:
        primary_grouping = "Use the approved legacy Ranking & Grouping Criteria."
        warnings.append("legacy grouping was interpreted at research-line/topic level")
    ranking_mode = _first_value(_subsection(policy, "Ranking Mode")).lower()
    ranking_mode = ranking_mode.split()[0] if ranking_mode else ""
    if ranking_mode not in VALID_RANKING_MODES:
        ranking_mode = "qualitative_fallback"
        warnings.append("ranking mode is missing or unsupported; using qualitative fallback")

    line_rows = _table(organization)
    research_lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in line_rows:
        name = _column(row, "Research Line", "Line", "Method Family")
        raw_id = _column(row, "Research Line ID", "Line ID")
        if not name and not raw_id:
            continue
        line_id = raw_id.upper() if VALID_LINE_ID.fullmatch(raw_id.upper()) else stable_research_line_id(name or raw_id)
        line_warnings: list[str] = []
        if not raw_id or raw_id.upper() != line_id:
            line_warnings.append("stable fallback ID generated from research-line name")
            warnings.append(f"{name or line_id}: missing or invalid Research Line ID")
        if line_id in seen:
            warnings.append(f"duplicate Research Line ID ignored: {line_id}")
            continue
        seen.add(line_id)
        research_lines.append(
            {
                "line_id": line_id,
                "name": name or line_id,
                "group": _column(row, "Primary Group", "Group"),
                "definition": _column(row, "Definition"),
                "scope_question": _column(row, "Main Scope Question", "Scope Question"),
                "priority_tier": _column(row, "Priority Tier", "Tier") or None,
                "warnings": line_warnings,
            }
        )

    dimensions = []
    for row in _table(comparison):
        name = _column(row, "Dimension", "Comparison Dimension")
        if name:
            dimensions.append(
                {
                    "dimension_id": _column(row, "Dimension ID") or stable_research_line_id(name).replace("RL-", "DIM-", 1),
                    "name": name,
                    "description": _column(row, "Description"),
                    "value_type": _column(row, "Value Type", "Type") or "text",
                    "applies_to": _column(row, "Applies To") or "research_line",
                }
            )

    factors = []
    for row in _table(_subsection(policy, "Priority Factors")):
        name = _column(row, "Factor", "Name")
        if not name:
            continue
        factor: dict[str, Any] = {
            "factor_id": (_column(row, "Factor ID") or stable_research_line_id(name).replace("RL-", "F-", 1)).upper(),
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
        warnings.append("priority factor rubric is incomplete")

    if ranking_mode == "weighted_composite":
        weights = [factor.get("weight") for factor in factors]
        if not factors or any(not isinstance(weight, float) for weight in weights):
            warnings.append("weighted_composite factors do not define parseable weights")
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

    primary_ordering = re.findall(
        r"Core|Supporting|Peripheral|Insufficient Evidence",
        _subsection(policy, "Primary Ordering"),
        re.IGNORECASE,
    )
    canonical_tiers = {tier.lower(): tier for tier in PRIORITY_TIERS}
    primary_ordering = [canonical_tiers[item.lower()] for item in primary_ordering]
    if not primary_ordering:
        primary_ordering = list(PRIORITY_TIERS)

    missing_policy_text = _subsection(policy, "Missing-Data Policy")
    missing_policy = "unknown_not_zero"
    if not missing_policy_text:
        warnings.append("missing-data policy was defaulted to unknown_not_zero")

    status = "COMPLETE" if policy and ranking_mode != "qualitative_fallback" else "PARTIAL"
    if ranking_mode == "weighted_composite" and any(
        "weighted_composite" in warning for warning in warnings
    ):
        status = "PARTIAL"
    return {
        "prioritization_schema_version": PRIORITIZATION_SCHEMA_VERSION,
        "ranking_unit": "research_line" if ranking_unit.lower().replace("-", "_") == "research_line" else ranking_unit,
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
        "secondary_ordering": _first_value(_subsection(policy, "Secondary Ordering")),
        "missing_data_policy": missing_policy,
        "missing_data_policy_text": missing_policy_text,
        "contradictory_evidence_policy": "always_surface",
        "research_lines": research_lines,
        "comparison_dimensions": dimensions,
        "factors": factors,
        "paper_roles": list(PAPER_ROLES),
        "compile_status": status,
        "warnings": warnings,
        "source": source,
    }


def write_scope_prioritization(workspace: Path, scope_text: str | None = None) -> dict[str, Any]:
    text = scope_text if scope_text is not None else (workspace / "SCOPE.md").read_text(encoding="utf-8")
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
        descending = str(contract.get("numeric_direction") or "descending").lower() != "ascending"
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
        factors = [factor for factor in contract.get("factors", []) if isinstance(factor, dict)]
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
            warnings.append("weighted composite is not computable from the approved factor weights")
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
                        raw = raw[0].get("value") if isinstance(raw[0], dict) else raw[0]
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
                if isinstance(claimed, (int, float)) and not math.isclose(float(claimed), total, abs_tol=1e-6):
                    warnings.append(f"{line_id}: agent composite replaced by program value")
    else:
        warnings.append(f"unsupported ranking mode {mode!r}; no preliminary order produced")
        unranked.extend(str(line.get("line_id")) for line in lines)
    return {
        "mode": mode,
        "ordered_line_ids": [item[-1] for item in sorted(ordered)],
        "unranked_line_ids": sorted(set(unranked)),
        "computed_scores": scores,
        "warnings": warnings,
    }
