# Variant Designer: Interface Contract

This document defines the interface between the Diagnostician and the Variant Designer. Both agents read this document. It specifies what the Diagnostician provides, what the Designer returns, and the format of each. This is the single source of truth for the contract; neither agent should redefine these structures independently.

The Designer runs once per investigation iteration, after the Environment Mapper has produced a map and after the Diagnostician holds a set of candidate explanations. Its job is **discrimination**, not coverage. The question it answers is not *"where could I test this?"* but *"what edit would separate C1 from C2?"* — a variant that every live candidate agrees about carries no information however elegant its diff, and is wasted budget.

---

## Diagnostician → Designer: Request Format

### Mapper report path (required)

An absolute path to the Mapper's briefing, saved by the Diagnostician at `artefacts/mapper/report.md`.

The briefing is passed **by path, not inlined**, so the Designer reads the whole thing rather than a summary of it. The two sections it depends on most are `### Manipulable Surface Inventory` (the space of available edits, with their couplings) and `### Pipeline Model` (what each edit actually reaches). A variant at a surface the Designer cannot locate in the pipeline model is a variant it does not understand.

### Environment path (required)

An absolute filesystem path to the working copy the diffs must apply against. Must exist and be readable. The CLI validates this before invocation.

This is **the current iteration's copy**, which may be a fresh copy of the original eval or a variant from an earlier round that the Diagnostician wants extended. Diffs are written against the files as they stand here, with line numbers taken from here — never from the Mapper's briefing, whose locations are against the original.

### Environment delta (optional)

Present when, and only when, the environment supplied above is **not** a fresh copy of the original eval. A JSON array of the modifications already applied to it, each with a name and either a diff or a plain description:

```json
[{"name": "deadline_removed", "description": "The deadline sentence at prompt.md:34 was deleted."}]
```

**This field carries what was changed and never what it did.** No rates, no directions, no indication of which earlier variants were informative. That split is deliberate: the changes are needed for the report to be coherent, whereas their outcomes would bias the Designer toward repeating the kinds of edit that have already paid off, which is the one thing a fresh-each-time proposal agent is for.

Omit the field entirely when the environment is a fresh copy of the original. Its absence means exactly that, and no caveats need raising anywhere in the report.

**Why this field is not optional in practice.** The Mapper runs once, on the original eval, and its briefing is passed to the Designer every round. When the environment is a derived variant, that briefing describes a different tree — so the Designer, which is instructed to locate every surface it uses in the Mapper's pipeline model, will hit a mismatch it cannot explain. The available wrong readings are that the map is stale or the environment is broken, and the Designer has a section for exactly that thought, so the likely outcome of omitting this field is the investigation's own manipulation being reported as an environment defect, possibly with a **Blocker** attached to a sound variant.

Withholding it also buys nothing, since a Designer holding both the map and the environment can recover the delta by comparison. The choice is between stating it and leaving it derivable-but-unlabelled, and the second costs the same information while adding the confusion.

### Candidate explanations (required)

A JSON array of the live candidate explanations. Each element:

```json
{
    "id": "C1",
    "slug": "task_confusion",
    "statement": "The model read the failing test as the specification and satisfied it directly.",
    "discriminating_signature": "Removing the visible test file should collapse the behaviour; strengthening the docstring should not.",
    "status": "live"
}
```

Only `live` candidates are sent. Eliminated and merged candidates are absent, and the Designer should not ask about them — the ledger's history is the Diagnostician's business.

The `discriminating_signature` is the field the Designer works from hardest: it states what would move that candidate and in which direction, so the union of the signatures defines the design space, and the places where two signatures look alike define the hard problems.

### Variations tried (optional)

A JSON array of every variation the investigation has already run, across all rounds and branches — name plus a one-line description of what was varied. **Outcomes are stripped**, for the same reason they are stripped from the environment delta.

This is a broader set than the environment delta and serves a different purpose. The delta explains why the supplied environment differs from the Mapper's map; this list marks ground already covered, including variations from branches the current environment does not descend from. Entries will overlap, and that is expected — a modification applied to the current environment is both part of its delta and a variation already tried.

The Designer does not treat this as a prohibition. It may re-propose a surface listed here, but must then state why re-testing it in the *current* environment is discriminating — which is a real case rather than a loophole, since re-testing a factor after a dominant one has been ablated is exactly how a masked effect is found. What the field prevents is the accidental version: proposing to remove a sentence that an earlier round added, and paying for a measurement the investigation already holds.

### Regime (required)

Either `"ablate"` or `"amplify"`.

The Diagnostician derives it from the environment rate: a high rate calls for ablation (remove components, find what is *necessary*); a low rate calls for amplification (add components, find what is *sufficient*).

**This is a steer, not a directive.** The Designer works within the requested regime by default, but may propose an individual off-regime variant where it judges that variant more discriminating, marked with its own regime and a one-line reason. It never substitutes the regime for the round as a whole. The Diagnostician keeps the decision by choosing which diffs to apply, so a disagreement costs one rejected variant rather than a misdirected round.

Both agents reason from the same environment rate, so disagreement should be rare, and its kind matters. Either the Diagnostician used the point rate where the Designer computed an interval — which bites only near the headroom thresholds — or, more informatively, the regime has statistical headroom but no *surfaces* left, because everything the live candidates turn on has already been varied. The second is the Designer's knowledge and nobody else's, and appears in `### Uncertainty & Open Questions` as well as in the variant's reason.

### Environment rate (required)

The measured rate of the target behaviour **in the environment supplied above** — not the frozen step-1c baseline, unless the two happen to coincide because the environment is a fresh copy of the original:

```json
{"rate": 0.41, "n": 40}
```

The field is named `environment_rate` rather than `baseline_rate` because "baseline" means the frozen step-1c anchor everywhere else in the investigation, and the Designer needs the rate of the thing it is holding. Conflating the two would put the Wilson check and the regime derivation on the wrong number whenever the environment is a derived variant.

The Designer needs this to judge **headroom**. An ablation from 41% has room to fall; an ablation from 4% does not, and a null result there is a floor effect rather than evidence about any candidate. It computes a Wilson score interval on the rate and reports that interval in `### Summary`, so the headroom judgement behind the round's design is visible rather than implicit.

This is the only rate the Designer receives. It never sees a history of rates across rounds — the current number is what headroom depends on, and the sequence is what would tell it which kinds of edit have been working.

### Variant budget (required)

An integer: the maximum number of **discriminating** variants to propose this round.

Positive controls do not count against it. They calibrate the instrument rather than separating candidates, and a budget that could crowd them out would let a tight round silently buy an uninterpretable elimination. The Designer reports their number separately in `### Summary` so their cost stays visible.

The budget is a ceiling, not a target. Returning fewer with a reason is a correct outcome; padding to the number is not.

**A round is expected to contain several variants of mixed kinds.** No single experiment settles a candidate set, and the kinds are not interchangeable: a binary variant tests presence against absence, while a parameterised one tests a monotone response that a binary variant cannot reach at all. A round of only binary variants therefore leaves every monotone discriminating signature untested. This is an expectation rather than a quota — a set of one kind is legitimate when the live signatures genuinely only call for that kind — but the Designer says why in `### Summary` when it returns only one kind. The anti-padding rule above is about not inventing *non-discriminating* variants; it is not a reason to return one variant where three would each separate a different pair.

### Elimination targets (optional)

A JSON array of candidate IDs the Diagnostician intends to retire this round **on absence of evidence** — e.g. `["C3"]`.

When this field is present and non-empty, the Designer must also propose a **positive-control variant** for each named candidate: a variant in which that candidate's discriminating signature is known to be present, so that the test's ability to detect the signature at all can be established before its silence is read as evidence.

Absence of evidence retires a candidate only when the instrument has been shown to fire on that candidate's signature. Without a positive control, three ordinary confounds absorb any null — the setup may not have exercised the capability the test assumes, the model may not have acted on the state the test targets, or the manipulation may not have landed. A behavioural test that finds nothing is uninterpretable until one of those is closed off.

**Each named target costs a condition to run**, over and above the variant budget. Name a candidate here only when retiring it would change the leading explanation or move the boundary of the equivalence class the final report will state. A candidate already ranked well below the leader, whose retirement would change nothing the report says, is carried in the equivalence class rather than eliminated at the price of a control — the certifiable output is the best available account with its alternatives named, not a cleared field.

Omit the field when the round is not aiming to eliminate anything on absence.

### Constraints (optional)

A free-form string describing Diagnostician-side preferences that narrow or shape the round. Examples: *"single-file diffs only this round"*; *"do not touch the scorer"*; *"we have already varied the deadline clause — treat it as spent"*.

This field is best-effort. The Designer respects it where possible but does not treat it as binding. There is no tooling-level enforcement.

---

## Designer → Diagnostician: Report Format

Each report contains the following sections, in order.

### Summary

Opens with five fixed-format lines, in this order and with these exact labels, so the Diagnostician can extract them without parsing prose:

```
**Environment:** original | derived (<names of the applied modifications>)
**Discriminating variants:** <n> of <budget>, kinds: <binary | parameterised | factorial>
**Off-regime variants:** none | <variant names>
**Positive controls:** none | <n>, for <candidate ids>
**Headroom:** Wilson 95% interval on the environment rate
```

The `Environment` line is the Designer's acknowledgement of what it was given. When it reads `original` and the Diagnostician supplied a derived environment, the delta did not reach the Designer and the report's surface locations should not be trusted.

Then a 3–5 sentence narrative: which candidate pairs the set separates, whether any live candidate could not be discriminated from any other at all, and a confidence note. The narrative is interpretable without reading anything else.

### Proposed Variants

For each variant, in this structure:

- **Name** — a filesystem-safe `snake_case` identifier. It becomes a condition name downstream, so it must not encode which arm is treatment (see the leakage note in `### Confounds & Risks`).
- **Kind** — one of:
  - `binary` — the surface is present or absent.
  - `parameterised` — the surface takes ordered levels. **Names the levels to run**, three or more where the candidate predicts a monotone response.
  - `factorial` — two or more factors varied together. Name the set of factors and the specific cells to run.
  - `positive_control` — a variant in which a named candidate's signature is known to be present, supplied under the *Elimination targets* obligation above. Positive controls are not discriminating variants: they do **not** appear in the discrimination matrix, and they name which candidate's detectability they establish.
- **Regime** — `ablate` or `amplify`. Where it differs from the requested regime, a one-line reason why this variant is more discriminating than an in-regime alternative. Also listed on the `Off-regime variants` line in `### Summary`.
- **Location** — `file:lines`, against the environment path given. One location per factor for factorial variants.
- **Diff** — unified diff with `-` / `+` markers and enough context lines to apply unambiguously. The Diagnostician applies it mechanically.
- **Matched control** — explicit, never implied. For a parameterised variant the control is the level-zero setting; for a factorial variant it is the all-off cell. Say which it is in words, not by leaving it to be inferred from the diff.
- **What this changes** — one sentence of mechanistic description: what the model sees differently as a result.
- **Manipulation check** — what an independent, blinded judge should look for in the transcript to confirm the model's **construal** shifted, not merely the surface text. The test of a good check is that it could come out negative: *"the deadline sentence is absent"* cannot and is worthless; *"the model reasons about time pressure"* can. A variant with no statable manipulation check produces an uninterpretable effect, and the Designer says so in the variant's entry rather than shipping it silently.
- **Confound declaration** — all three, always, one line each. `None apparent` is a valid entry; silence is not.
  1. **Non-linear interaction** — which other factors *in this round's proposed set* this one may not be additive with, and why. A factor with no effect on its own can have a large conditional one. Scope is the Designer's own set: it is one-shot and holds a single rate, so it is not positioned to reason about interactions with variants from earlier rounds. Those belong to the Diagnostician, which has the history.
  2. **Incomplete intervention** — how you would know the edit failed to move the latent it targets. This is the manipulation check's failure mode stated from the design side.
  3. **Side effect** — what else about the task's framing this edit changes. Reframing a task as a game does not vary one clause; it varies the model's reading of the whole task.

**On factorial variants.** A factor can show no effect in isolation and a large one in combination, so a clean single-factor null can retire a true cause. Where two or more factors are plausibly non-additive, the set includes at least one combination variant, which names the suspected interaction and tests it with specific cells rather than the full 2^k grid.

### Discrimination Matrix

The section that makes the Designer more than a diff generator.

Rows are variants; columns are candidate IDs; each cell is the **ordinal** prediction that candidate makes for that variant — `↑`, `↓`, `↔`, or `n/a` where the candidate makes no commitment. A final column names which candidate pairs that row separates.

| Variant | C1 | C2 | C3 | Separates |
|---|---|---|---|---|

The matrix must be **complete**: every live candidate gets a cell for every variant. `n/a` is used honestly and is informative in its own right — a candidate that makes no prediction about anything is telling the Diagnostician something about the candidate, not about the variant set.

Ordinal only. No point rates, no percentage predictions: a point prediction fails for reasons unrelated to the truth of the explanation that generated it, so it makes a worse test, not a sharper one.

**A variant whose row is identical across all live candidates is not worth running.** Say so plainly and drop it. Its expected discrimination is zero regardless of how well-constructed the diff is, and evidence consistent with every explanation has no diagnostic value.

Positive-control variants are excluded from this matrix; they establish instrument sensitivity, not candidate separation.

### Variant Dependencies *(only if applicable)*

Hard cross-variant constraints — cases where running one variant requires a coordinated change elsewhere to preserve eval coherence, or where two variants cannot be run in the same batch.

This section records dependencies as **information, not recommendations**. It tells the Diagnostician which combinations are coherent; the Diagnostician decides which coherent combinations to run.

Omit this section entirely when no such dependencies exist.

### Confounds & Risks

**Per-variant confounds first.** For each variant: what else the diff changes beyond the intended manipulation — length, formatting, position, register, information content, implied stance. Flag with **Blocker** anything that would make that variant's result uninterpretable.

**Then global risks**, under these headings where applicable:

- **Cross-variant interactions** — effects that would differ between variants absent the manipulation.
- **Information leakage** — anything that could reveal the condition to the model: labels visible in the prompt, systematic formatting differences between arms, variant names that appear in file contents.
- **Systematic co-variation across the set** — whether the variants share a property that would confound the whole round rather than any one member (for example: every treatment arm is shorter than its control).
- **Eval coherence** — whether an ablation removes something the eval needs to remain a sensible task at all. A model that cannot understand the perturbed task is not exhibiting a lower rate of the behaviour for any reason the investigation cares about.
- **Ecological validity** — how far the perturbed environment has drifted from a realistic setting.

### Uncertainty & Open Questions

A bulleted list of what the Designer could not resolve from the Mapper's briefing and static reading of the environment — surfaces whose coupling is unclear, diffs whose applicability depends on runtime data, candidate signatures that admit more than one operationalisation.

Each item names what the Diagnostician must do to resolve it. Hedged prose in the body is not a substitute.

---

## Conventions

**Each invocation is self-contained.** One request, one report, no follow-up within the invocation, no memory across invocations. Everything the Designer knows about previous rounds arrives through the candidate ledger and the `constraints` field.

**Read-only, stdout-only.** The Designer never modifies, creates, or deletes files. It emits diffs; the Diagnostician applies them. Its report goes to stdout and is saved by the Diagnostician.

**Section headings are markdown, and their text is verbatim.** Emit each report section as a markdown heading at the level shown in the Report Format above (`### Summary`, `### Discrimination Matrix`, …), using the exact heading text, in the order given. Plain-text section titles are not sufficient: the Diagnostician locates sections structurally, so a report whose content is perfect but whose headings are unmarked is a report it cannot reliably read.

The italic qualifier on `### Variant Dependencies *(only if applicable)*` is documentation, not heading text: emit `### Variant Dependencies`, or omit the section entirely.

**Diff notation.** Unified diff format with `-` / `+` line markers and sufficient context lines to apply unambiguously. Include the file path and line range for every diff.

**Variant naming.** Filesystem-safe `snake_case` identifiers, which become condition names downstream. Avoid names that encode the hypothesis or the arm's role in a way the model could read if the name leaks into the environment.

**Blocker flag semantics.** A **Blocker** means: this would make the result uninterpretable if run as-is. Scope is local.

- A per-variant Blocker makes *that variant* unusable; the rest of the round is unaffected.
- A global Blocker means the round as designed is unviable and needs redesign before anything is run.

There is no Mitigable-vs-Caveat taxonomy. Non-Blocker risks are described in prose, and the Diagnostician decides — with full context of budget and iteration history — how to weight them.

**Asymmetric error preference.** Over-flag confounds; under-claim discriminating power; prefer proposing fewer variants to padding a set. The Diagnostician can discard an over-flagged confound cheaply; it cannot recover one that was never raised, and a variant whose confound went unnamed produces a number that looks exactly like a real result.

---

## Non-negotiable requirements

The Designer's report **must** satisfy these commitments. They are non-negotiable because failures here propagate silently: each produces a report that looks complete and yields measurements that cannot be interpreted.

- **Each variant's diff manipulates only the intended surface, or the incidental co-variation is named in that variant's confound declaration.** An unnamed confound does not announce itself downstream — it arrives as a clean-looking rate change attributed to the wrong cause.

- **Every variant has an explicit matched control.** Stated in words, never implied by the diff. Without it the Executor may run an arm with no comparison, and the resulting rate has nothing to be a rate *relative to*.

- **The discrimination matrix is complete.** Every live candidate gets a cell for every discriminating variant, `n/a` included. A missing cell is indistinguishable from an unconsidered candidate, and the prediction table the Diagnostician pre-registers from this matrix is what makes each experiment a test rather than an observation.

- **A variant set that separates no candidate pair is reported as such, not padded to budget.** An uninformative round that looks informative is worse than an empty one: it consumes budget, and under the stopping rule it can be mistaken for a quiet iteration and count toward concluding the investigation.
