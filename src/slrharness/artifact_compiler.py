"""Compile agent-authored topic evidence into canonical control artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slrharness.contracts import SCHEMA_VERSION, atomic_write_json, atomic_write_text
from slrharness.prioritization import PAPER_ROLES
from slrharness.source_registry import (
    parse_frontmatter,
    stable_paper_id,
    stable_technical_id,
)

PAPER_ARTIFACT_TYPE = "academic_paper_note"
TECHNICAL_ARTIFACT_TYPE = "technical_source_note"
METADATA_STATUSES = {"PASS", "CORRECTED", "UNRESOLVED", "NOT_CHECKED"}
TECHNICAL_ROLES = {
    "implementation_detail",
    "official_system_description",
    "reproducibility_support",
    "benchmark_description",
    "historical_context",
    "claim_only",
    "unassigned",
}
GENERATED_NOTE_NAMES = {"index.md", "no_results.md", "metadata_check.md"}


@dataclass
class TopicCompilationResult:
    compiled: bool
    status: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recovered_paths: list[dict[str, str]] = field(default_factory=list)
    paper_count: int = 0
    technical_source_count: int = 0
    metadata_status: str = "FAILED"
    manifest: dict[str, Any] | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _DiscoveredNote:
    kind: str
    source: Path
    canonical: Path
    metadata: dict[str, Any]
    body: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _relative(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def _nominal_relative(workspace: Path, path: Path) -> str:
    """Return an expected program path without following an unsafe symlink."""
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.resolve().relative_to(workspace.resolve()).as_posix()


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _legacy_items(path: Path, key: str) -> list[dict[str, Any]]:
    value = _json_object(path)
    items = value.get(key, []) if value else []
    return [item for item in items if isinstance(item, dict)]


def _legacy_for_note(
    workspace: Path, path: Path, items: list[dict[str, Any]]
) -> dict[str, Any]:
    relative = _relative(workspace, path)
    exact = [item for item in items if str(item.get("note_path")) == relative]
    if len(exact) == 1:
        return exact[0]
    basename = [
        item
        for item in items
        if Path(str(item.get("note_path") or "")).name == path.name
    ]
    return basename[0] if len(basename) == 1 else {}


def _looks_like_paper(metadata: dict[str, Any], body: str) -> bool:
    if metadata.get("artifact_type") == PAPER_ARTIFACT_TYPE:
        return True
    fields = sum(
        bool(metadata.get(name))
        for name in ("paper_id", "doi", "arxiv_id", "authors", "year")
    )
    sections = sum(
        bool(re.search(pattern, body, re.MULTILINE | re.IGNORECASE))
        for pattern in (
            r"^##\s+Problem and motivation",
            r"^##\s+Core method and contribution",
            r"^##\s+Evidence",
        )
    )
    return fields >= 2 or sections >= 2


def _looks_like_technical(metadata: dict[str, Any], body: str) -> bool:
    if metadata.get("artifact_type") == TECHNICAL_ARTIFACT_TYPE:
        return True
    fields = sum(
        bool(metadata.get(name))
        for name in ("source_id", "organization", "resource_type", "url")
    )
    return fields >= 2 or bool(
        re.search(r"^##\s+(?:Technical content|Claims|Evidence)", body, re.MULTILINE)
    )


def _paper_identity(metadata: dict[str, Any]) -> dict[str, Any] | None:
    identity = {
        "title": metadata.get("title"),
        "authors": metadata.get("authors"),
        "doi": metadata.get("doi"),
        "arxiv_id": metadata.get("arxiv_id"),
    }
    if not identity["title"] or not (
        identity["authors"] or identity["doi"] or identity["arxiv_id"]
    ):
        return None
    return identity


def _collision_target(path: Path, source: Path) -> Path:
    if not path.exists() or path.resolve() == source.resolve():
        return path
    digest = hashlib.sha256(
        (source.as_posix() + "\0" + source.read_text(encoding="utf-8")).encode(
            "utf-8"
        )
    ).hexdigest()[:8]
    return path.with_name(f"{path.stem}-{digest}{path.suffix}")


def _discover_notes(
    workspace: Path,
    paths: Any,
    report: dict[str, Any],
    *,
    dry_run: bool,
) -> list[_DiscoveredNote]:
    root = paths.artifact_root.resolve()
    old_papers = _legacy_items(paths.paper_dir / "index.json", "papers")
    old_technical = _legacy_items(paths.technical_dir / "index.json", "sources")
    output: list[_DiscoveredNote] = []
    for candidate in sorted(root.rglob("*.md")):
        if not candidate.resolve().is_relative_to(root):
            report["errors"].append(
                "candidate path escapes the task artifact root: "
                f"{_nominal_relative(workspace, candidate)}"
            )
            continue
        if candidate.name.lower() in GENERATED_NOTE_NAMES:
            continue
        if paths.audit_dir.resolve() in candidate.resolve().parents:
            continue
        try:
            metadata, body = parse_frontmatter(candidate)
        except OSError as exc:
            report["errors"].append(f"cannot read candidate {candidate}: {exc}")
            continue
        in_papers = paths.paper_dir.resolve() in (
            candidate.resolve(),
            *candidate.resolve().parents,
        )
        in_technical = paths.technical_dir.resolve() in (
            candidate.resolve(),
            *candidate.resolve().parents,
        )
        paper_legacy = _legacy_for_note(workspace, candidate, old_papers)
        technical_legacy = _legacy_for_note(workspace, candidate, old_technical)
        if metadata.get("artifact_type") == TECHNICAL_ARTIFACT_TYPE:
            kind = "technical"
        elif metadata.get("artifact_type") == PAPER_ARTIFACT_TYPE:
            kind = "paper"
        elif in_papers or paper_legacy or _looks_like_paper(metadata, body):
            kind = "paper"
        elif in_technical or technical_legacy or _looks_like_technical(metadata, body):
            kind = "technical"
        else:
            # A Markdown file directly under the task root is not claimed
            # unless its content identifies its artifact type.
            continue
        legacy = paper_legacy if kind == "paper" else technical_legacy
        merged = {**legacy, **metadata}
        if not metadata.get("artifact_type"):
            report["warnings"].append(
                f"{_relative(workspace, candidate)} has no artifact_type; "
                "classified from its directory/content"
            )
        directory = paths.paper_dir if kind == "paper" else paths.technical_dir
        target = _collision_target(directory / candidate.name, candidate)
        report["discovered_files"].append(
            {"kind": kind, "path": _relative(workspace, candidate)}
        )
        if target.resolve() != candidate.resolve():
            repair = {
                "kind": kind,
                "before": _relative(workspace, candidate),
                "after": _relative(workspace, target),
            }
            report["path_repairs"].append(repair)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                candidate.replace(target)
        read_path = candidate if dry_run else target
        output.append(_DiscoveredNote(kind, read_path, target, merged, body))
    return output


def _frontmatter_insert(path: Path, key: str, value: str) -> bool:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\r?\n", text)
    if not match:
        return False
    updated = text[: match.end()] + f'{key}: "{value}"\n' + text[match.end() :]
    atomic_write_text(path, updated)
    return True


def _paper_entries(
    workspace: Path,
    notes: list[_DiscoveredNote],
    report: dict[str, Any],
    task_line_id: str,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for note in notes:
        metadata = note.metadata
        identity = _paper_identity(metadata)
        if identity is None:
            report["warnings"].append(
                "ignored unidentifiable paper Markdown: "
                f"{_relative(workspace, note.canonical)}"
            )
            continue
        paper_id = stable_paper_id(identity)
        proposed_id = str(metadata.get("paper_id") or "").strip()
        if proposed_id and proposed_id != paper_id:
            report["warnings"].append(
                f"ignored agent-proposed paper ID {proposed_id}; using {paper_id}"
            )
        if not proposed_id:
            report["generated_ids"].append(
                {"path": _relative(workspace, note.canonical), "paper_id": paper_id}
            )
            if not dry_run and not _frontmatter_insert(
                note.canonical, "paper_id", paper_id
            ):
                report["warnings"].append(
                    f"generated {paper_id} was retained in the index only"
                )
        raw_lines = metadata.get("research_line_ids")
        line_ids = (
            [str(value) for value in raw_lines if str(value).strip()]
            if isinstance(raw_lines, list)
            else []
        )
        if not line_ids and task_line_id not in {"", "NOT_APPLICABLE"}:
            line_ids = [task_line_id]
            report["warnings"].append(
                f"paper {paper_id} inherited research line {task_line_id}"
            )
        role = str(metadata.get("primary_evidence_role") or "unassigned").lower()
        if role not in PAPER_ROLES:
            report["warnings"].append(
                f"invalid evidence role {role!r} for {paper_id}; using unassigned"
            )
            role = "unassigned"
        elif not metadata.get("primary_evidence_role"):
            report["warnings"].append(
                f"paper {paper_id} has no evidence role; using unassigned"
            )
        output.append(
            {
                "note_path": _relative(workspace, note.canonical),
                "paper_id": paper_id,
                "title": metadata.get("title"),
                "authors": metadata.get("authors"),
                "year": metadata.get("year"),
                "venue": metadata.get("venue") or metadata.get("publication"),
                "doi": metadata.get("doi"),
                "arxiv_id": metadata.get("arxiv_id"),
                "public_url": metadata.get("public_url") or metadata.get("url"),
                "version": metadata.get("version"),
                "version_group": metadata.get("version_group"),
                "access": metadata.get("access") or metadata.get("access_level"),
                "reading_status": metadata.get("reading_status"),
                "discovery_route": metadata.get("discovery_route"),
                "metadata_check_status": "NOT_CHECKED",
                "research_line_ids": line_ids,
                "primary_evidence_role": role,
                "inclusion_status": metadata.get("inclusion_status", "included"),
                "evidence_ids": metadata.get("evidence_ids", []),
            }
        )
    return output


def _technical_entries(
    workspace: Path,
    notes: list[_DiscoveredNote],
    report: dict[str, Any],
    task_line_id: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for note in notes:
        metadata = note.metadata
        if not metadata.get("title") or not (
            metadata.get("url") or metadata.get("public_url")
        ):
            report["warnings"].append(
                "ignored unidentifiable technical Markdown: "
                f"{_relative(workspace, note.canonical)}"
            )
            continue
        source_id = stable_technical_id(metadata)
        proposed = str(metadata.get("source_id") or "").strip()
        if proposed and proposed != source_id:
            report["warnings"].append(
                f"ignored agent-proposed technical ID {proposed}; using {source_id}"
            )
        raw_lines = metadata.get("research_line_ids")
        line_ids = (
            [str(value) for value in raw_lines if str(value).strip()]
            if isinstance(raw_lines, list)
            else []
        )
        if not line_ids and task_line_id not in {"", "NOT_APPLICABLE"}:
            line_ids = [task_line_id]
        support_role = str(metadata.get("support_role") or "unassigned").lower()
        if support_role not in TECHNICAL_ROLES:
            report["warnings"].append(
                f"invalid technical support role {support_role!r} for {source_id}; "
                "using unassigned"
            )
            support_role = "unassigned"
        output.append(
            {
                "note_path": _relative(workspace, note.canonical),
                "source_id": source_id,
                "title": metadata.get("title"),
                "organization": metadata.get("organization")
                or metadata.get("author_organization"),
                "resource_type": metadata.get("resource_type"),
                "url": metadata.get("url") or metadata.get("public_url"),
                "publication_date": metadata.get("publication_date")
                or metadata.get("updated_at"),
                "verification_status": metadata.get("verification_status")
                or metadata.get("status"),
                "research_line_ids": line_ids,
                "support_role": support_role,
                "inclusion_status": metadata.get("inclusion_status", "included"),
            }
        )
    return output


def _resolve_finding_path(
    workspace: Path,
    value: Any,
    papers: list[dict[str, Any]],
    report: dict[str, Any],
) -> str | None:
    raw = str(value or "").replace("\\", "/").strip()
    by_path = {str(item["note_path"]): item for item in papers}
    if raw in by_path:
        return raw
    matches = [path for path in by_path if Path(path).name == Path(raw).name]
    if len(matches) == 1:
        report["warnings"].append(
            f"recovered metadata finding basename {raw!r} as {matches[0]}"
        )
        report["unresolved_paths"] = [
            value for value in report["unresolved_paths"] if value != raw
        ]
        return matches[0]
    if raw:
        report["unresolved_paths"].append(raw)
        if len(matches) > 1:
            report["warnings"].append(
                f"ambiguous metadata finding basename was not guessed: {raw}"
            )
    return None


def _read_jsonl(path: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    output: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            report["warnings"].append(
                f"ignored invalid JSONL line {number} in {path.name}"
            )
            continue
        if value == []:
            continue
        if not isinstance(value, dict):
            report["warnings"].append(
                f"ignored non-object JSONL line {number} in {path.name}"
            )
            continue
        output.append(value)
    return output


def _metadata_audit(
    workspace: Path,
    paths: Any,
    papers: list[dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    findings = _read_jsonl(paths.metadata_findings, report)
    legacy = _json_object(paths.audit_dir / "metadata_check.json")
    if legacy:
        legacy_items = legacy.get("items", [])
        if isinstance(legacy_items, list):
            findings.extend(item for item in legacy_items if isinstance(item, dict))
    if legacy and legacy.get("producer") != "slrharness":
        if isinstance(legacy.get("items"), list):
            report["imported_legacy_artifacts"].append(
                _relative(workspace, paths.audit_dir / "metadata_check.json")
            )
        for field in (
            "task_id",
            "paper_count",
            "checked_count",
            "passed_count",
            "corrected_count",
            "unresolved_count",
            "overall_status",
        ):
            if field in legacy:
                report["ignored_agent_counts"].append(
                    {"artifact": "metadata_check.json", "field": field}
                )
    latest: dict[str, dict[str, Any]] = {}
    for finding in findings:
        canonical = _resolve_finding_path(
            workspace, finding.get("note_path"), papers, report
        )
        if canonical:
            latest[canonical] = finding
    items: list[dict[str, Any]] = []
    for paper in papers:
        path = str(paper["note_path"])
        finding = latest.get(path, {})
        status = str(finding.get("status") or "NOT_CHECKED").upper()
        if status not in METADATA_STATUSES:
            report["warnings"].append(
                f"invalid metadata status {status!r} for {path}; using NOT_CHECKED"
            )
            status = "NOT_CHECKED"
        paper["metadata_check_status"] = status
        items.append(
            {
                "note_path": path,
                "paper_id": paper["paper_id"],
                "status": status,
                "checked_fields": finding.get("checked_fields", {}),
                "remaining_uncertainties": finding.get(
                    "remaining_uncertainties", []
                ),
            }
        )
    checked = sum(item["status"] != "NOT_CHECKED" for item in items)
    passed = sum(item["status"] == "PASS" for item in items)
    corrected = sum(item["status"] == "CORRECTED" for item in items)
    unresolved = sum(
        item["status"] in {"UNRESOLVED", "NOT_CHECKED"} for item in items
    )
    overall = "PASS" if unresolved == 0 else "PARTIAL"
    return {
        "schema_version": SCHEMA_VERSION,
        "producer": "slrharness",
        "task_id": paths.task_id,
        "checked_at": _now(),
        "overall_status": overall,
        "paper_count": len(papers),
        "checked_count": checked,
        "passed_count": passed,
        "corrected_count": corrected,
        "unresolved_count": unresolved,
        "items": items,
    }


def _pending_corrections(path: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for request in _read_jsonl(path, report):
        request_id = str(request.get("request_id") or "")
        if request_id:
            latest[request_id] = request
    return [
        value
        for value in latest.values()
        if str(value.get("status") or "").lower() == "pending"
    ]


def _prioritization(
    paths: Any,
    agent_manifest: dict[str, Any] | None,
    report: dict[str, Any],
) -> dict[str, Any]:
    observation_path = paths.audit_dir / "coordinator_observations.json"
    observations = _json_object(observation_path)
    if observation_path.is_file() and observations is None:
        report["warnings"].append(
            "malformed coordinator observations were ignored"
        )
    source = observations or (
        agent_manifest.get("prioritization") if agent_manifest else None
    )
    value = dict(source) if isinstance(source, dict) else {}
    task_context = (
        _json_object(paths.task_contract)
        or _json_object(paths.task_state)
        or {}
    )
    line_id = str(
        value.get("research_line_id")
        or task_context.get("research_line_id")
        or ""
    )
    value["research_line_id"] = line_id or "NOT_APPLICABLE"
    records = value.pop("paper_role_records", None)
    if isinstance(records, list):
        roles: dict[str, str] = {}
        for item in records:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "unassigned").lower()
            roles[str(item.get("paper_id") or "")] = (
                role if role in PAPER_ROLES else "unassigned"
            )
        value["paper_roles"] = {key: role for key, role in roles.items() if key}
    elif not isinstance(value.get("paper_roles"), dict):
        if value.get("paper_roles") is not None:
            report["warnings"].append(
                "malformed coordinator paper roles were ignored"
            )
        value["paper_roles"] = {}
    return value


def build_checkpoint_inventory(paths: Any) -> dict[str, Any]:
    """Inventory persisted work without changing it."""
    workspace = paths.artifact_root.parents[
        len(Path(paths.topic_path).parts) - 1
    ]
    paper_notes: list[str] = []
    for path in paths.paper_dir.rglob("*.md"):
        if path.name.lower() in GENERATED_NOTE_NAMES:
            continue
        try:
            metadata, body = parse_frontmatter(path)
        except OSError:
            continue
        if _looks_like_paper(metadata, body) and _paper_identity(metadata):
            paper_notes.append(_relative(workspace, path))
    paper_notes.sort()
    technical_notes: list[str] = []
    for path in paths.technical_dir.rglob("*.md"):
        if path.name.lower() in GENERATED_NOTE_NAMES:
            continue
        try:
            metadata, body = parse_frontmatter(path)
        except OSError:
            continue
        if (
            _looks_like_technical(metadata, body)
            and metadata.get("title")
            and (metadata.get("url") or metadata.get("public_url"))
        ):
            technical_notes.append(_relative(workspace, path))
    technical_notes.sort()
    finding_count = 0
    if paths.metadata_findings.is_file():
        for line in paths.metadata_findings.read_text(encoding="utf-8").splitlines():
            try:
                finding_count += isinstance(json.loads(line), dict)
            except json.JSONDecodeError:
                continue
    temporary_report = {"warnings": []}
    pending = _pending_corrections(paths.correction_requests, temporary_report)
    synthesis_present = (
        paths.synthesis.is_file()
        and len(paths.synthesis.read_text(encoding="utf-8").strip()) >= 200
    )
    prioritization_present = False
    if paths.synthesis.is_file():
        prioritization_present = "Scope-Driven Research-Line Assessment" in (
            paths.synthesis.read_text(encoding="utf-8")
        )
    prioritization_present = prioritization_present or (
        paths.audit_dir / "coordinator_observations.json"
    ).is_file()
    missing: list[str] = []
    paper_no_result_path = paths.paper_dir / "NO_RESULTS.md"
    technical_no_result_path = paths.technical_dir / "NO_RESULTS.md"
    paper_no_results = paper_no_result_path.is_file() and (
        paper_no_result_path.resolve().is_relative_to(paths.paper_dir.resolve())
    )
    technical_no_results = technical_no_result_path.is_file() and (
        technical_no_result_path.resolve().is_relative_to(paths.technical_dir.resolve())
    )
    if paper_no_result_path.is_file() and not paper_no_results:
        report["errors"].append("paper NO_RESULTS.md escapes its canonical directory")
    if technical_no_result_path.is_file() and not technical_no_results:
        report["errors"].append(
            "technical NO_RESULTS.md escapes its canonical directory"
        )
    if not paper_notes and not paper_no_results:
        missing.append("academic_research")
    elif paper_notes and finding_count == 0:
        missing.append("metadata_check")
    if pending:
        missing.append("corrections")
    if not technical_notes and not technical_no_results:
        missing.append("technical_research")
    if not synthesis_present:
        missing.append("topic_synthesis")
    return {
        "paper_notes": paper_notes,
        "technical_notes": technical_notes,
        "paper_no_results": paper_no_results,
        "technical_no_results": technical_no_results,
        "metadata_findings_count": finding_count,
        "pending_correction_count": len(pending),
        "synthesis_present": synthesis_present,
        "prioritization_present": prioritization_present,
        "missing_steps": missing,
    }


def _index_markdown(title: str, rows: list[dict[str, Any]], id_key: str) -> str:
    lines = [f"# {title}", "", "| ID | Title | Note |", "|---|---|---|"]
    for item in rows:
        lines.append(
            f"| {item.get(id_key, '')} | {item.get('title', '')} | "
            f"{item.get('note_path', '')} |"
        )
    return "\n".join(lines) + "\n"


def compile_topic_artifacts(
    workspace: Path,
    paths: Any,
    config: Any,
    process_result: Any | None,
    *,
    dry_run: bool = False,
) -> TopicCompilationResult:
    """Discover evidence and generate canonical indexes, audit, and manifest."""
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "producer": "slrharness",
        "task_id": paths.task_id,
        "discovered_files": [],
        "path_repairs": [],
        "generated_ids": [],
        "ignored_agent_counts": [],
        "imported_legacy_artifacts": [],
        "unresolved_paths": [],
        "warnings": [],
        "errors": [],
    }
    agent_manifest = _json_object(paths.manifest)
    if paths.manifest.is_file() and agent_manifest is None:
        report["warnings"].append(
            "malformed agent manifest was ignored and rebuilt"
        )
    if agent_manifest and agent_manifest.get("producer") != "slrharness":
        report["imported_legacy_artifacts"].append(
            _relative(workspace, paths.manifest)
        )
        report["warnings"].append(
            "agent-authored manifest was imported as observations and rebuilt"
        )
        if agent_manifest.get("task_id") not in {None, paths.task_id}:
            report["warnings"].append(
                "ignored agent manifest task_id; used program task_id"
            )
        for field in ("task_id", "paper_count", "source_count"):
            if field in agent_manifest:
                report["ignored_agent_counts"].append(
                    {"artifact": "coordinator_manifest.json", "field": field}
                )
        for section, field in (
            ("academic_worker", "paper_count"),
            ("technical_worker", "source_count"),
            ("metadata_checker", "checked_count"),
        ):
            value = agent_manifest.get(section)
            if isinstance(value, dict) and field in value:
                report["ignored_agent_counts"].append(
                    {
                        "artifact": "coordinator_manifest.json",
                        "field": f"{section}.{field}",
                    }
                )
        if not dry_run:
            atomic_write_json(
                paths.audit_dir / "agent_manifest_input.json", agent_manifest
            )
    for legacy_path, count_field in (
        (paths.paper_dir / "index.json", "paper_count"),
        (paths.technical_dir / "index.json", "source_count"),
    ):
        legacy_index = _json_object(legacy_path)
        if legacy_index and legacy_index.get("producer") != "slrharness":
            report["imported_legacy_artifacts"].append(
                _relative(workspace, legacy_path)
            )
            for field in ("task_id", count_field):
                if field in legacy_index:
                    report["ignored_agent_counts"].append(
                        {"artifact": legacy_path.name, "field": field}
                    )
    notes = _discover_notes(workspace, paths, report, dry_run=dry_run)
    task_context = (
        _json_object(paths.task_contract)
        or _json_object(paths.task_state)
        or {}
    )
    task_line_id = str(task_context.get("research_line_id") or "")
    papers = _paper_entries(
        workspace,
        [note for note in notes if note.kind == "paper"],
        report,
        task_line_id,
        dry_run=dry_run,
    )
    technical = _technical_entries(
        workspace,
        [note for note in notes if note.kind == "technical"],
        report,
        task_line_id,
    )
    paper_no_result_path = paths.paper_dir / "NO_RESULTS.md"
    technical_no_result_path = paths.technical_dir / "NO_RESULTS.md"
    paper_no_results = paper_no_result_path.is_file()
    technical_no_results = technical_no_result_path.is_file()
    if not papers and not paper_no_results:
        report["errors"].append("no recognizable paper notes or paper NO_RESULTS.md")
    if not technical and not technical_no_results:
        report["errors"].append(
            "no recognizable technical notes or technical NO_RESULTS.md"
        )
    synthesis_inside_topics = paths.synthesis.resolve().is_relative_to(
        (workspace / "topics").resolve()
    )
    if paths.synthesis.is_file() and not synthesis_inside_topics:
        report["errors"].append("topic synthesis path escapes the topics tree")
    synthesis_valid = (
        synthesis_inside_topics
        and paths.synthesis.is_file()
        and len(paths.synthesis.read_text(encoding="utf-8").strip()) >= 200
    )
    if not synthesis_valid:
        report["errors"].append("topic synthesis is missing or too short")
    audit = _metadata_audit(workspace, paths, papers, report)
    pending = _pending_corrections(paths.correction_requests, report)
    prioritization = _prioritization(paths, agent_manifest, report)
    prior_process = (
        agent_manifest.get("process", {})
        if agent_manifest and agent_manifest.get("producer") == "slrharness"
        else {}
    )
    if not isinstance(prior_process, dict):
        prior_process = {}
    process_timed_out = bool(
        getattr(process_result, "timed_out", prior_process.get("timed_out", False))
    )
    process_exit_code = getattr(
        process_result, "exit_code", prior_process.get("exit_code")
    )
    process_completed = bool(
        getattr(
            process_result,
            "completed",
            not process_timed_out and process_exit_code in {None, 0},
        )
    )
    process_failed = bool(
        process_timed_out
        or not process_completed
        or process_exit_code not in {None, 0}
    )
    accept_after_failure = bool(
        getattr(config, "accept_valid_artifacts_after_process_failure", True)
    )
    if process_failed and not accept_after_failure:
        report["errors"].append(
            "process failed and accepting recovered artifacts is disabled"
        )
    if process_timed_out:
        report["warnings"].append(
            "coordinator timed out; persisted artifacts recovered"
        )
    elif process_exit_code not in {None, 0}:
        report["warnings"].append(
            "coordinator exited nonzero; persisted artifacts recovered"
        )
    material_partial = bool(
        audit["overall_status"] == "PARTIAL"
        or pending
        or process_failed
        or report["path_repairs"]
        or report["unresolved_paths"]
    )
    status = (
        "FAILED"
        if report["errors"]
        else "PARTIAL"
        if material_partial
        else "COMPLETE"
    )
    paper_index_path = (
        paths.paper_dir / "index.json"
        if papers
        else paper_no_result_path
        if paper_no_results
        else paths.paper_dir / "index.json"
    )
    technical_index_path = (
        paths.technical_dir / "index.json"
        if technical
        else technical_no_result_path
        if technical_no_results
        else paths.technical_dir / "index.json"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "producer": "slrharness",
        "task_id": paths.task_id,
        "status": status,
        "topic_synthesis": _nominal_relative(workspace, paths.synthesis),
        "academic_worker": {
            "index_path": _relative(workspace, paper_index_path),
            "paper_count": len(papers),
        },
        "metadata_checker": {
            "audit_path": _relative(workspace, paths.audit_dir / "metadata_check.json"),
            "overall_status": audit["overall_status"],
        },
        "technical_worker": {
            "index_path": _relative(workspace, technical_index_path),
            "source_count": len(technical),
        },
        "prioritization": prioritization,
        "limitations": sorted(set(report["warnings"])),
        "normalization_warnings": sorted(set(report["warnings"])),
        "process": {
            "exit_code": process_exit_code,
            "timed_out": process_timed_out,
        },
        "completed_at": _now(),
    }
    if not dry_run:
        if papers:
            atomic_write_json(
                paths.paper_dir / "index.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "producer": "slrharness",
                    "task_id": paths.task_id,
                    "paper_count": len(papers),
                    "papers": papers,
                },
            )
            atomic_write_text(
                paths.paper_dir / "INDEX.md",
                _index_markdown("Academic Paper Notes", papers, "paper_id"),
            )
        if technical:
            atomic_write_json(
                paths.technical_dir / "index.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "producer": "slrharness",
                    "task_id": paths.task_id,
                    "source_count": len(technical),
                    "sources": technical,
                },
            )
            atomic_write_text(
                paths.technical_dir / "INDEX.md",
                _index_markdown("Technical Source Notes", technical, "source_id"),
            )
        atomic_write_json(paths.audit_dir / "metadata_check.json", audit)
        audit_lines = [
            "# Metadata Check",
            "",
            f"Overall status: {audit['overall_status']}",
            f"Papers: {audit['paper_count']}",
            f"Checked: {audit['checked_count']}",
            f"Unresolved: {audit['unresolved_count']}",
        ]
        atomic_write_text(
            paths.audit_dir / "metadata_check.md", "\n".join(audit_lines) + "\n"
        )
        checkpoint = build_checkpoint_inventory(paths)
        atomic_write_json(paths.checkpoint, checkpoint)
        report["warnings"] = sorted(set(report["warnings"]))
        report["errors"] = sorted(set(report["errors"]))
        atomic_write_json(paths.normalization_report, report)
        atomic_write_json(paths.manifest, manifest)
    else:
        checkpoint = build_checkpoint_inventory(paths)
    return TopicCompilationResult(
        compiled=not report["errors"],
        status=status,
        errors=list(report["errors"]),
        warnings=list(report["warnings"]),
        recovered_paths=list(report["path_repairs"]),
        paper_count=len(papers),
        technical_source_count=len(technical),
        metadata_status=str(audit["overall_status"]),
        manifest=manifest,
        checkpoint=checkpoint,
    )
