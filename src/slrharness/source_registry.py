"""Deterministic paper-note validation and idempotent global source aggregation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slrharness.contracts import SCHEMA_VERSION, atomic_write_json, atomic_write_text
from slrharness.control_plane import current_issues
from slrharness.prioritization import (
    PAPER_ROLES,
    PRIORITIZATION_PATH,
    PRIORITY_TIERS,
    fallback_research_line_id,
    load_scope_prioritization,
    rank_research_lines,
    write_scope_prioritization,
)

ARTIFACTS_DIR = "artifacts"
REGISTRY_PATH = "artifacts/SOURCE_REGISTRY.json"
PAPER_LIST_PATH = "artifacts/PAPER_LIST.md"
REFERENCES_PATH = "artifacts/REFERENCES.md"
NOTE_AUDIT_PATH = "artifacts/audits/paper_note_validation.json"
DEDUP_AUDIT_PATH = "artifacts/audits/source_deduplication.json"

VALID_ACCESS = {"full_text", "abstract_only", "metadata_only", "unavailable"}
VALID_READING = {"screened", "partial", "depth_read"}
MISSING_VALUES = {
    "not reported",
    "not applicable",
    "not accessible from available source",
    "unavailable",
    "[unverified]",
    "no doi identified",
    "no arxiv id identified",
}


@dataclass
class NoteValidation:
    note_path: str
    paper_id: str | None
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AggregationResult:
    registry: dict[str, Any]
    note_audit: dict[str, Any]
    dedup_audit: dict[str, Any]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _workspace_path(workspace: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    return candidate if candidate.is_relative_to(workspace.resolve()) else None


def parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Parse the deliberately small top-level YAML subset used by templates."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", text, re.DOTALL)
    if not match:
        return {}, text
    metadata: dict[str, Any] = {}
    current_list: str | None = None
    for raw_line in match.group(1).splitlines():
        if current_list and re.match(r"^\s+-\s+", raw_line):
            metadata[current_list].append(_scalar(raw_line.split("-", 1)[1].strip()))
            continue
        current_list = None
        key, separator, raw_value = raw_line.partition(":")
        if not separator or raw_line[:1].isspace():
            continue
        key = key.strip()
        raw_value = raw_value.strip()
        if not raw_value:
            metadata[key] = []
            current_list = key
        else:
            metadata[key] = _scalar(raw_value)
    return metadata, text[match.end() :]


def write_frontmatter_field(path: Path, field: str, value: Any) -> bool:
    """Programmatically set one top-level frontmatter field; keep body intact."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^(---\s*\r?\n)(.*?)(\r?\n---\s*\r?\n)(.*)$", text, re.DOTALL)
    if not match:
        return False
    raw = str(value if value is not None else "")
    if re.search(r'[:#\[\]{}&*!|>%@`]', raw) or raw != raw.strip():
        rendered = json.dumps(raw, ensure_ascii=False)
    else:
        rendered = raw
    line = f"{field}: {rendered}"
    fm = match.group(2)
    if re.search(rf"^{re.escape(field)}\s*:", fm, re.MULTILINE):
        fm2 = re.sub(rf"^{re.escape(field)}\s*:.*$", line, fm, count=1, flags=re.MULTILINE)
    else:
        fm2 = fm.rstrip("\n") + "\n" + line
    path.write_text(match.group(1) + fm2 + match.group(3) + match.group(4), encoding="utf-8")
    return True


def _scalar(value: str) -> Any:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return [
            item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()
        ]
    return value


def _present(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    return bool(str(value or "").strip())


def _missing_allowed(value: Any) -> bool:
    return str(value or "").strip().lower() in MISSING_VALUES


def normalize_doi(value: Any) -> str:
    doi = str(value or "").strip().lower()
    doi = doi.strip("[]<>{}")
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = doi.removeprefix("doi:").strip()
    doi = doi.rstrip(".,;")
    if (
        not doi
        or _missing_allowed(doi)
        or re.search(
            r"\b(?:not reported|not found|not available|no doi|unknown|"
            r"unverified|none|n/?a)\b",
            doi,
        )
        or not re.fullmatch(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", doi)
    ):
        return ""
    return doi


def normalize_arxiv(value: Any) -> str:
    arxiv = str(value or "").strip().lower()
    arxiv = arxiv.strip("[]<>{}()")
    arxiv = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", arxiv)
    arxiv = arxiv.removeprefix("arxiv:").split("?", 1)[0].split("#", 1)[0]
    arxiv = arxiv.removesuffix(".pdf")
    arxiv = re.sub(r"v\d+$", "", arxiv).strip()
    if (
        not arxiv
        or _missing_allowed(arxiv)
        or re.search(
            r"\b(?:not reported|not found|not available|no arxiv|unknown|"
            r"unverified|none|n/?a)\b",
            arxiv,
        )
        or not re.fullmatch(
            r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})", arxiv
        )
    ):
        return ""
    return arxiv


def _arxiv_from_doi(doi: str) -> str:
    prefix = "10.48550/arxiv."
    return normalize_arxiv(doi[len(prefix) :]) if doi.startswith(prefix) else ""


def _normalized_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _stable_id(prefix: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}{digest}"


def stable_paper_id(metadata: dict[str, Any]) -> str:
    doi = normalize_doi(metadata.get("doi"))
    arxiv = normalize_arxiv(metadata.get("arxiv_id")) or _arxiv_from_doi(doi)
    authors = metadata.get("authors", "")
    first_author = authors[0] if isinstance(authors, list) and authors else str(authors)
    identity = (
        f"doi:{doi}"
        if doi and not _arxiv_from_doi(doi)
        else f"arxiv:{arxiv}"
        if arxiv
        else (
            f"title:{_normalized_title(metadata.get('title'))}|"
            f"{_normalized_title(first_author)}"
        )
    )
    return _stable_id("P", identity)


def stable_technical_id(source: dict[str, Any]) -> str:
    url = str(source.get("url") or source.get("public_url") or "").strip().lower()
    fallback = (
        f"{_normalized_title(source.get('title'))}|"
        f"{_normalized_title(source.get('organization'))}"
    )
    return _stable_id("T", f"url:{url}" if url else fallback)


def validate_paper_note(
    workspace: Path,
    note_path: Path,
    index_item: dict[str, Any] | None = None,
    audit_item: dict[str, Any] | None = None,
) -> NoteValidation:
    relative = note_path.resolve().relative_to(workspace.resolve()).as_posix()
    result = NoteValidation(relative, None)
    if not note_path.is_file() or not note_path.read_text(encoding="utf-8").strip():
        result.errors.append("note is missing or empty")
        result.valid = False
        return result
    metadata, body = parse_frontmatter(note_path)
    result.metadata = metadata
    result.paper_id = str(metadata.get("paper_id") or "").strip() or None
    for key in ("paper_id", "title", "authors", "year", "metadata_status"):
        if not _present(metadata.get(key)):
            result.errors.append(f"missing frontmatter field: {key}")
    if not _present(metadata.get("authors")) and not _missing_allowed(
        metadata.get("authors")
    ):
        result.errors.append("authors must be populated or explicitly unavailable")
    if not _present(metadata.get("year")) and not _missing_allowed(
        metadata.get("year")
    ):
        result.errors.append("year must be populated or explicitly unverified")
    identifiers = [
        normalize_doi(metadata.get("doi")),
        normalize_arxiv(metadata.get("arxiv_id")),
        str(metadata.get("public_url") or "").strip(),
    ]
    if not any(identifiers) and not any(
        _missing_allowed(metadata.get(key)) for key in ("doi", "arxiv_id", "public_url")
    ):
        result.errors.append(
            "missing source identifier or explicit no-identifier value"
        )
    if metadata.get("access") not in VALID_ACCESS:
        result.errors.append(f"invalid access level: {metadata.get('access')!r}")
    if metadata.get("reading_status") not in VALID_READING:
        result.errors.append(
            f"invalid reading status: {metadata.get('reading_status')!r}"
        )
    contract = load_scope_prioritization(workspace)
    known_lines = {
        str(item.get("line_id"))
        for item in contract.get("research_lines", [])
        if isinstance(item, dict) and item.get("line_id")
    }
    raw_line_ids = metadata.get("research_line_ids")
    line_ids = raw_line_ids if isinstance(raw_line_ids, list) else []
    if not line_ids:
        result.warnings.append("paper note has no research_line_ids")
    for line_id in line_ids:
        if str(line_id) not in known_lines:
            result.warnings.append(
                f"paper note uses unknown Research Line ID {line_id}"
            )
    role = str(metadata.get("primary_evidence_role") or "").lower()
    if not role:
        result.warnings.append("paper note has no primary_evidence_role")
    elif role not in PAPER_ROLES:
        result.warnings.append(f"paper note has invalid evidence role {role!r}")

    required_sections = {
        "problem/motivation": (
            r"^##\s+.*\b(problem|motivation|objective)\b",
            r"^##\s+.*\bwhy\b",
        ),
        "method/contribution": (
            r"^##\s+.*\b(method|contribution|approach|architecture)\b",
        ),
        "experiments": (
            r"^##\s+.*\b(experiment|evaluation|results|quantitative)\b",
        ),
        "limitations": (r"^##\s+.*\blimitation",),
        "evidence": (
            r"^##\s+.*\bevidence\b",
            r"^##\s+.*\blocators?\b",
        ),
    }
    for label, patterns in required_sections.items():
        if not any(
            re.search(pattern, body, re.MULTILINE | re.IGNORECASE)
            for pattern in patterns
        ):
            # Soft: missing headings become warnings unless the note is tiny.
            if len(body.strip()) < 400:
                result.errors.append(f"missing section: {label}")
            else:
                result.warnings.append(f"section heading not recognized: {label}")
    if not re.search(
        r"^##\s+.*\b(Scope-Driven Positioning|Positioning)\b",
        body,
        re.MULTILINE | re.IGNORECASE,
    ):
        result.warnings.append("paper note has no Scope-Driven Positioning section")
    experiment_text = _section(body, "Data, experiments, and quantitative results")
    if not experiment_text.strip():
        # Try any experiments-like section body.
        for heading in (
            "Experiments",
            "Evaluation",
            "Results",
            "Quantitative results",
            "Data and experiments",
        ):
            experiment_text = _section(body, heading)
            if experiment_text.strip():
                break
    if not experiment_text.strip():
        if len(body.strip()) >= 400:
            result.warnings.append("experimental setup/results section not recognized")
        else:
            result.errors.append("experimental setup/results are empty")
    elif not re.search(
        r"Not reported|Not applicable|Not accessible|\[UNVERIFIED\]|\||\d",
        experiment_text,
        re.IGNORECASE,
    ):
        result.warnings.append(
            "experiments/results lack explicit missing-value markers"
        )
    limitation_text = _section(body, "Limitations")
    if not limitation_text.strip():
        limitation_text = _section(body, "Reported limitations")
    if not limitation_text.strip() and len(body.strip()) < 400:
        result.errors.append("limitations are empty")
    elif not limitation_text.strip():
        result.warnings.append("limitations section not recognized")

    numeric_lines = [
        re.sub(r"\b(?:19|20)\d{2}\b", "", line) for line in experiment_text.splitlines()
    ]
    if any(
        re.search(r"\b\d+(?:\.\d+)?%?(?!\w)", line)
        and not re.search(
            r"\bE\d+\b|section|table|figure|page|\[UNVERIFIED\]",
            line,
            re.IGNORECASE,
        )
        for line in numeric_lines
    ):
        result.warnings.append("quantitative result lacks context or evidence locator")

    if index_item:
        indexed = str(index_item.get("note_path") or "").replace("\\", "/")
        if indexed != relative:
            result.errors.append("note path does not match paper index")
        for key in ("title", "doi", "arxiv_id"):
            if _canonical(index_item.get(key)) and _canonical(
                index_item.get(key)
            ) != _canonical(metadata.get(key)):
                result.errors.append(f"paper index and note disagree on {key}")
    if audit_item and audit_item.get("status") == "CORRECTED":
        corrected = audit_item.get("corrected_frontmatter") or {}
        if not isinstance(corrected, dict):
            result.errors.append("corrected_frontmatter must be an object")
        else:
            for key, expected in corrected.items():
                if key not in CORRECTABLE_FRONTMATTER_FIELDS:
                    result.warnings.append(
                        f"ignored non-frontmatter correction field {key!r}"
                    )
                    continue
                if _metadata_value(key, metadata.get(key)) != _metadata_value(
                    key, expected
                ):
                    result.errors.append(f"corrected metadata not applied for {key}")
    result.valid = not result.errors
    return result


def _canonical(value: Any) -> str:
    if isinstance(value, list):
        return "|".join(_canonical(item) for item in value)
    text = unicodedata.normalize("NFC", str(value or "")).strip().casefold()
    return re.sub(r"\s+", " ", text)


CORRECTABLE_FRONTMATTER_FIELDS = {
    "title", "authors", "year", "venue", "doi", "arxiv_id", "public_url",
    "version", "version_group",
}


def _metadata_value(field: str, value: Any) -> Any:
    if field == "doi":
        return normalize_doi(value)
    if field == "arxiv_id":
        return normalize_arxiv(value)
    if field == "authors":
        values = value if isinstance(value, list) else str(value or "").split(";")
        return tuple(_canonical(item) for item in values if _canonical(item))
    return _canonical(value)


def _section(body: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\r?\n(.*?)(?=^##\s+|\Z)",
        body,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _manifest_topics(workspace: Path) -> list[tuple[str, dict[str, Any], Path]]:
    values: list[tuple[str, dict[str, Any], Path]] = []
    for path in sorted((workspace / "topics").rglob("coordinator_manifest.json")):
        manifest = _load_json(path)
        if not manifest or manifest.get("status") not in {"COMPLETE", "PARTIAL"}:
            continue
        topic = str(manifest.get("topic_synthesis") or "")
        values.append((topic, manifest, path.parent))
    return values


def aggregate_sources(workspace: Path) -> AggregationResult:
    """Rebuild all global source artifacts deterministically without deleting notes."""
    # Recompile this derived artifact from the immutable approved scope. This
    # upgrades older/partial parser output without changing SCOPE.md itself.
    if (workspace / "SCOPE.md").is_file():
        write_scope_prioritization(workspace)

    previous = _load_json(workspace / REGISTRY_PATH) or {}
    previous_papers = [
        paper for paper in previous.get("papers", []) if isinstance(paper, dict)
    ]
    issue_state = current_issues(workspace)

    raw_papers: list[dict[str, Any]] = []
    raw_technical: list[dict[str, Any]] = []
    validations: list[NoteValidation] = []
    structural_issues: list[str] = []
    topic_coverage: dict[str, dict[str, list[str]]] = {}
    topic_assessments: list[tuple[str, dict[str, Any]]] = []

    legacy_topics = _manifest_topics(workspace)
    for topic, manifest, _root in legacy_topics:
        topic_coverage.setdefault(topic, {"papers": [], "technical_sources": []})
        assessment = manifest.get("prioritization")
        if isinstance(assessment, dict):
            topic_assessments.append((topic, assessment))
        academic = manifest.get("academic_worker") or {}
        paper_index_value = academic.get("index_path")
        paper_index_path = _workspace_path(workspace, paper_index_value)
        paper_index = _load_json(paper_index_path) if paper_index_path else None
        audit_value = (manifest.get("metadata_checker") or {}).get("audit_path")
        audit_path = _workspace_path(workspace, audit_value)
        audit = _load_json(audit_path) if audit_path else {}
        audit_items = {
            str(item.get("note_path")): item
            for item in audit.get("items", [])
            if isinstance(item, dict)
        }
        if paper_index:
            for item in paper_index.get("papers", []):
                if (
                    not isinstance(item, dict)
                    or item.get("inclusion_status") == "excluded"
                ):
                    continue
                note_rel = str(item.get("note_path") or "").replace("\\", "/")
                note_path = _workspace_path(workspace, note_rel)
                if note_path is None:
                    structural_issues.append(
                        f"paper note escapes workspace: {note_rel}"
                    )
                    continue
                validation = validate_paper_note(
                    workspace, note_path, item, audit_items.get(note_rel)
                )
                validations.append(validation)
                metadata = {**item, **validation.metadata}
                metadata.update(
                    {
                        "note_path": note_rel,
                        "topic_path": topic,
                        "validation_status": "PASS" if validation.valid else "FAILED",
                        "validation_errors": validation.errors,
                        "validation_warnings": validation.warnings,
                        # The compiler reconciles the latest finding and
                        # correction-event state; it is authoritative over a
                        # stale agent-authored frontmatter status.
                        "metadata_status": item.get("metadata_check_status")
                        or validation.metadata.get("metadata_status"),
                    }
                )
                raw_papers.append(metadata)
        elif Path(str(paper_index_value or "")).name != "NO_RESULTS.md":
            structural_issues.append(f"missing paper index for {topic}")

        technical = manifest.get("technical_worker") or {}
        technical_index_value = technical.get("index_path")
        technical_index_path = _workspace_path(workspace, technical_index_value)
        technical_index = (
            _load_json(technical_index_path) if technical_index_path else None
        )
        if technical_index:
            for item in technical_index.get("sources", []):
                if (
                    not isinstance(item, dict)
                    or item.get("inclusion_status") == "excluded"
                ):
                    continue
                note_rel = str(item.get("note_path") or "").replace("\\", "/")
                note_path = _workspace_path(workspace, note_rel)
                required = (
                    "title",
                    "resource_type",
                    "url",
                    "verification_status",
                )
                missing = [field for field in required if not _present(item.get(field))]
                if note_path is None or not note_path.is_file():
                    missing.append("note file")
                if missing:
                    missing_fields = ", ".join(missing)
                    structural_issues.append(
                        f"invalid technical source {note_rel}: missing {missing_fields}"
                    )
                raw_technical.append(
                    {**item, "note_path": note_rel, "topic_path": topic}
                )
        elif Path(str(technical_index_value or "")).name != "NO_RESULTS.md":
            structural_issues.append(f"missing technical index for {topic}")

    # New control-plane workspaces do not persist per-topic indexes/manifests.
    # Discover their canonical notes from program-owned task paths. Legacy
    # manifests above remain a read-only migration input.
    covered = {_topic_key(topic) for topic, _manifest, _root in legacy_topics}
    state = _load_json(workspace / "SLR_STATE.json") or {}
    control = state.get("control") if isinstance(state.get("control"), dict) else {}
    program_tasks = control.get("topic_tasks") if isinstance(control, dict) else {}
    if not isinstance(program_tasks, dict):
        program_tasks = {}
    for task in program_tasks.values():
        if not isinstance(task, dict) or task.get("status") not in {
            "COMPLETE", "COMPLETE_WITH_WARNINGS"
        }:
            continue
        topic_root = _topic_key(str(task.get("topic_path") or ""))
        if not topic_root or topic_root in covered:
            continue
        synthesis = topic_root + ".md"
        topic_coverage.setdefault(synthesis, {"papers": [], "technical_sources": []})
        root = workspace / topic_root
        paper_dir = root / "papers"
        paper_notes = sorted(paper_dir.glob("*.md")) if paper_dir.is_dir() else []
        for note_path in paper_notes:
            if note_path.name.lower() in {"index.md", "no_results.md", "metadata_check.md"}:
                continue
            metadata, _body = parse_frontmatter(note_path)
            relative = note_path.resolve().relative_to(workspace.resolve()).as_posix()
            if not metadata.get("title") or not (
                metadata.get("authors") or metadata.get("doi") or metadata.get("arxiv_id")
            ):
                structural_issues.append(f"invalid paper note identity: {relative}")
                continue
            canonical_id = stable_paper_id(metadata)
            raw_papers.append(
                {
                    **metadata,
                    "paper_id": canonical_id,
                    "note_path": relative,
                    "topic_path": synthesis,
                    "validation_status": "PASS",
                    "validation_errors": [],
                    "validation_warnings": [],
                    "metadata_status": (
                        "UNRESOLVED"
                        if any(
                            issue.get("target") == relative
                            and issue.get("status") != "verified_closed"
                            for issue in issue_state.values()
                        )
                        else "PASS"
                    ),
                }
            )
            validations.append(
                NoteValidation(relative, canonical_id, valid=True, metadata=metadata)
            )
        technical_dir = root / "technical_sources"
        technical_notes = (
            sorted(technical_dir.glob("*.md")) if technical_dir.is_dir() else []
        )
        for note_path in technical_notes:
            if note_path.name.lower() in {"index.md", "no_results.md"}:
                continue
            metadata, _body = parse_frontmatter(note_path)
            relative = note_path.resolve().relative_to(workspace.resolve()).as_posix()
            if not metadata.get("title") or not metadata.get("url"):
                structural_issues.append(f"invalid technical note identity: {relative}")
                continue
            raw_technical.append(
                {
                    **metadata,
                    "source_id": stable_technical_id(metadata),
                    "note_path": relative,
                    "topic_path": synthesis,
                }
            )

    identity_to_ids: dict[str, set[str]] = {}
    paper_id_to_dois: dict[str, set[str]] = {}
    paper_id_to_arxiv: dict[str, set[str]] = {}
    for item in raw_papers:
        doi = normalize_doi(item.get("doi"))
        arxiv = normalize_arxiv(item.get("arxiv_id"))
        note_id = str(item.get("paper_id") or "")
        if doi and note_id:
            identity_to_ids.setdefault(f"doi:{doi}", set()).add(note_id)
            paper_id_to_dois.setdefault(note_id, set()).add(doi)
        if arxiv and note_id:
            identity_to_ids.setdefault(f"arxiv:{arxiv}", set()).add(note_id)
            paper_id_to_arxiv.setdefault(note_id, set()).add(arxiv)
    for identity, note_ids in identity_to_ids.items():
        if len(note_ids) > 1:
            structural_issues.append(
                f"different Paper IDs refer to the same identifier {identity}: "
                f"{sorted(note_ids)}"
            )
    for label, mappings in (
        ("DOIs", paper_id_to_dois),
        ("arXiv IDs", paper_id_to_arxiv),
    ):
        for note_id, identities in mappings.items():
            if len(identities) <= 1:
                continue
            identities_text = sorted(identities)
            structural_issues.append(
                f"Paper ID {note_id} refers to multiple {label}: {identities_text}"
            )

    papers, paper_decisions = _deduplicate_papers(raw_papers, previous_papers)
    technical_sources, technical_duplicates = _deduplicate_technical(raw_technical)
    duplicates = [
        item
        for item in paper_decisions
        if item.get("canonical_id")
        and item.get("merge_reason") in {"strong_match", "weak_match"}
    ]
    duplicates.extend(technical_duplicates)
    for paper in papers:
        for topic in paper["topic_paths"]:
            topic_coverage.setdefault(topic, {"papers": [], "technical_sources": []})[
                "papers"
            ].append(paper["source_id"])
    for source in technical_sources:
        for topic in source["topic_paths"]:
            topic_coverage.setdefault(topic, {"papers": [], "technical_sources": []})[
                "technical_sources"
            ].append(source["source_id"])

    unresolved = [
        paper["source_id"]
        for paper in papers
        if paper.get("metadata_status") in {"UNRESOLVED", "NOT_CHECKED"}
        or paper.get("validation_status") == "FAILED"
    ]
    registry = {
        "schema_version": SCHEMA_VERSION,
        "papers": papers,
        "technical_sources": technical_sources,
        "duplicates": duplicates,
        "unresolved_metadata": sorted(unresolved),
        "topic_coverage": topic_coverage,
        "research_lines": [],
        "generated_at": _now(),
    }
    registry["research_lines"] = _aggregate_research_lines(
        workspace,
        papers,
        technical_sources,
        topic_coverage,
        topic_assessments,
    )
    contract = load_scope_prioritization(workspace)
    registry["prioritization"] = rank_research_lines(
        contract, registry["research_lines"]
    )
    note_audit = {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "PASS"
            if all(item.valid for item in validations) and not structural_issues
            else "FAILED"
        ),
        "notes_checked": len(validations),
        "valid_notes": sum(item.valid for item in validations),
        "errors": structural_issues,
        "items": [
            {
                "note_path": item.note_path,
                "paper_id": item.paper_id,
                "status": "PASS" if item.valid else "FAILED",
                "errors": item.errors,
                "warnings": item.warnings,
            }
            for item in validations
        ],
        "generated_at": _now(),
    }
    dedup_errors = [
        item for item in paper_decisions if item.get("severity") == "error"
    ]
    dedup_warnings = [
        item
        for item in paper_decisions
        if item.get("merge_reason") in {"weak_match", "conflict_not_merged"}
        or item.get("migration_warning")
    ]
    dedup_audit = {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "FAILED"
            if dedup_errors
            else "PASS_WITH_WARNINGS"
            if dedup_warnings
            else "PASS"
        ),
        "paper_input_count": len(raw_papers),
        "paper_output_count": len(papers),
        "technical_input_count": len(raw_technical),
        "technical_output_count": len(technical_sources),
        "duplicates": duplicates,
        "decisions": [*paper_decisions, *technical_duplicates],
        "warnings": dedup_warnings,
        "errors": dedup_errors,
        "generated_at": _now(),
    }
    _write_json(workspace / REGISTRY_PATH, registry)
    _write_json(workspace / NOTE_AUDIT_PATH, note_audit)
    _write_json(workspace / DEDUP_AUDIT_PATH, dedup_audit)
    atomic_write_text(workspace / PAPER_LIST_PATH, _paper_list(papers))
    atomic_write_text(
        workspace / REFERENCES_PATH, _references(papers, technical_sources)
    )
    return AggregationResult(registry, note_audit, dedup_audit)


def _task_line_markers(workspace: Path) -> dict[str, str]:
    try:
        text = (workspace / "TASKS.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    output: dict[str, str] = {}
    for match in re.finditer(
        r"^\s*-\s*\[[ xX]\]\s+(topics/\S+).*?\[line=([^\]]+)\]",
        text,
        re.MULTILINE | re.IGNORECASE,
    ):
        output[match.group(1).rstrip("/")] = match.group(2).strip().upper()
    return output


def _topic_key(value: str) -> str:
    return value.replace("\\", "/").removesuffix(".md").rstrip("/")


def _aggregate_research_lines(
    workspace: Path,
    papers: list[dict[str, Any]],
    technical_sources: list[dict[str, Any]],
    topic_coverage: dict[str, dict[str, list[str]]],
    topic_assessments: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Add a line-level view without changing source deduplication semantics."""
    contract = load_scope_prioritization(workspace)
    lines: dict[str, dict[str, Any]] = {}
    for item in contract.get("research_lines", []):
        if not isinstance(item, dict) or not item.get("line_id"):
            continue
        line_id = str(item["line_id"])
        lines[line_id] = {
            "line_id": line_id,
            "name": item.get("name") or line_id,
            "group": item.get("group") or "Unspecified",
            "definition": item.get("definition") or "",
            "scope_question": item.get("scope_question") or "",
            "priority_tier": item.get("priority_tier"),
            "scope_status": "APPROVED",
            "topic_paths": [],
            "paper_ids": [],
            "technical_source_ids": [],
            "paper_roles": {role: [] for role in PAPER_ROLES},
            "comparison_values": {},
            "factor_assessments": {},
            "proposed_tiers": [],
            "latest_year": None,
            "_manifest_paper_roles": {},
            "ranking_status": "INSUFFICIENT_EVIDENCE",
            "warnings": list(item.get("warnings") or []),
        }

    def ensure(line_id: str) -> dict[str, Any] | None:
        if not line_id or line_id == "NOT_APPLICABLE":
            return None
        if line_id not in lines:
            lines[line_id] = {
                "line_id": line_id,
                "name": line_id,
                "group": "Emergent",
                "definition": "",
                "scope_question": "",
                "priority_tier": None,
                "scope_status": "EMERGENT_UNAPPROVED",
                "topic_paths": [],
                "paper_ids": [],
                "technical_source_ids": [],
                "paper_roles": {role: [] for role in PAPER_ROLES},
                "comparison_values": {},
                "factor_assessments": {},
                "proposed_tiers": [],
                "latest_year": None,
                "_manifest_paper_roles": {},
                "ranking_status": "QUALITATIVE",
                "warnings": ["research line is not defined in the approved scope"],
            }
        return lines[line_id]

    task_markers = _task_line_markers(workspace)
    topic_to_line: dict[str, str] = {}
    assessment_by_topic = {
        _topic_key(topic): value for topic, value in topic_assessments
    }
    for topic in topic_coverage:
        key = _topic_key(topic)
        assessment = assessment_by_topic.get(key, {})
        line_id = str(assessment.get("research_line_id") or task_markers.get(key) or "")
        if not line_id:
            line_id = fallback_research_line_id(key)
        line = ensure(line_id)
        if line is None:
            continue
        topic_to_line[key] = line_id
        line["topic_paths"].append(topic)
        tier = str(assessment.get("proposed_tier") or "").strip()
        if tier:
            canonical = next(
                (
                    value
                    for value in PRIORITY_TIERS
                    if value.lower() == tier.lower()
                ),
                tier,
            )
            line["proposed_tiers"].append(
                {
                    "topic_path": topic,
                    "tier": canonical,
                    "confidence": assessment.get("confidence"),
                }
            )
        for field in ("comparison_values", "factor_assessments"):
            values = assessment.get(field)
            if isinstance(values, dict):
                for key_name, value in values.items():
                    line[field].setdefault(str(key_name), []).append(
                        {"topic_path": topic, "value": value}
                    )
        manifest_roles = assessment.get("paper_roles")
        if isinstance(manifest_roles, dict):
            line["_manifest_paper_roles"].update(
                {str(key): str(value).lower() for key, value in manifest_roles.items()}
            )
        for source_id in assessment.get("supporting_source_ids", []):
            line["technical_source_ids"].append(str(source_id))
        line["warnings"].extend(
            str(value) for value in assessment.get("warnings", []) if value
        )

    for paper in papers:
        explicit = paper.get("research_line_ids") or []
        associated = set(str(value) for value in explicit)
        if not associated:
            associated.update(
                topic_to_line.get(_topic_key(str(topic)), "")
                for topic in paper.get("topic_paths", [])
            )
            associated.discard("")
        paper["research_line_ids"] = sorted(associated)
        roles = (
            paper.get("evidence_roles")
            if isinstance(paper.get("evidence_roles"), dict)
            else {}
        )
        for line_id in associated:
            line = ensure(line_id)
            if line is None:
                continue
            line["paper_ids"].append(paper["source_id"])
            try:
                publication_year = int(str(paper.get("year") or ""))
            except ValueError:
                publication_year = None
            if publication_year is not None:
                current_year = line.get("latest_year")
                if current_year is None or publication_year > current_year:
                    line["latest_year"] = publication_year
            role = str(roles.get(line_id) or "").lower()
            if not role:
                aliases = {paper["source_id"], *paper.get("alias_ids", [])}
                role = next(
                    (
                        value
                        for paper_key, value in line["_manifest_paper_roles"].items()
                        if paper_key in aliases
                    ),
                    "unassigned",
                )
            if role not in PAPER_ROLES:
                line["warnings"].append(
                    f"paper {paper['source_id']} has invalid role {role!r}; "
                    "treated as unassigned"
                )
                role = "unassigned"
            line["paper_roles"][role].append(paper["source_id"])

    for source in technical_sources:
        explicit = source.get("research_line_ids") or []
        associated = set(str(value) for value in explicit)
        if not associated:
            associated.update(
                topic_to_line.get(_topic_key(str(topic)), "")
                for topic in source.get("topic_paths", [])
            )
            associated.discard("")
        source["research_line_ids"] = sorted(associated)
        for line_id in associated:
            line = ensure(line_id)
            if line is not None:
                line["technical_source_ids"].append(source["source_id"])

    for line in lines.values():
        line.pop("_manifest_paper_roles", None)
        for key in ("topic_paths", "paper_ids", "technical_source_ids"):
            line[key] = sorted(set(line[key]))
        for role in PAPER_ROLES:
            line["paper_roles"][role] = sorted(set(line["paper_roles"][role]))
        if not line["paper_ids"]:
            line["ranking_status"] = "INSUFFICIENT_EVIDENCE"
            line["warnings"].append("research line has no academic evidence")
        elif line["scope_status"] == "EMERGENT_UNAPPROVED":
            line["ranking_status"] = "QUALITATIVE"
        elif line["warnings"] or contract.get("compile_status") == "PARTIAL":
            line["ranking_status"] = "PARTIAL"
        else:
            line["ranking_status"] = "COMPLETE"
        line["warnings"] = sorted(set(line["warnings"]))
    return [lines[line_id] for line_id in sorted(lines)]


def _deduplicate_papers(
    raw: list[dict[str, Any]], previous: list[dict[str, Any]] | dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deduplicate without allowing weak keys to bridge strong identities."""
    groups: list[list[dict[str, Any]]] = []
    decisions: list[dict[str, Any]] = []

    def identity(item: dict[str, Any]) -> tuple[set[str], set[str]]:
        doi = normalize_doi(item.get("doi"))
        explicit_arxiv = normalize_arxiv(item.get("arxiv_id"))
        datacite_arxiv = _arxiv_from_doi(doi)
        arxiv_ids = {value for value in (explicit_arxiv, datacite_arxiv) if value}
        publisher_dois = {doi} if doi and not datacite_arxiv else set()
        return arxiv_ids, publisher_dois

    def group_identity(members: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
        arxiv_ids: set[str] = set()
        publisher_dois: set[str] = set()
        for member in members:
            member_arxiv, member_dois = identity(member)
            arxiv_ids.update(member_arxiv)
            publisher_dois.update(member_dois)
        return arxiv_ids, publisher_dois

    def weak_key(item: dict[str, Any]) -> str:
        authors = item.get("authors", "")
        first = authors[0] if isinstance(authors, list) and authors else authors
        title = _normalized_title(item.get("title"))
        author = _normalized_title(first)
        return f"{title}|{author}" if title and author else ""

    for item in raw:
        item_arxiv, item_dois = identity(item)
        item_strong = bool(item_arxiv or item_dois)
        strong_matches: list[int] = []
        weak_match_index: int | None = None
        for index, members in enumerate(groups):
            group_arxiv, group_dois = group_identity(members)
            arxiv_conflict = bool(
                item_arxiv and group_arxiv and item_arxiv != group_arxiv
            )
            doi_conflict = bool(item_dois and group_dois and item_dois != group_dois)
            shared_strong = bool(
                (item_arxiv & group_arxiv) or (item_dois & group_dois)
            )
            same_version = bool(
                _canonical(item.get("version_group"))
                and any(
                    _canonical(item.get("version_group"))
                    == _canonical(member.get("version_group"))
                    for member in members
                )
            )
            same_weak = bool(
                weak_key(item)
                and any(weak_key(item) == weak_key(member) for member in members)
            )
            if arxiv_conflict or doi_conflict:
                if shared_strong or same_version or same_weak:
                    decisions.append(
                        {
                            "kind": "paper",
                            "merge_reason": "conflict_not_merged",
                            "note_paths": sorted(
                                {str(item.get("note_path")), *(
                                    str(member.get("note_path")) for member in members
                                )}
                            ),
                            "strong_identities": sorted(
                                {*(f"arxiv:{v}" for v in item_arxiv | group_arxiv),
                                 *(f"doi:{v}" for v in item_dois | group_dois)}
                            ),
                        }
                    )
                continue
            if shared_strong:
                strong_matches.append(index)
                continue
            if (
                weak_match_index is None
                and not item_strong
                and not group_arxiv
                and not group_dois
                and same_weak
            ):
                weak_match_index = index
                continue
            if same_version:
                decisions.append(
                    {
                        "kind": "paper",
                        "merge_reason": "weak_match",
                        "merged": False,
                        "note_paths": sorted(
                            {str(item.get("note_path")), *(
                                str(member.get("note_path")) for member in members
                            )}
                        ),
                        "warning": "version_group alone is insufficient to merge",
                    }
                )
        if strong_matches:
            target = strong_matches[0]
            groups[target].append(item)
            for other in reversed(strong_matches[1:]):
                groups[target].extend(groups.pop(other))
            match_reason = "strong_match"
            match_index = target
        elif weak_match_index is not None:
            groups[weak_match_index].append(item)
            match_reason = "weak_match"
            match_index = weak_match_index
        else:
            groups.append([item])
            match_index = None
        if match_index is not None:
            decisions.append(
                {
                    "kind": "paper",
                    "merge_reason": match_reason,
                    "note_paths": sorted(
                        {str(member.get("note_path")) for member in groups[match_index]}
                    ),
                }
            )

    output: list[dict[str, Any]] = []
    used_source_ids: set[str] = set()
    previous_records = previous if isinstance(previous, list) else []
    legacy_previous_ids = previous if isinstance(previous, dict) else {}
    for members in groups:
        arxiv_ids, publisher_dois = group_identity(members)
        if len(arxiv_ids) > 1 or len(publisher_dois) > 1:
            # This is a defensive postcondition. The grouping algorithm above
            # should make it unreachable, but never publish a lossy cluster.
            decisions.append(
                {
                    "kind": "paper",
                    "merge_reason": "conflict_not_merged",
                    "severity": "error",
                    "note_paths": sorted(
                        str(item.get("note_path")) for item in members
                    ),
                    "strong_identities": sorted(
                        {*(f"arxiv:{v}" for v in arxiv_ids),
                         *(f"doi:{v}" for v in publisher_dois)}
                    ),
                }
            )
            # Preserve every input rather than silently discarding an identity.
            for member in reversed(members[1:]):
                groups.append([member])
            members = members[:1]
            arxiv_ids, publisher_dois = group_identity(members)
        canonical = max(
            members,
            key=lambda item: (
                item.get("metadata_status") in {"PASS", "CORRECTED"},
                item.get("access") == "full_text",
                bool(normalize_doi(item.get("doi"))),
            ),
        )
        # Preserve all non-conflicting strong identifiers in the canonical row.
        if arxiv_ids:
            canonical = {**canonical, "arxiv_id": next(iter(arxiv_ids))}
        if publisher_dois:
            canonical = {**canonical, "doi": next(iter(publisher_dois))}

        existing_id: str | None = None
        current_notes = {str(item.get("note_path")) for item in members}
        for old in previous_records:
            old_id = str(old.get("source_id") or "")
            old_arxiv, old_dois = identity(old)
            conflicts = bool(
                (old_arxiv and arxiv_ids and old_arxiv != arxiv_ids)
                or (old_dois and publisher_dois and old_dois != publisher_dois)
            )
            shared = bool((old_arxiv & arxiv_ids) or (old_dois & publisher_dois))
            if old_id and shared and not conflicts and old_id not in used_source_ids:
                existing_id = old_id
                break
            if old_id and current_notes & set(map(str, old.get("note_paths", []))):
                if not shared or conflicts or old_id in used_source_ids:
                    decisions.append(
                        {
                            "kind": "paper",
                            "merge_reason": "conflict_not_merged",
                            "migration_warning": True,
                            "old_source_id": old_id,
                            "note_paths": sorted(current_notes),
                        }
                    )
        if not previous_records:
            existing_id = next(
                (
                    legacy_previous_ids[key]
                    for key in [
                        *(f"arxiv:{value}" for value in arxiv_ids),
                        *(f"doi:{value}" for value in publisher_dois),
                    ]
                    if key in legacy_previous_ids
                    and legacy_previous_ids[key] not in used_source_ids
                ),
                None,
            )
        source_id = existing_id or stable_paper_id(canonical)
        if source_id in used_source_ids:
            decisions.append(
                {
                    "kind": "paper",
                    "merge_reason": "conflict_not_merged",
                    "severity": "error",
                    "canonical_id": source_id,
                    "note_paths": sorted(current_notes),
                }
            )
            seed = "split:" + "|".join(sorted(current_notes))
            suffix = 0
            while source_id in used_source_ids:
                source_id = _stable_id("P", f"{seed}:{suffix}")
                suffix += 1
        used_source_ids.add(source_id)
        research_line_ids = sorted(
            {
                str(line_id)
                for item in members
                for line_id in (
                    item.get("research_line_ids")
                    if isinstance(item.get("research_line_ids"), list)
                    else []
                )
                if str(line_id)
            }
        )
        evidence_roles: dict[str, str] = {}
        for item in members:
            item_roles = item.get("evidence_roles")
            if isinstance(item_roles, dict):
                evidence_roles.update(
                    {str(key): str(value).lower() for key, value in item_roles.items()}
                )
            primary_role = str(item.get("primary_evidence_role") or "").lower()
            item_lines = item.get("research_line_ids")
            if primary_role and isinstance(item_lines, list) and item_lines:
                evidence_roles.setdefault(str(item_lines[0]), primary_role)
        entry = {
            "source_id": source_id,
            "alias_ids": sorted(
                {
                    str(item.get("paper_id"))
                    for item in members
                    if item.get("paper_id") and str(item.get("paper_id")) != source_id
                }
            ),
            "title": canonical.get("title"),
            "authors": canonical.get("authors"),
            "year": canonical.get("year"),
            "venue": canonical.get("venue") or canonical.get("publication"),
            "doi": normalize_doi(canonical.get("doi")) or None,
            "arxiv_id": normalize_arxiv(canonical.get("arxiv_id")) or None,
            "public_url": canonical.get("public_url") or canonical.get("url"),
            "version": canonical.get("version"),
            "version_group": canonical.get("version_group"),
            "note_paths": sorted({str(item.get("note_path")) for item in members}),
            "topic_paths": sorted({str(item.get("topic_path")) for item in members}),
            "research_line_ids": research_line_ids,
            "evidence_roles": evidence_roles,
            "metadata_status": canonical.get("metadata_status")
            or canonical.get("metadata_check_status"),
            "access": canonical.get("access") or canonical.get("access_level"),
            "reading_status": canonical.get("reading_status"),
            "evidence_ids": sorted(
                {
                    str(evidence)
                    for item in members
                    for evidence in item.get("evidence_ids", [])
                }
            ),
            "inclusion_status": canonical.get("inclusion_status", "included"),
            "validation_status": (
                "PASS"
                if all(item.get("validation_status") == "PASS" for item in members)
                else "FAILED"
            ),
        }
        output.append(entry)
        if len(members) > 1:
            reason = next(
                (
                    item["merge_reason"]
                    for item in reversed(decisions)
                    if item.get("note_paths") == entry["note_paths"]
                    and item.get("merge_reason") in {"strong_match", "weak_match"}
                ),
                "strong_match",
            )
            decisions.append(
                {
                    "kind": "paper",
                    "canonical_id": source_id,
                    "note_paths": entry["note_paths"],
                    "merge_reason": reason,
                    "matched_by": sorted(
                        [
                            *(f"arxiv:{value}" for value in arxiv_ids),
                            *(f"doi:{value}" for value in publisher_dois),
                        ]
                        or [f"fallback:{weak_key(canonical)}"]
                    ),
                }
            )
    return sorted(output, key=lambda item: item["source_id"]), decisions


def _deduplicate_technical(
    raw: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in raw:
        grouped.setdefault(stable_technical_id(item), []).append(item)
    output: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for source_id, members in grouped.items():
        canonical = members[0]
        research_line_ids = sorted(
            {
                str(line_id)
                for item in members
                for line_id in (
                    item.get("research_line_ids")
                    if isinstance(item.get("research_line_ids"), list)
                    else []
                )
                if str(line_id)
            }
        )
        entry = {
            "source_id": source_id,
            "title": canonical.get("title"),
            "organization": canonical.get("organization")
            or canonical.get("author_organization"),
            "resource_type": canonical.get("resource_type"),
            "url": canonical.get("url") or canonical.get("public_url"),
            "publication_date": canonical.get("publication_date")
            or canonical.get("updated_at"),
            "note_paths": sorted({str(item.get("note_path")) for item in members}),
            "topic_paths": sorted({str(item.get("topic_path")) for item in members}),
            "research_line_ids": research_line_ids,
            "support_roles": sorted(
                {
                    str(item.get("support_role"))
                    for item in members
                    if item.get("support_role")
                }
            ),
            "verification_status": canonical.get("verification_status")
            or canonical.get("status"),
            "inclusion_status": canonical.get("inclusion_status", "included"),
        }
        output.append(entry)
        if len(members) > 1:
            duplicates.append(
                {
                    "kind": "technical_source",
                    "canonical_id": source_id,
                    "note_paths": entry["note_paths"],
                    "matched_by": [f"url:{entry['url']}"],
                }
            )
    return sorted(output, key=lambda item: item["source_id"]), duplicates


def _paper_list(papers: list[dict[str, Any]]) -> str:
    lines = [
        "# Global Paper List",
        "",
        (
            "| Paper ID | Title | Authors | Year | Venue | DOI / arXiv | Access | "
            "Metadata | Topics | Notes |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for paper in papers:
        identifier = (
            paper.get("doi") or paper.get("arxiv_id") or "[UNVERIFIED METADATA]"
        )
        lines.append(
            "| {source_id} | {title} | {authors} | {year} | {venue} | {identifier} | "
            "{access} | {metadata_status} | {topics} | {notes} |".format(
                **paper,
                identifier=identifier,
                topics=", ".join(paper["topic_paths"]),
                notes="<br>".join(paper["note_paths"]),
            )
        )
    return "\n".join(lines) + "\n"


def _references(
    papers: list[dict[str, Any]], technical_sources: list[dict[str, Any]]
) -> str:
    lines = ["# References", "", "## Academic References", ""]
    for paper in papers:
        marker = (
            " [UNVERIFIED METADATA]"
            if paper["source_id"]
            in {
                item["source_id"]
                for item in papers
                if item.get("metadata_status") in {"UNRESOLVED", "NOT_CHECKED"}
                or item.get("validation_status") == "FAILED"
            }
            else ""
        )
        authors = paper.get("authors")
        if isinstance(authors, list):
            authors = ", ".join(authors)
        identifier = (
            paper.get("doi") or paper.get("arxiv_id") or paper.get("public_url")
        )
        lines.append(
            f"- [{paper['source_id']}] {authors}. ({paper.get('year')}). "
            f"{paper.get('title')}. {paper.get('venue')}. {identifier}.{marker}"
        )
    lines.extend(["", "## Technical and Web Sources", ""])
    for source in technical_sources:
        lines.append(
            f"- [{source['source_id']}] {source.get('organization')}. "
            f"{source.get('title')}. {source.get('url')}."
        )
    return "\n".join(lines) + "\n"
