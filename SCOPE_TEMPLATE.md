# Scope: {Theme Title}

## Research Question

{One clear, answerable research question. Use PICo for scoping reviews:
Population, Interest, Context. Use PICO for intervention reviews:
Population, Intervention, Comparison, Outcome.}

## Key Concepts & Search Terms

| Concept | Terms / Synonyms |
| ------- | ---------------- |
| {concept_1} | {term_a}, {term_b}, {term_c} |
| {concept_2} | {term_a}, {term_b} |

## Inclusion Criteria

- Publication type: {e.g., peer-reviewed journal articles, conference papers}
- Date range: {e.g., 2015–present}
- Language: {e.g., English}
- Domain: {e.g., machine learning, NLP}
- Other: {e.g., must report quantitative results}

## Exclusion Criteria

- {e.g., Preprints without peer review}
- {e.g., Papers without accessible full text}
- {e.g., Position papers or opinion pieces}

## Comparison Dimensions

| Dimension | Description | Value Type |
| --------- | ----------- | ---------- |
| {dim_1} | {what this captures} | {categorical, numeric, boolean, text} |
| {dim_2} | {what this captures} | {...} |

## Ranking & Grouping Criteria

How should items be ordered and grouped in `SUMMARY.md`? Fill in whichever
parts apply. The manager agent uses these to structure the synthesis.

### Grouping

{How to partition items into sections. Examples:
- Group by method family (pruning vs. quantization vs. distillation)
- Group by closeness to a target paper / reference method
- Group by application domain
- One group only (flat list)}

### Primary ordering (within each group)

{The main ranking criterion. Examples:
- Numeric: descending by "compression ratio" dimension
- Subjective: weighted score = 0.4 * "ease of access" + 0.3 * "data points" +
  0.3 * "is SOTA". Define how each factor is scored (e.g., ease of access:
  1 = public code + dataset, 0.5 = code only, 0 = neither).
- By date (newest first)
- By citation count
- By closeness to reference work: {cite reference}}

### Secondary ordering (tiebreaker)

{E.g., year descending, alphabetical by first author}

### Notes on subjective scoring

{If using a weighted combination of subjective factors, define the rubric
here clearly enough that a worker can extract a score from each source
without guessing. Example:

- Ease of access (0-1): 1 if code + dataset public, 0.5 if either, 0 if neither
- Data points (0-1): normalize log(dataset_size) to 0-1 range
- SOTA (0 or 1): 1 if best reported on any canonical benchmark, else 0}

## Target Databases

- [ ] Semantic Scholar
- [ ] arXiv
- [ ] PubMed
- [ ] Scopus
- [ ] IEEE Xplore
- [ ] Google Scholar
- [ ] DBLP
- [ ] Other: {...}

## Review Depth

{Choose one:}

- exhaustive — include all matching results
- targeted — top-N per query (N = {...})

## Notes

{Any additional context: motivation, prior related reviews, specific
angles of interest, things to deliberately exclude, etc.}
