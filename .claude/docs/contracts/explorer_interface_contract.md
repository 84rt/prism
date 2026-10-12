# Environment Explorer: Interface Contract

This document defines the interface between the orchestrator and the Environment Explorer. Both agents read this document. It specifies what the orchestrator provides, what the Explorer returns, and the format of each. This is the single source of truth for the contract; neither agent should redefine these structures independently.

---

## Orchestrator → Explorer: Request Format

Each request from the orchestrator to the Environment Explorer contains the following fields.

### Experiment description (required)

A plain-English statement of the experiment's purpose. One or two paragraphs describing what question the investigation is addressing and why.

### Hypothesis (required)

The specific hypothesis being tested. A good hypothesis names an independent variable, a dependent variable, and an expected direction of effect.

Unlike the Transcript Analyst, the Explorer **does** receive the hypothesis. There is no informational firewall here: the Explorer's job is precisely to identify where in the evaluation environment the hypothesised variable could be manipulated, and to flag the confounds such manipulation would introduce. This requires knowing what the hypothesis is.

### Environment path (required)

An absolute filesystem path to the directory containing the evaluation environment. Must exist and be readable. The CLI validates this before invocation.

### Constraints (optional)

A free-form string describing any orchestrator-side preferences that narrow or shape the Explorer's work this iteration. Examples: "focus on system prompt and scoring; skip the scaffold directory"; "environment is very large — prioritise prompts and scoring over utility scripts"; "prefer single-file changes this round".

This field is best-effort. The Explorer respects it where possible but does not treat it as binding. There is no tooling-level enforcement.

### Re-ask fields (optional; sent only together)

- **`conformance_defects`** — an array of strings: the defect lines `scripts/check_report.py explorer` printed for the previous attempt at this same request.
- **`previous_report`** — the absolute path of that non-conformant report.

Their presence makes the invocation the **one re-ask**; the request is otherwise identical to the failed one, and both are never sent on a first attempt. Read the previous report and emit it again restructured into the Report Format's exact headings below: carry its content across under the right headings, and re-read the environment only for a section the defects show was genuinely missing. A reformatting request never arrives through `constraints`. One field without the other, or a `previous_report` that is not an existing file, is rejected as an agent error.

---

## Explorer → Orchestrator: Report Format

Each report from the Environment Explorer to the orchestrator contains the following sections, in order. The summary comes first; the remaining sections provide supporting detail for drill-down.

### Summary

A 3–5 sentence overview of the exploration. State: a one-line characterisation of the eval environment, the proposed IV manipulation in one sentence, whether any Blockers have been flagged, and a confidence note. This section is interpretable without reading anything else; if the orchestrator stops reading here, it should know whether the proposed design is viable.

### Environment Summary & File Catalogue

A short narrative describing what the eval measures and how, followed by a table of files with their types and roles in the pipeline. The narrative should read as a developer's briefing on the environment — the condensed version an eval author would give a colleague stepping in for the first time. The file catalogue is structural: file path, type (system prompt, task config, scoring rubric, scaffold code, data, README, utility), and role in the pipeline.

For large environments where full cataloguing is impractical, prioritise files on the prompt → task → scoring path and note any deprioritised directories explicitly. Do not silently skip files.

### Pipeline Model

An explicit trace of how the evaluation flows from input to score: prompt construction → task execution → scoring → aggregation. Identify which files implement each stage. This is the disciplined artefact that grounds every modification-site proposal — the orchestrator uses it to sanity-check that each proposed site actually influences the hypothesised variable.

### Modification Sites

For each site where a change could test the hypothesis:

- **Location**: file path, line range.
- **Current content**: the exact text at the site.
- **Rationale**: why modifying this site bears on the hypothesis.
- **Variants** (2–3, ranked most-minimal-first): each variant is a unified diff showing exact before/after text, plus the matched control that the variant implies. *Minimal* means altering only what is necessary to manipulate the hypothesised variable — no incidental rewording, reformatting, or restructuring.
- **Per-site risks**: confounds, cross-file dependencies, information leakage, or other concerns that attach specifically to this site or to particular variants. Flag any risk that would invalidate the experiment with **Blocker** (see *Conventions* below). Non-Blocker risks are described in prose.

Matched controls must be explicit for every variant; never implied.

### Variant Dependencies *(only if applicable)*

Hard constraints between variants across different sites — cases where choosing a particular variant at one site requires a corresponding variant at another site to preserve eval coherence (for example, a change to a task's system prompt that requires a coordinated change to the scoring rubric to avoid breaking the pipeline).

This section documents dependencies as information, not as recommendations. It tells the orchestrator which combinations of variants are coherent and which would break the eval; the orchestrator decides which coherent combinations to run.

Omit this section entirely when no such cross-site dependencies exist.

### Global Risk Assessment

Risks that do not attach to any specific modification site but affect the experimental design as a whole. Organise under these subheadings where applicable:

- **Confounds** — design-level co-variation between the hypothesised IV and other properties.
- **Cross-condition interactions** — effects that would differ between conditions even absent the manipulation.
- **Information leakage** — aspects of the design that could reveal the experimental condition to the model (e.g., condition labels visible in the prompt, formatting differences between arms).
- **Scoring / scaffolding** — risks arising from the scoring logic or scaffold code that affect all conditions.
- **Ecological validity** — concerns about how well the manipulated environment resembles realistic use.

Flag any risk that would invalidate the experiment with **Blocker**. Non-Blocker risks are described in prose.

### Uncertainty & Open Questions

A bulleted list of specific items the Explorer could not resolve from static reading — files whose role is ambiguous, diffs whose applicability depends on runtime data, hypotheses that admit multiple operationalisations, dependencies that require clarification from the user.

Each item should name what the orchestrator needs to do to resolve it (read a specific file, ask the user, run a cheap sanity check). The orchestrator must resolve these items before applying modifications.

---

## Conventions

**Each invocation is self-contained.** Every delegation from the orchestrator to the Explorer is a single-shot interaction: one request, one report, no follow-up within the same invocation. The Explorer has no persistent memory across invocations; it builds its understanding of the environment from scratch each time. If the orchestrator needs a different exploration, it constructs a new request.

**Read-only, stdout-only.** The Explorer never modifies, creates, or deletes files. Its report is written to stdout; the detached launcher (`scripts/launch_subagent.py`) captures it to the per-invocation path `<artefacts-dir>/explorer/report_<tag>_explorer_<n>.md`, with no separate save step.

**Section headings are markdown, and their text is verbatim.** Emit each report section as a markdown heading at the level shown in the Report Format above (`### Summary`, `### Pipeline Model`, …), using the exact heading text, in the order given. Plain-text section titles are not sufficient: the orchestrator locates sections structurally, so a report whose content is perfect but whose headings are unmarked is a report it cannot reliably read. This is stated explicitly rather than left to be inferred from the formatting of this document.

**The report is the whole of stdout.** Everything printed is captured verbatim as the saved artefact, so begin directly with the first section heading — no preamble, no narration of readiness, no sign-off, and no horizontal rules outside sections. A leading "here is the report" line becomes a permanent part of a document that downstream agents read by structure.

**Diff notation.** Use unified diff format with `-` / `+` line markers and sufficient context lines for the diff to apply unambiguously. Include the file path and line range for each diff so the orchestrator can apply it mechanically.

**Condition naming.** When the orchestrator names conditions (downstream of the Explorer), the names must be filesystem-safe `snake_case` identifiers (e.g., `control`, `treatment_explicit_goal`). This convention is recorded here because the Explorer's per-site rationale may reference the conceptual role a variant plays under a particular naming scheme. These descriptive names are the orchestrator's own: before anything runs, each condition is given an opaque execution id (`c_<4 hex>`), and only the id names files, directories and logs. So never write a condition's name, or a role word such as `control` or `treatment`, into a diff — neither into file contents nor into the name of a file the diff creates.

**Blocker flag semantics.** A **Blocker** attaches to a specific site, variant, condition, or global risk and means: this would invalidate the experiment if the design went ahead as-is. Scope is local:

- A per-site Blocker attaches to a specific variant and makes that variant unusable; other variants at the same site may be fine.
- A Blocker on a condition means that particular condition is unusable; other conditions may still be runnable.
- A global Blocker means the design as a whole is unviable — redesign or hypothesis revision is required before any condition can be run.

There is no Mitigable-vs-Caveat taxonomy. Non-Blocker risks are described in prose. The orchestrator decides, with full context of budget and iteration history, how to weight non-Blocker risks.

**Asymmetric error preference.** On confounds and uncertainty, prefer false positives to false negatives: when uncertain whether something is a confound, flag it rather than omit; when uncertain whether a file's role matters, catalogue it with a note rather than skip; under-claim certainty rather than overclaim. The scaffold downstream can recover from the Explorer over-flagging; it cannot recover from the Explorer silently missing a confound or papering over ambiguity.

---

## Non-negotiable requirements

The Explorer's report **must** satisfy these commitments. They are non-negotiable because failures on these dimensions propagate silently through the downstream pipeline and cannot be recovered by subsequent stages.

- **IV isolation.** For each proposed variant, the Explorer must identify whether the diff manipulates only the hypothesised independent variable. Incidental co-variation (formatting, length, position, register) must be either designed out of the variant or named as a per-site confound. A variant whose diff changes more than the hypothesised variable without explicit flagging is a silent failure.

- **Calibrated confound detection.** The Explorer must surface confounds, information leakage, cross-file dependencies, and scoring/scaffolding risks at both the per-site and global levels. Blocker flags must be applied where applicable. A risk that would invalidate the experiment and is not flagged as Blocker is a silent failure.

- **Honest uncertainty.** Ambiguity about file roles, diff applicability, or hypothesis operationalisation must be enumerated explicitly in *Uncertainty & Open Questions*. Hedging prose in the body of the report does not substitute. Confident narration over genuinely ambiguous ground is a silent failure.
