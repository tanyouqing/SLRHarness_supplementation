# Scope: Agent Memory for LLM-Based Autonomous Agents

## Research Question

What memory architectures, mechanisms, and learning methods enable LLM-powered autonomous agents to store, retrieve, and utilize information across tasks and time?

## Key Concepts & Search Terms

The following are seed concepts taken from the research specification. They are not an exhaustive or mandatory search strategy.

| Concept | Terms / Synonyms |
| ------- | ---------------- |
| Agent memory | "agent memory", "memory for LLM-based autonomous agents", "memory modules for LLM-based agents" |
| Memory-enabled LLM systems | "memory-augmented LLMs", "memory-centric agent frameworks" |
| Memory mechanisms | "memory architecture", "memory mechanism", storage, retrieval, update, utilization, "learning method" |
| Named examples | MemGPT, Voyager |

The reviewing agent may refine queries or use other terms when helpful, provided that the resulting papers remain within the scope below.

## Inclusion Criteria

- The work centrally concerns memory for an LLM-based or LLM-powered autonomous agent.
- The work studies at least one of the following: a memory module for an LLM-based agent, a memory-augmented LLM used for agent behavior, or a memory-centric agent framework.
- The work contributes evidence or technical detail about memory architecture, storage, retrieval, update, utilization, or learning across tasks or time.
- The curated set should cover foundational works and recent influential works in this direction.
- The source must contain enough bibliographic information to be uniquely identified.
- No publication-date range, publication-type restriction, or source-language restriction is prescribed by this scope.

## Exclusion Criteria

- Traditional reinforcement-learning replay buffers that are not part of an LLM-based agent memory system.
- Non-LLM cognitive architectures.
- Neuroscience studies of memory.
- Work in which memory for an LLM-based agent is only incidental and is not a central contribution or subject of analysis.

## Comparison Dimensions

For every included paper, extract each dimension insofar as the paper reports it. Use `not reported` when the paper does not provide the information. Use `[UNVERIFIED]` when a fact could not be verified from the available evidence.

| Dimension | Description | Value Type |
| --------- | ----------- | ---------- |
| Citation and source identifiers | Title, authors, year, venue, link, DOI, or arXiv identifier, as available | Structured text |
| Problem and motivation | The problem or motivation addressed by the paper | Free text |
| Core contribution | The paper's principal memory-related method or contribution | Free text |
| Memory types | The memory types identified or implemented by the paper | Categorical / free text |
| Memory architecture | How memory is organized and connected to the LLM-powered agent | Free text |
| Storage mechanism | How information is stored | Free text |
| Retrieval mechanism | How stored information is selected or retrieved | Free text |
| Update mechanism | How memory is added, changed, consolidated, or removed | Free text |
| Utilization mechanism | How retrieved memory affects agent behavior or generation | Free text |
| Learning method | Any reported method for learning or adapting memory behavior | Free text / not reported |
| Agent framework | The agent framework or system used in evaluation | Categorical / free text |
| Benchmarks and datasets | Evaluation benchmarks, tasks, or datasets | Structured text |
| LLM and configuration | Model and relevant reported configuration | Structured text |
| Evaluation metrics | Metrics and evaluation protocols | Structured text |
| Baselines | Compared systems or methods | Structured text |
| Key quantitative results | Specific reported numerical results, with their experimental context | Numeric / structured text |
| Limitations | Limitations stated by the authors or clearly inferable from the reported evidence | Free text |
| Verification status | Whether the extracted information was verified and what access limitations applied | Categorical / free text |

## Ranking & Grouping Criteria

### Grouping

No grouping scheme is prescribed. The reviewing agent may organize existing research chronologically, by method family, or by another scheme it considers effective. The chosen scheme must be stated clearly and must support the comparisons required in the final review.

### Primary Ordering

No paper-level ranking or numeric scoring is prescribed. The survey must nevertheless include both foundational works and recent influential works, as required by the research specification.

### Secondary Ordering

Not prescribed.

### Notes on Subjective Scoring

Do not invent a quality, importance, novelty, or influence score. If the review uses terms such as `foundational` or `influential`, explain the evidence or role supporting that characterization.

## Target Databases

No databases are prescribed. The reviewing agent may choose any search tools, databases, primary-source repositories, or workflows it considers effective. Primary sources should be prioritized over secondhand summaries.

## Review Depth

- Curate 15–30 papers, or the maximum number that can reasonably be covered.
- Produce one standalone persisted note for every paper included in the review.
- The reviewing agent may choose the reading depth for each paper, but every note must satisfy all applicable comparison dimensions and deliverable requirements below.

## Required Deliverables

All deliverable content must be in English.

### D1. Paper Survey List (`paper_list.md`)

Produce a curated paper list covering the direction. Each entry must contain enough bibliographic detail to be uniquely identifiable, including title, authors, year, venue, and link or arXiv identifier insofar as available.

### D2. Per-Paper Reading Notes (`topics/papers/<paper-slug>.md`)

Produce one standalone persisted note for every paper in the review. Each note must capture all applicable comparison dimensions defined above and must be useful without consulting the final report. Notes that exist only in conversation or agent memory do not satisfy this deliverable.

### D3. Final Literature Review (`literature_review.md`)

The final review must contain the following five clearly labeled sections in this order:

1. **Terminology & Scope**
   - A glossary of domain-specific terms with definitions.
   - A mapping of synonyms or alternative terms used across papers for the same concepts.
   - An overview of the field's scope and subareas.
2. **Importance & Motivation of the Direction**
   - Why this research direction matters.
   - The key open problems that motivate work in it.
3. **Existing Research: Motivations & Methodologies**
   - An organized account of prior work using a scheme chosen by the reviewing agent.
   - A summary comparison table across papers.
   - For each major line of work: motivation, methodology, and key findings.
4. **Differences & Establishable Consensus**
   - A systematic comparison of agent frameworks, benchmarks, model or LLM configurations, evaluation protocols, and metrics across papers.
   - Findings on which multiple independent works agree.
   - Notable contradictions or inconsistencies in the literature.
5. **Potential Future Directions**
   - Concrete directions derived from limitations or gaps in existing work.
   - Each direction justified by references to the papers whose limitations motivate it.

Include a full reference list at the end of `literature_review.md`.

## Acceptance Criteria

Before declaring completion, re-read all deliverables against these criteria and fix any gaps:

1. **Completeness**: `paper_list.md`, all per-paper notes, and `literature_review.md` exist as persisted files; every listed paper has a corresponding note; all five required sections are present in the required order.
2. **Traceability**: Every non-trivial claim in `literature_review.md` is traceable to a specific paper using author-year attribution, and a full reference list is included.
3. **Fidelity**: Do not fabricate citations, numbers, or results. Mark facts that could not be verified as `[UNVERIFIED]`. State which parts rely on incomplete information when access is limited.
4. **Self-check**: Re-read the deliverables and correct identified omissions or inconsistencies before completion.

## Notes

- The reviewing agent chooses its search strategy, paper-selection process, reading depth, and internal workflow.
- The reviewing agent may refine, merge, or split intermediate artifacts as long as the final persisted deliverables satisfy this scope.
- Findings and future directions must be grounded in the reviewed primary sources rather than assumed in advance.
