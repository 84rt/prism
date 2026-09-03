# Session C Brief — Integration and Delivery (Tasks 6b → 10)

You are assembling the Model Forensics mode from parts other sessions built, then proving it runs end to end.

**This brief is also a handoff document.** Sessions A and B have both finished and filled in the two "What you are inheriting" sections below. Those sections, not the plan, are the authoritative record of what was actually built. Everything through Task 6 is committed; your work starts at 6b.

---

## What changed before you started

After Sessions A and B finished, `.claude/docs/` was reorganised and three decisions the brief had left open were settled. **The plan predates all of this.**

**Documents are now grouped by who may read them**, which makes the directory structure the blinding model rather than an unrelated tidy-up:

```
.claude/docs/
├── investigator/   ← lead agents only; every sub-agent denies this directory wholesale
├── contracts/      ← lead agents + the owning sub-agent
└── references/     ← one sub-agent each (Inspect for Executor, Scout for Analyst)
```

**`runner.py` now supports directory-level denial.** A `RESTRICTED_FILES` pattern ending in `/` matches anywhere in the path; anything else still matches as a filename suffix, so existing patterns are unaffected. Every sub-agent now denies `.claude/docs/investigator/` and `.claude/skills/` with one entry each. This is the important part: **a document added to a denied directory later is blinded automatically.** `diagnostician_responsibilities.md` is already blocked in all six agents despite not existing yet — verified by exercising the real hook, not by inspection.

**Renames:** `analyst_delegation_guide.md` → `investigator/orchestrator_analyst_guide.md`. The Diagnostician's equivalent will be `investigator/diagnostician_analyst_guide.md` — **you write it, as part of Task 7.** It carries neutral-topic translation plus forensic-specific delegation (`behaviour_spec` construction, grader reuse, the manipulation check). Both live in the same directory, so both are blinded by the same entry.

**Consequences for your tasks.** Every path in the plan's Task 9 wiring list is now wrong — rebuild from disk. `link_file` creates parent directories via `ensure_local_dir`, so subdirectory paths need no special handling, but `uninstall.sh`'s empty-directory cleanup loop enumerates directories by hand and now lists the three subdirectories deepest-first. Install/uninstall round-trip was verified after the move: 9 docs linked, no broken symlinks, no residue.

---

**Read first:** this brief, then `docs/superpowers/plans/2026-08-14-model-forensics-mode.md` — Global Constraints, D1–D15, and Tasks 6b, 7, 8, 9 and 10 in full. Then `.claude/docs/investigator/model_forensics_principles.md`, using the step→section index in Task 1 to read targeted sections rather than the whole thing at once.

---

## How your work differs from A's and B's

They wrote components against a specification. You are doing the opposite: **consuming what exists and making it cohere.** Three consequences.

Your primary risk is not writing bad code — it is **citing things that aren't there.** Task 7 refers to contract sections by exact heading; Task 9 wires an exact file list; Task 8 embeds a citation index. Each of those is a claim about another session's output. Verify against the files, never against the plan's description of them.

**The plan's file lists are known to be stale.** They were written before Sessions A and B ran and predate several things: `analyst_forensic_addendum.md` (a sixth new `.claude/docs` file, absent from Task 9's wiring list), the `baseline_rate` → `environment_rate` rename, the Designer's three new optional request fields, and `sample_id` on the Executor. Task 9's `link_file` list and Task 6b's field documentation must both be rebuilt from what is on disk, not copied from the plan. The two handoff sections below are the authoritative record of what changed.

**Where the plan and reality disagree, reality wins** — but say so. If a contract's headings differ from what the plan anticipated, update your citations and note the divergence rather than silently accommodating it. The same rule governs Task 10: when a sub-agent violates its contract, fix the *contract or system prompt*, not your expectation of it. A sub-agent that cannot follow its guidance is evidence the guidance is underspecified.

You are the **last checkpoint before this mode is usable.** Nobody downstream will catch what you miss.

---

## What you are inheriting from Session A

> **Session A: fill this in when you finish.** Keep it short — C needs the shape of what you built, not a narrative.
>
> - Files actually created (if this differs from the plan's list, say so and why)
> - The exact `### Section Headings` each of your three contracts emits, in order — Task 7 cites these structurally and Task 9 wires them
> - Each agent's `required_fields` as passed to `AgentCLIConfig`, and any mode-conditional fields validated elsewhere
> - Design calls you made that the plan left open, and your reasoning
> - Anything you discovered that changes a downstream task
> - Anything you could not verify offline and left for Task 10

**Files created — 15, exactly the brief's allowlist, no deviations.** Three packages of four (`__init__`, `system_prompt`, `agent`, `main`) under `subagents/{environment_mapper,variant_designer,redteam_critic}/`, plus the three contracts now at `.claude/docs/contracts/{mapper,designer,critic}_interface_contract.md`. Nothing outside the allowlist was touched. All of it is now committed, along with the reorganisation described above.

**Report section headings, verbatim and in emission order.** All at level 3.

- **Mapper** — `### Summary`, `### Environment Summary & File Catalogue`, `### Pipeline Model`, `### Instruction Analysis`, `### Scorer Analysis`, `### Tool & Environment Analysis`, `### Manipulable Surface Inventory`, `### Environment Defects`, `### Uncertainty & Open Questions`.
- **Designer** — `### Summary`, `### Proposed Variants`, `### Discrimination Matrix`, `### Variant Dependencies`, `### Confounds & Risks`, `### Uncertainty & Open Questions`. The contract shows the fourth as `### Variant Dependencies *(only if applicable)*`; the italic qualifier is documentation, and a Conventions line states that the emitted heading is `### Variant Dependencies`, or the section is omitted. Cite the bare form.
- **Critic** — per mode, and only the active mode's sections are emitted:
  - `candidate_critique`: `### Independent Reading`, `### Blind Spots`, `### Assumption Audit`, `### Proposed Additional Candidates`, `### Candidates I Would Retire`.
  - `diff_review`: `### Summary`, `### Per-Diff Analysis`, `### Cross-Diff Analysis`, `### Inferred Purpose`.
  - `prediction`: `### Chosen Factor`, `### Prediction`, `### Reasoning`, `### What Would Falsify This`.

Two Summaries now open with **fixed-format label lines before any prose**, so Task 7 can extract them without parsing narrative. Mapper: `**Prohibition status:**`, `**Scorer detection:**`, `**Defect sufficiency:**`. Designer: `**Environment:**`, `**Discriminating variants:**`, `**Off-regime variants:**`, `**Positive controls:**`, `**Headroom:**`.

**`required_fields` as passed to `AgentCLIConfig`.**

| Agent | `required_fields` | `directory_field` |
|---|---|---|
| Environment Mapper | `environment_path`, `behaviour_description` | `environment_path` |
| Variant Designer | `mapper_report_path`, `environment_path`, `candidates`, `regime`, `environment_rate`, `variant_budget` | `environment_path` |
| Red-Teaming Critic | `mode` | none |

The Critic's remaining requirements are **mode-conditional** and cannot live in the flat list. They are validated in `redteam_critic/agent.py::_build_prompt` against a module-level `MODE_REQUIRED_FIELDS`, which raises `RuntimeError` — caught at `cli.py:111` for **exit 2**, not exit 1. `candidate_critique`: `transcript_paths`, `mapper_report_path`, `candidates`, `iteration`. `diff_review`: `diffs`, `environment_path`, `mapper_report_path`. `prediction`: `investigation_log_path`, `leading_explanation`, `untested_surfaces`.

**Design calls the plan left open.**

- **`environment_rate` replaces the plan's `baseline_rate`** on the Designer, and means *the rate of the environment supplied*, not the frozen step-1c baseline. The Designer may be handed a derived variant, and putting its Wilson headroom check and regime derivation on the frozen baseline would compute both against the wrong number. It receives one rate and never a history of rates.
- **`regime` is a steer, not a directive.** The Designer never substitutes the regime for a whole round. It proposes individual off-regime variants marked with a reason, listed on the `**Off-regime variants:**` line; the Diagnostician rejects one by not applying its diff. Both agents reason from the same rate, so disagreement should be rare.
- **Three optional request fields added to the Designer**, each because a contract obligation had no field to trigger it:
  - `elimination_targets` — candidate IDs to be retired on *absence* of evidence. This is what triggers D13's positive controls; the plan required them but gave no way to name a target.
  - `environment_delta` — what the supplied environment is, relative to the Mapper's map. **Load-bearing; see downstream note.**
  - `variations_tried` — ground already covered across all rounds and branches.
  - Both of the latter carry **what changed, never what it did**. No rates, no directions. The changes are needed for coherence; their outcomes would bias a fresh-each-time agent toward repeating edits that already worked.
- **`iteration` is required and rendered** on `candidate_critique`. The plan listed it as a request field but its own `_build_prompt` example neither validated nor rendered it — the silent-failure pattern the briefs warn about.
- **Positive controls sit outside `variant_budget`** and are excluded from the discrimination matrix. They calibrate the instrument rather than separating candidates, and a budget that could crowd them out lets a tight round silently buy an uninterpretable elimination.
- **D15's non-linear-interaction confound is scoped to the Designer's own proposed set**, not "at baseline". A one-shot agent holding one rate cannot speak to interactions with earlier rounds' variants; that reasoning belongs to the Diagnostician, which has the history.
- **The Critic contract is organised by direction** (`## Diagnostician → Critic: Request Format` / `## Critic → Diagnostician: Report Format`), matching the other three contracts, with modes as subsections and a `## Modes` router table at the top.

**What changes downstream tasks.**

- **Task 6b** must document `environment_rate` (**not** `baseline_rate` — a copied example using the old name fails validation), plus `elimination_targets`, `environment_delta`, `variations_tried` on the Designer and `iteration` on the Critic. Note the Critic's exit-2 path: a bad `mode`, or a mode missing a field, is an *agent* error.
- **Task 7 owes the Designer `environment_delta` whenever it hands over a derived variant.** This is the sharpest inheritance. The Mapper runs once on the original and its briefing goes to the Designer every round, so on a derived environment the map describes a different tree — and the Designer is instructed to locate every surface in that map. Without the delta it meets an unexplainable mismatch whose available readings are "stale map" or "broken environment", and the likely result is the investigation's own manipulation being reported as an environment defect with a **Blocker** on a sound variant. The `**Environment:**` Summary line is the detector: if it reads `original` when a derived environment was supplied, the delta did not arrive and that report's surface locations are untrustworthy.
- **Task 7** also owns the value-of-information gate on `elimination_targets` — each named target costs a condition, so name one only where retiring it would change the leading explanation or move the equivalence-class boundary. A candidate ranked well below the leader belongs in the equivalence class, not in an elimination round. The repo owner has taken this into Task 7's responsibilities.
- **`diagnostician_responsibilities.md` was in none of the `RESTRICTED_FILES` lists — since resolved.** The lists enumerated filenames, so a document that did not yet exist could not be in them. The reorganisation replaced enumeration with directory-level denial, so writing that file into `investigator/` blinds it in all six agents automatically. **The reasoning still binds you: put it in `investigator/`.** A responsibilities doc placed anywhere else inherits the original gap.
- **Task 9 wires my 15 files** (plus B's addendum and whatever you add), and every `main.py` bootstrap resolves the project root by three `dirname` calls from `__file__` — so `subagents/<agent>/main.py` depth must be preserved by the symlink layout. Rebuild the full list from disk rather than counting from any prose in this brief.
- **On the brief's standing lint gate:** `ruff` is not installed in this container and `uv run ruff check` fails to spawn. `uv run --with ruff ruff check --select E4,E7,E9,F subagents/` passes clean across the tree. A bare `uv run --with ruff ruff check` resolves ruff 0.16.3, whose defaults additionally flag the `# noqa: E402` on every `main.py` (pre-existing on all six agents) and 2 × ASYNC230 in `runner.py`. Nothing here is new.

**Left for Task 10 — nothing was run against an API.** Every check was offline: CLI validation paths, prompt-builder field coverage (each request field probed with a sentinel value), and lint. So all *behavioural* contract adherence is unverified: verbatim heading emission, the fixed-format Summary lines, whether the Designer actually computes the Wilson interval rather than eyeballing it, and whether the Critic honours the `### Independent Reading`-before-ledger ordering. That last one is the highest-value thing to watch, since the whole mode rests on it and a violation is invisible in the output.

---

## What you are inheriting from Session B

> **Session B: fill this in when you finish.** Same brief as above.
>
> - Final shape of `behaviour_spec` as implemented, including which fields are required when
> - The new report sections the Analyst contract now specifies, with exact headings
> - **How Task 6 resolved:** whether `runs_per_condition` was implemented in the Executor's system prompt or the resample was specified via `epochs` instead — and if you touched `subagents/experiment_executor/system_prompt.py`, flag it, since that file also serves the Orchestrator
> - Confirmation the Orchestrator path is unchanged, and how you verified it
> - Anything you discovered that changes a downstream task

### Files touched

```
subagents/transcript_analyst/agent.py            modified
subagents/transcript_analyst/system_prompt.py    modified
subagents/experiment_executor/system_prompt.py   modified  ← outside B's allowlist, see Task 6
.claude/docs/contracts/executor_interface_contract.md      modified
.claude/docs/contracts/analyst_forensic_addendum.md        NEW      ← needs Task 9 wiring
.claude/docs/contracts/analyst_interface_contract.md       UNCHANGED (zero diff — see below)
```

### The design changed from the plan: two prompt variants, not one conditional prompt

The plan (Task 5 Steps 1 and 3) had forensic content added *into* the shared contract and the shared system prompt, gated by in-text conditionals. **That is not what was built**, at the repo owner's direction. Reason: an Orchestrator-mode Analyst would have read the forensic instructions on every invocation and had to decide for itself that they did not apply — and a tightly-scoped neutral topic ("examine whether models edit test files rather than implementations") reads a great deal like a behaviour description. The boundary is now structural instead:

- `system_prompt.py` exports `SYSTEM_PROMPT` (unchanged, the Orchestrator's), `FORENSIC_SECTION` (the overlay), and `SYSTEM_PROMPT_FORENSIC = SYSTEM_PROMPT + FORENSIC_SECTION`. Composed, not duplicated — one copy of the shared instructions, so the modes cannot drift.
- `agent.py` selects between them on `data.get("behaviour_spec")`.
- All forensic *contract* content lives in the new `analyst_forensic_addendum.md`. `analyst_interface_contract.md` has a **zero-line diff**.
- Only the forensic prompt names the addendum, so an Orchestrator-mode Analyst is never told it exists.

**Consequences for you.** Task 7 must cite the addendum for every forensic report section, not the base contract. Task 6b must document `behaviour_spec` as the field that flips the mode. Task 9 must wire the addendum (below).

### `behaviour_spec` as implemented

Optional request field on the Analyst; its presence *is* the mode switch. Sub-fields:

| Field | Required? |
|---|---|
| `description` | always |
| `reference_transcript` | on first build; optional once `grader_dir` is populated |
| `grader_dir` | optional — absent/empty means build-and-persist, populated means load-and-apply-unmodified |
| `manipulation_check` | optional — when present, its report subsection becomes required |

`main.py`'s `required_fields` is **unchanged** (`["topic", "transcript_source"]`). `behaviour_spec` is validated nowhere; a malformed one reaches the Analyst as JSON.

### Exact headings the addendum adds

New **top-level report sections**, both required whenever a `behaviour_spec` was supplied, including when empty:

- `### Behaviours not captured by the grader` — placed after `### Quantified results`
- `### Eval-awareness split` — placed after the above, before `### Scan results path`

Headings carry **no mode qualifier**. Emit them exactly as written; the `*(forensic mode)*` annotations in the addendum's prose are documentation, not heading text.

New **run-in bold subsections** (not headings) inside existing base sections:

- In `### Summary`: the manipulation-check verdict on the headline line when `failed`/`ambiguous`; and a sub-threshold grader reported as an investigation-stopping failure rather than demoted to "Provisional"
- In `### Scanner definitions`: the grader documented on **every** invocation, loaded or built — the base section says "for each scanner built during this analysis cycle", which read literally would leave the primary instrument undocumented from iteration 2 onward
- In `### Validation metrics`: *Grader provenance*, *Floor check*, *Borderline analysis*, *Manipulation check*
- In `### Quantified results`: *Graded behaviour rate*

The addendum also specifies **what a populated `grader_dir` must contain** (scanner definition, validation set with label provenance, build-time metrics, `README.md`), and that a directory missing any of these counts as unpopulated. That is an interface between invocations, not an implementation detail, so Task 7 should not re-specify it.

### How Task 6 resolved

**Specified via `epochs`; `runs_per_condition` was not implemented.** Three reasons: implementing it changes the Orchestrator's Executor; `execute_evals.py` rejects duplicate `log_dir` values, so *n* runs force *n* sibling directories against an Analyst interface that maps one condition to one path; and `--epochs` was already the only one rendered.

**I did touch `subagents/experiment_executor/system_prompt.py`** — flagged as the brief requires, since it also serves the Orchestrator. Two changes, both additive and approved by the owner:

1. Added `sample_id` → `--sample-id` to the rendered override list, plus a paragraph on how it pairs with `epochs`.
2. Preserved and extended the `runs_per_condition` accepted-but-not-applied paragraph (which appeared in that file mid-session from another source, not written by me).

**`sample_id` is new and matters to you.** `--epochs` repeats the *whole dataset*, so a *k*-sample task with `epochs: 30` yields 30*k* transcripts — 30× the intended cost, and the resample pool gets contaminated with transcripts of scenarios that were never under investigation, which is where the Analyst draws its negative controls and borderline cases from. `--sample-id` pins the episode. Added to the contract's override table and to the Executor's system prompt.

### Zero-success split

The brief's line "a zero success count here is terminal for the investigation" conflates two things the Executor cannot both see. Resolved as:

- **Executor** (contract + system prompt): report zero successful runs plainly, full evidence, standard retry policy only, never relax a pin to force success. Makes no judgement about meaning.
- **Diagnostician** (yours, Task 7): distinguishing "the eval could not run" (fix and re-run) from "the behaviour did not occur" (terminal, step 1x). The Executor is structurally blind to the second — it never reports scorer outputs.

The Executor contract now contains a **forward reference to `diagnostician_responsibilities.md`** for what follows from each. That file does not exist until you write it.

### Orchestrator path unchanged — how verified

- `git diff --numstat` on `transcript_analyst/system_prompt.py`: **51 additions, 0 deletions**. `analyst_interface_contract.md`: **no diff at all**. `agent.py`: 2 deletions, both mechanical (the import line, and `system_prompt=SYSTEM_PROMPT` becoming a variable).
- Ran the prompt builder both ways against a stubbed `run_agent` and asserted: the Orchestrator-mode system prompt is **byte-identical to the committed HEAD version**; it contains no occurrence of "forensic", "grader", or the addendum filename; no `## Behaviour Spec` section appears in the user prompt when the field is absent; the forensic prompt equals base + overlay exactly.
- `ruff check` clean on all three edited Python files. Repo-wide count is unchanged from HEAD (10, all `RUF100` in files nobody touched).

### What changes your downstream tasks

1. **Task 9 — wire `.claude/docs/contracts/analyst_forensic_addendum.md`.** `link_file` in `install.sh`, the matching `unlink_file` in `uninstall.sh`, and the `.gitignore` entry. `.claude/docs/` is symlinked per-file, so **the addendum does not exist at runtime in a target repo until you do this**, and the forensic Analyst will fail to read a file its system prompt tells it to read. This is the single highest-consequence item in this handoff.
2. **Task 7 — require `sample_id` in the 1b resample.** Phase 1 must have the Diagnostician identify the episode's sample id at scoping and pass it. Without it the resample is *k*× cost with a contaminated pool, and nothing downstream will flag it.
3. **Task 7 — cite the addendum, not the base contract**, for all forensic report additions. Load-bearing for a non-obvious reason: **there is no pointer to the addendum anywhere else.** The base contract deliberately does not mention it, because that file is also read by the Orchestrator-mode Analyst and a pointer would reintroduce the mode-ambiguity the split exists to remove. The Analyst finds the addendum via its forensic system prompt; the Diagnostician finds it only if `diagnostician_responsibilities.md` and `subagent_invocation.md` name it. Miss this and the Diagnostician builds requests against the base contract alone, never sends `behaviour_spec`, and forensic mode silently never engages.
4. **Task 6b — `behaviour_spec` under the Analyst**, noting that its presence selects the forensic system prompt, and that the firewall is not breached (it names the explanandum, never an explanation). Also add `sample_id` to the Executor's documented override key list.
5. **`orchestrator_responsibilities.md:222` lists the supported override keys and does not include `sample_id`.** Outside my allowlist so I left it. Harmless — the Orchestrator simply won't know the key exists — but worth a one-line addition when you are in that file.
6. **Not verified offline, left for Task 10:** that Inspect's `--sample-id` accepts the ids the target task actually exposes, and that a loaded grader round-trips from a persisted `grader_dir`. Both need a real run.

7. **Audit document access end-to-end, for both modes, before Task 10.** There are now two lead-investigator roles (Orchestrator, Diagnostician) and a sub-agent set that partly overlaps between them, and every document each party reads is governed by a *different* mechanism: system prompts name contracts by path, `RESTRICTED_FILES` denies reads, and `install.sh` decides which files exist at runtime at all. Nothing cross-checks them. Walk the matrix explicitly — for each agent in each mode, confirm that the contract it is told to read exists, is installed, and is the right one; that the delegation/methodology guidance it needs is reachable; and that everything it must *not* see is actually denied.

   **The three items below were resolved by the reorganisation (see "What changed before you started"). They are kept because the reasoning still governs the audit, and because the last one names a failure mode that the fix prevents but does not abolish — a document placed outside a denied directory is still invisible to every list.**

   - **The Analyst's `RESTRICTED_FILES` has not kept up with the new mode, and this is a live hole.** It currently blocks only `eval_science_principles.md` and `orchestrator_analyst_guide.md`. Session A's Mapper, Designer and Critic each additionally block `model_forensics_principles.md` and `candidate-methodology.md`. So the Analyst — the agent whose blinding the whole design turns on — is the *least* blinded of the five, and can read the Diagnostician's full methodological framework including how candidates are adjudicated. `model_forensics_principles.md` is already committed to `.claude/docs/`, so this is not hypothetical. The fix is two entries in `subagents/transcript_analyst/agent.py`; it was left out of Session B because it changes the Orchestrator's Analyst too and wanted an explicit decision.
   - **The Diagnostician has no equivalent of `orchestrator_analyst_guide.md`.** The Orchestrator has one, the Analyst is blocked from reading it, and it carries the neutral-topic translation rules. Task 1c's spec cross-references `hypothesis-methodology.md §Neutral Topic Translation`, which lives in the `orchestrate` skill. Decide whether the Diagnostician reuses that file, gets its own, or has the rules restated in `diagnostician_responsibilities.md` — and whichever you choose, make sure the Analyst's blinding list covers it.
   - **The addendum escapes every blinding list, because it is a new basename.** Four agents — Mapper, Designer, Explorer and Executor — block `analyst_interface_contract.md` in their `RESTRICTED_FILES`. None blocks `analyst_forensic_addendum.md`. `_build_hooks` matches with `file_path.endswith(pattern)`, so splitting the contract into a second file silently opened a hole in lists written to close it. Low severity on its own — the addendum describes measurement, not candidate explanations — but it is the general failure mode worth internalising before Task 9 adds six more files: **every new document is invisible to every existing blinding list until someone adds it.** Decide the policy once (block the pair together, or match on a shared prefix) rather than patching per file.

   - **Symmetry across all five sub-agents.** Each system prompt should name its contract the same way, each contract should be installed by Task 9, and each agent should be able to read its own contract (deliberately excluded from its own `RESTRICTED_FILES`, as `environment_explorer/agent.py` documents). Drift here is invisible until an agent is denied a file it was told to read, which surfaces as a confusing mid-run failure rather than a clear error.

---

## Your sequence

Strictly serial — each task depends on the one before.

**6b — Consolidate the invocation reference.** Small. Absorbs the four edits Tasks 2–5 were forbidden from making, so this file has exactly one author. Copy each JSON example from the corresponding `main.py`'s `required_fields` so every example is guaranteed to validate, then prove it by running each one.

**7 — Diagnostician responsibilities.** The long pole, and the plan's largest deliverable. It defines the pipeline, the two-file state model (`CANDIDATES.md` plus append-only log deltas), the pre-registration table, and the report structure. Everything upstream exists to be consumed here.

**8 — The `/diagnose` skill.** Entry point plus two methodology documents. Depends on Task 7's phase numbering, so do not start it early.

**9 — Install wiring and README.** Mechanical but load-bearing: a missed `link_file` means the file silently does not exist at runtime in a target repo. Note **Task 0 is already done** — the `/orchestrate` rename and its six call sites are committed, so you are adding new entries, not renaming old ones. Mirror every entry into `uninstall.sh` *including its empty-directory cleanup loop*, which enumerates directories by hand and is easy to miss.

**10 — End-to-end dry run.** Where contract mismatches surface. Expect to find some; that is the task's purpose.

---

## Standing constraints

- **Do not commit.** The repo owner commits by hand.
- **Additive only on shared files.** An Orchestrator investigation must behave identically after your work. This binds hardest in Task 9, where `install.sh` and `uninstall.sh` are shared with a working pipeline.
- **No new Python dependencies**; everything runs under the existing `uv run --with claude-agent-sdk --with python-dotenv` invocation.
- **Lint is the gate** — there is no test suite. `ruff` is not installed in this container, so the working invocation is `uv run --with ruff ruff check --select E4,E7,E9,F subagents/ scripts/`, which passes clean across the tree. A bare `uv run ruff check` fails to spawn, and a bare `uv run --with ruff ruff check` additionally reports pre-existing `RUF100` and `ASYNC230` findings that are not yours (see Session A's note below).
- Interface contracts are the single source of truth for request/report shape; system prompts govern process. Both the Diagnostician and the sub-agent read the contract.

---

## Open question you inherit

**How much of `model_forensics_principles.md` the Critic should be denied.** Session A blocks it entirely, on the reasoning that shared methodology means shared priors — which is the whole point of a cross-family critic. But the doc also contains the admissibility bar the Critic is held to (every proposed candidate needs a discriminating signature), so that requirement is restated in the Critic's own system prompt and can drift out of sync. Not a blocker. Worth revisiting if Task 10 shows the Critic proposing untestable candidates.

---

## Two things to verify early, before Task 7 gets long

**Read A's three contracts side by side.** They are template siblings, and the failure mode is not any one being wrong but the three drifting apart in section shape — which propagates into Task 7 and only surfaces at Task 10. Ten minutes here is cheap.

**Confirm the citation index still resolves.** Task 1 built a step→section map into `model_forensics_principles.md` that Task 8 embeds in `SKILL.md`. The doc has twelve numbered sections plus a Standing Epistemic Position; if any citation in the plan points somewhere that no longer exists, fix it before Task 8 copies it forward.
