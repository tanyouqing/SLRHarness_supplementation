# Artifact contracts

JSON contracts use schema version `1.0`. A missing version is read as legacy v1
with a warning; an unknown version is an error. Manifest paths are
workspace-relative and containment-checked.

| Artifact | Producer | Consumer | Ownership | Required contract / validation | Resume |
|---|---|---|---|---|---|
| `SLR_STATE.json` | Orchestrator | Orchestrator, status | Program | scope status, revision, approval gate, finalization state | atomic source of lifecycle truth |
| `SCOPE_PROPOSAL.md` | Scope Agent | User | Agent content | substantive required headings | retry same revision if interrupted |
| `SCOPE_SOURCES.md` | Scope Agent | User, Manager | Agent content | attempts, access and limitations | archived per revision |
| `SCOPE.md` | approval code | Manager, all agents | Program copy | exact approved proposal | never agent-modified |
| `TASKS.md` | Manager; repair insertion by program | Orchestrator | Mixed, controlled | stable topic/gap path and checkbox syntax | completed tasks are not rerun |
| `topics/**/task.json` | Orchestrator | Orchestrator | Program | task ID, mode, round, attempt, process result | retry only incomplete task |
| paper note | Academic Worker | Checker, registry, Finalizer | Agent | canonical frontmatter/body template and evidence locators | retained and revalidated |
| `papers/index.json` | Academic Worker | Coordinator, registry | Agent | version, task ID, count, note paths | old versionless index warns |
| `technical_sources/index.json` | Technical Worker | Coordinator, registry | Agent | version, task ID, count, source paths | no-result record allowed |
| `audits/correction_requests.jsonl` | Checker/Worker | Coordinator | Agent append log | stable request ID; pending/resolved/unresolved | latest status wins; bounded rounds |
| `audits/metadata_check.json` | Checker | Coordinator, registry | Agent content, program validation | counts and PASS/CORRECTED/UNRESOLVED items | recheck without content judging |
| `coordinator_manifest.json` | Coordinator | Orchestrator | Agent, written last | version, exact task ID/paths/counts/final status | artifact validation decides acceptance |
| topic synthesis `.md` | Coordinator | Manager, Finalizer | Agent | assigned canonical path and supporting traceability | supporting notes are not topics |
| `artifacts/SOURCE_REGISTRY.json` | Aggregator | audits, Finalizer | Program | stable paper/technical IDs, aliases, coverage | idempotently rebuilt |
| `PAPER_LIST.md`, `REFERENCES.md` | Aggregator | Finalizer | Program | generated only from registry | atomically rebuilt |
| `prefinal_audit.json` | Program + Manager semantics | repair/finalize | Mixed validated JSON | bounded gap IDs and repair decision | same gap is not duplicated |
| `SUMMARY.md` | Finalizer | User | Agent content | one canonical English five-section report | failed drafts retained |
| `final_audit.json` | Validator | Orchestrator, user | Program | sections, IDs, references, provenance, counts | only accepted audit permits COMPLETE |
