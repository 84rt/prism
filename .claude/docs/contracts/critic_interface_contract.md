# Red-Teaming Critic: Interface Contract

This document defines the interface between the Diagnostician and the Red-Teaming Critic. Both agents read this document. It specifies what the Diagnostician provides, what the Critic returns, and the format of each, across the Critic's three modes. This is the single source of truth for the contract; neither agent should redefine these structures independently.

The Critic exists to address one structural risk. A forensic investigation performs inference to the best explanation, and that inference selects the best of the **considered** hypotheses, which may all be false. Nothing internal to the inference detects the omission, so the countermeasure is an adversary who does not share the Diagnostician's priors rather than more care applied to the existing list.

---

## Modes

Read this table first. Nothing else in this document makes sense until you know which mode you are in.

| Mode | Called at | Receives | Must NOT receive | Returns |
|---|---|---|---|---|
| `candidate_critique` | Step 2, and at the end of each 3d | Transcript paths, Mapper briefing path, candidate ledger | Nothing withheld | Blind-spot analysis, assumption audit, proposed additional candidates |
| `diff_review` | Step 3a | Variant diffs, environment path, Mapper briefing path | The candidates, the discrimination matrix, the behaviour description | Confound analysis per diff |
| `prediction` | Step 4 | Full investigation log, leading explanation, list of untested surfaces | The Diagnostician's own prediction | A chosen factor and an ordinal prediction with reasoning |

Note the asymmetry in the "Must NOT receive" column. `candidate_critique` withholds nothing — a critic that generated its own candidate list independently would be a second Diagnostician — so its anchoring defence is within-pass sequencing instead. `diff_review` keeps full input-blinding, because there the Critic can do its whole job without knowing what the diffs are meant to test.

---

## Cross-family routing

The Critic is **cross-family by default, and this is a hard gate rather than a preference.** Its value comes from reasoning that is decorrelated from the Diagnostician's, so a same-family Critic degrades the one component that exists specifically to be independent.

- Every invocation passes `subagent_route: "openrouter"` and a non-Claude `subagent_model`. Without `OPENROUTER_API_KEY`, `/diagnose` does not start; it fails at Phase 0 rather than degrading.
- **An `OPENAI_API_KEY` does not satisfy the gate.** The runner implements exactly two routes — `direct` (Anthropic) and `openrouter` — by deliberate choice; there is no direct-OpenAI path for a sub-agent. If OpenRouter is capped mid-run with no cross-family alternative, the Diagnostician suspends the investigation (`diagnostician_responsibilities.md §Sub-Agent Failure Handling`) rather than running a same-family Critic.
- Thinking configuration is left to the runner's default, which disables extended thinking for non-Claude models on the OpenRouter route.

---

## Diagnostician → Critic: Request Format

### Mode (required)

One of `candidate_critique`, `diff_review`, `prediction`. It determines which further fields are required and which report sections come back.

An unknown mode, or a mode missing one of its required fields, is an **agent error (exit code 2)** rather than a degraded run. Mode-conditional requirements cannot be expressed in the CLI's flat required-field list, so they are validated when the prompt is built.

### Fields for `candidate_critique`

- **`transcript_paths`** (required) — an array of absolute paths to **plain-text** transcripts of the episode under investigation, produced by `scripts/dump_transcripts.py`. Never an `.eval` file: those are zip archives, and a Critic handed one reads nothing.
- **`mapper_report_path`** (required) — absolute path to the Mapper's briefing, written by the launcher at `artefacts/mapper/report_<tag>_mapper_1.md`.
- **`candidates`** (required) — the candidate ledger as JSON: each entry with `id`, `slug`, `statement`, `discriminating_signature`, `status`. **Produce it with `scripts/ledger_to_json.py`** (`uv run --no-project python scripts/ledger_to_json.py investigations/<name>/CANDIDATES.md -o <request dir>/candidates.json`), never by hand: it serialises the ledger's live sections and refuses, with the defects listed, to emit any live candidate whose *Statement* or *Discriminating signature* field is missing. A candidate written as prose otherwise reaches the Critic as two empty strings, and the Critic then critiques a blank — which has happened, and produced retirement advice that rested on the emptiness rather than the candidate. A refusal means the ledger entry needs rewriting in the §State Management field format before the request is sent.
- **`iteration`** (required) — integer. `0` for the step-2 invocation, then the iteration number.
- **`evidence_summary`** (optional) — for post-3d invocations: what the last round of experiments showed, stated mechanistically (rates and directions, not conclusions).

The Diagnostician can rely on the Critic reading the transcripts and writing `### Independent Reading` **before** reading the candidate ledger, and never revising that section afterwards. This ordering is the mode's only anchoring defence, and the report states that it was followed.

### Fields for `diff_review`

- **`diffs`** (required) — an array of `{"name": ..., "diff": ...}` objects.
- **`environment_path`** (required) — absolute path to the environment the diffs apply against, so the Critic can read the surrounding context.
- **`mapper_report_path`** (required) — absolute path to the Mapper's briefing.

**The Diagnostician must withhold what the diffs are meant to test** — no candidates, no discrimination matrix, no behaviour description. This blinding is the mode's entire value: a reviewer who knows the intended manipulation reads confounds as acceptable costs rather than as flaws. Sending any of the withheld fields silently converts the review into a much weaker one.

### Fields for `prediction`

- **`investigation_log_path`** (required) — absolute path to the full investigation log.
- **`leading_explanation`** (required) — JSON: the statement and its discriminating signature.
- **`untested_surfaces`** (required) — an array of surfaces from the Mapper's inventory, minus everything already varied.

**The Diagnostician withholds its own prediction.** The two derive independently from the same stated explanation, and the comparison is the point: agreement checks that the explanation is specific enough to generate a prediction at all, and disagreement localises where it is underspecified.

### Optional in every mode

- **`conformance_defects`** — an array of strings: the defect lines printed by `scripts/check_report.py` for the previous attempt at this same request.
- **`previous_report`** — the absolute path of that non-conformant report.

The two are sent only together, and their presence makes the invocation the contract's **one re-ask** (see §Conventions, *Conformance is checked mechanically*); the request is otherwise identical to the failed one. Never sent on a first attempt. Read the previous report and emit it again restructured into the active mode's exact headings, carrying its content across; re-derive only what the defects show was genuinely missing (a proposal lacking a discriminating signature gets one, written as its falsifier). One field without the other, or a `previous_report` that is not an existing file, is rejected as an agent error.

The request the Critic receives also carries, in every mode, the contract's absolute path (resolved from the scaffold root, so it is valid from any working directory) and a verbatim checklist of the active mode's report headings. Neither is a request field the Diagnostician supplies; the agent's prompt builder adds both.

---

## Critic → Diagnostician: Report Format

Emit **only the active mode's sections**, as level-3 markdown headings with verbatim text, in the order given below. A report carrying sections from a mode that was not requested is malformed, not thorough. The headings below are the strings the request's checklist carries and the strings `scripts/check_report.py` verifies.

**`candidate_critique` sections**

### Independent Reading

What the Critic noticed in the transcripts, unprompted — written before the candidate ledger was read and never revised in light of it. Includes an explicit statement that this order was followed.

This section is a datum in its own right, not a preamble: it is the only record in the pipeline of how the episode reads to someone who has not seen the Diagnostician's framing.

### Blind Spots

Regions of explanation-space the candidate list does not cover. Each entry names **why the list would systematically miss it** — a shared assumption, an artefact of how the behaviour was described, an over-reading of the chain-of-thought, a framing inherited from the eval's author.

### Assumption Audit

Assumptions the candidate list treats as settled — typically about the chain-of-thought's faithfulness, the behaviour's intentionality, the match between the eval's framing and the model's construal, and whether the behaviour has one cause or several.

### Proposed Additional Candidates

Each in the ledger's own format, with an `X<n>` id marking Critic origin: id, slug, statement, discriminating signature.

**The discriminating signature is required**, the same admissibility bar the Diagnostician is held to. A candidate stating no manipulation that would move it cannot be the subject of diagnostic evidence, so it can never be retired and will sit in the ledger indefinitely. `scripts/check_report.py` checks that every proposal in this section carries one; a proposal without a signature is a conformance defect and triggers the re-ask.

**Proposals are merged by signature on receipt.** The Critic is sent the ledger and must not re-propose a candidate already on it, under a new id or a new slug; the request says so next to the ledger. The Diagnostician, on merging, compares each proposal's signature with every live candidate's and records a match as a duplicate of that candidate (one line in the log, no new entry) rather than admitting it. A proposal that is a genuine sharpening of an existing candidate is handled the same way, with the sharpened wording folded into the existing entry and the Critic credited in its *Origin* field.

Returned empty and explicitly marked as empty when there is nothing to add.

### Candidates I Would Retire

Candidates the Critic judges should be dropped or merged, with reasons. Two grounds recur: a candidate that **redescribes** the behaviour rather than causing it, and a **pair no available manipulation can separate**, which is one candidate until something separates them.

The standard for retiring is disconfirming evidence or indistinguishability — never absence of supporting evidence, since early rejection of the unproven is self-sealing.

**`diff_review` sections**

### Summary

3–5 sentences, **Blockers first**: how many diffs were reviewed, how many carry Blockers, and whether the set has a shared problem.

### Per-Diff Analysis

For each diff: everything it changes beyond its apparent primary change — length, register, formatting, position, information content, implied stance. Then whether it could break the eval's coherence, and whether it leaks condition identity to the model.

Flagged with **Blocker** where a finding would make that diff's result uninterpretable.

### Cross-Diff Analysis

Whether the diffs *as a set* share a systematic co-variation — every treatment shorter than its control, every edit in the same paragraph, every variant touching one file whose other contents differ between arms. A shared co-variation confounds the whole round rather than any single variant and is invisible from any one diff.

### Inferred Purpose

The Critic's guess at what the set is testing, stated **last** and explicitly labelled a guess, with the cues that produced it.

A guess that lands is a warning rather than a success: whatever the Critic could infer from the diffs, the model under evaluation may be able to infer from the environment.

**`prediction` sections**

### Chosen Factor

Which untested surface the Critic selects, and why varying it is a genuine test rather than a restatement of something already shown.

### Prediction

Ordinal only: `↑`, `↓`, or `↔`, with the direction stated in words as well as the symbol.

**No point rates, no percentage predictions, no expected effect sizes.** A point prediction fails for reasons unrelated to the truth of the explanation that generated it, so it makes the test noisier rather than sharper.

### Reasoning

The derivation from the leading explanation to the prediction, in enough detail that the Diagnostician can check whether it derived the same thing from the same explanation. This is what makes a disagreement diagnostic: two derivations reaching opposite predictions localise the underspecification, whereas two bare predictions that differ show only that two agents disagree.

### What Would Falsify This

The observation that would count **against** the leading explanation — not merely fail to support it. Names the outcome and what it would rule out.

---

## Conventions

**Each invocation is self-contained.** One request, one report, no follow-up within the invocation, no memory across invocations. Continuity reaches the Critic only through the artefacts it is given: the ledger, the evidence summary, the log.

**The Critic never proposes running anything.** It critiques, it proposes candidates, and in `prediction` mode it names one factor and predicts an ordinal direction. It does not design variants, write diffs, specify conditions, or recommend an experimental programme — that work belongs to the Designer and the Diagnostician, and a Critic doing it has stopped being decorrelated from the design it is meant to check.

**Read-only, stdout-only.** The Critic never modifies, creates, or deletes files. Its report goes to stdout and is captured by the Diagnostician.

**Section headings are markdown, and their text is verbatim.** Emit each report section as a level-3 markdown heading (`### Independent Reading`, `### Blind Spots`, …) using the exact heading text, in the order given for the active mode. Plain-text section titles are not sufficient: the Diagnostician locates sections structurally, so a report whose content is perfect but whose headings are unmarked is a report it cannot reliably read. The request carries the active mode's headings as a checklist, so the exact strings are always in front of the Critic when it writes.

**Conformance is checked mechanically, with one re-ask.** Before reading a Critic report, the Diagnostician runs `uv run --no-project python scripts/check_report.py critic <saved report> --mode <mode>`. It verifies the headings above (present, verbatim, level 3, in order, nothing substituted) and, in `candidate_critique`, that every proposed candidate carries a discriminating signature; a non-zero exit lists the defects. On failure the Diagnostician re-invokes the Critic **once**, with the identical request plus `conformance_defects` set to those lines and `previous_report` set to the failed report's absolute path, and saves the second report under its own name. If the second report also fails, the Diagnostician does not ask again: it reads the report structurally from the first recognisable heading, records the non-conformance in the log as evidence that the guidance is underspecified for that model (per `subagent_invocation.md`), and — for a proposal still lacking a signature — either drops the proposal or authors the signature itself and records that authorship in the candidate's *Origin*, since a Diagnostician-authored falsifier partly undoes the decorrelation the Critic exists to provide and must not pass as the Critic's own.

**The report is the whole of stdout.** Everything printed is captured verbatim as the saved artefact, so begin directly with the active mode's first section heading — no preamble, no narration of readiness, no sign-off, and no horizontal rules outside sections. A leading "here is the report" line becomes a permanent part of a document that downstream agents read by structure.

**Ordinal predictions only, in every mode.** Wherever the Critic states an expected direction, it states `↑` / `↓` / `↔` / `n/a` and not a number.

**Blocker flag semantics.** Used in `diff_review`. A **Blocker** means the result would be uninterpretable if run as-is. Scope is local: a per-diff Blocker makes that diff unusable; a global Blocker means the round as designed is unviable. There is no Mitigable-vs-Caveat taxonomy; non-Blocker risks are described in prose and the Diagnostician weighs them.

**Asymmetric error preference, and its limit.** On confounds and leakage, over-flag rather than miss — the Diagnostician can discard an over-flagged confound cheaply and cannot recover one that was never raised. This does **not** extend to objections or candidates: a candidate list the Critic cannot fault comes back as one it cannot fault, with what evidence would change that.

---

## Non-negotiable requirements

The Critic's report **must** satisfy these commitments. They are non-negotiable because failures here are invisible downstream — the report looks like a discharged critique either way.

- **In `candidate_critique`, `### Independent Reading` is written before the candidate ledger is read, and never revised afterwards**, with the report stating that this order was followed. Read in the other order, the Critic's impressions are a paraphrase of the Diagnostician's framing, and the pipeline has gained a second voice agreeing with itself rather than an independent one.

- **Every proposed candidate carries a discriminating signature.** Without this bar the Critic's contribution is unfalsifiable storytelling that the Diagnostician can neither test nor retire.

- **In `diff_review`, `### Inferred Purpose` is written last**, after the per-diff and cross-diff analysis is complete. Written first, the guess becomes the frame the review is conducted in, and the mode's blinding is spent for nothing.
