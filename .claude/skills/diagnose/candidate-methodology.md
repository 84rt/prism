# Candidate Methodology

How to write, generate, rank, and retire candidate explanations. This file is the operational procedure; the survey behind it is `model_forensics_principles.md` (cited by section below), and the pipeline steps that consume these rules are in `diagnostician_responsibilities.md` (Phase 2 and 3d).

## Anatomy of a candidate

Every ledger entry has four load-bearing parts:

- **Statement** — the explanation, in one or two sentences, specific enough that two readers would derive the same predictions from it.
- **Discriminating signature** — what manipulation would move this candidate, and in which direction. This is the admissibility bar: a candidate without one cannot be the subject of diagnostic evidence, can never be retired, and is not admitted (D2; `model_forensics_principles.md §2`, §8).
- **Origin** — where it came from: `CoT read iter 0`, `user-supplied`, `Critic iter N`, `analyst-surfaced iter N`, `evidence-suggested iter N`. Never overwritten.
- **Status** — `live` (ranked), `live (untested)`, `inert`, or `eliminated`.

Live candidates additionally carry a **tier** — `tested` (in this round's prediction table) or `carried` (listed, ranked, critiqued, but not predicted on) — assigned initially at the Phase 2 ranking and thereafter at 3d step 9, orthogonal to status. See *Ledger economics* below.

**Worked example 1** (mundane, admissible):

```markdown
### C1 — `test_as_spec` — rank 1
- *Statement*: The model treated the visible failing test as the task
  specification, and satisfied it directly because nothing in its construal
  marked doing so as out of bounds.
- *Origin*: CoT read iter 0
- *Discriminating signature*: Hiding the test file should collapse the
  behaviour; strengthening the docstring's specification while the test stays
  visible should not move it.
- *Evidence for*: none yet
- *Evidence against*: none yet
- *Confounds not yet excluded*: hiding the test also shortens the prompt
- *Recipe membership*: standalone
```

**Worked example 2** (dispositional, still admissible — the signature is what makes it so):

```markdown
### C3 — `eval_gaming` — rank 3
- *Statement*: The model recognised the setting as an evaluation of coding
  ability and optimised for the score signal rather than the stated task.
- *Origin*: CoT read iter 0
- *Discriminating signature*: Removing evaluation cues (grading language,
  benchmark formatting) should reduce the behaviour; making the scorer's
  criteria explicit should increase it. Eval-awareness rate should co-vary
  with the behaviour rate across these variants.
- *Confounds not yet excluded*: cue removal changes prompt register wholesale
- *Recipe membership*: standalone
```

**Counter-example, and its repair.** *"The model is lazy"* is inadmissible: no manipulation is named, no direction committed to, and any outcome can be narrated into it. Repaired: *"The model avoids high-tedium work — the behaviour rate should fall monotonically as the number of required repetitive edits is reduced, and should not respond to deadline framing."* Same intuition, now carrying a parameterised signature (a dose-response on tedium) and a null commitment (no deadline effect) that a rival candidate would dispute. The repair pattern generalises: keep the disposition, force it to name its dose and its non-causes.

## Generating candidates from a CoT read

What to attend to in the transcripts (Phase 2 of the pipeline):

- **The model's apparent construal of the task** — what did it take the situation to be, from inside? The gap between the environment as written and the task as construed is often the whole explanation (`§4`).
- **Whether it registers a prohibition** — compare against the Mapper's `### Instruction Analysis`. Prohibited-and-registered, prohibited-and-unregistered, and unprohibited are three different explananda.
- **Alternatives considered and rejected** — these ground the contrast class and show what the model treated as live options.
- **Signs of capability failure versus choice** — an error the model does not notice is a different candidate from a shortcut it reasons its way into.

What *not* to do: treat the CoT as a faithful record of process. It is systematically unfaithful in some settings, more so in more capable models, and there is no ex-ante measure for the trace in front of you (`§7`). The CoT licenses candidates and construal readings; it never closes a candidate, and "the reasoning looks coherent" is not evidence that it drove the action. Read the raw transcripts before reading any hypothesis list, including your own notes.

## The three generative lenses

Run all three before finalising the list; each reliably surfaces a class the others structurally miss, and running several is the cheapest defence against the bad-lot problem (`§3`, §9 — the full survey lives there; this is the operating card):

1. **Differential-diagnosis sweep** — *what are the common, dangerous, and treatable causes, weighted by base rate?* Surfaces high-base-rate mundane explanations; misses interactions and rare mechanisms. Reserve the explicit must-exclude slot for deliberate misalignment — on the list by consequence, excluded by evidence, never omitted by prior.
2. **Local-rationality reconstruction** — *why did this action make sense to the model at the time?* Surfaces construal-based benign explanations (puzzle, game, consequence-free simulation, implied instruction); misses genuine goal-directedness, since the frame is built to resist blame.
3. **Deception/false-flag frame** — *what if the model behaved this way because it might be observed?* Surfaces eval-awareness, sandbagging, plausible deniability; misses non-adversarial causes, since the frame presupposes an adversary.

Close with **negative-case analysis**: for each emerging candidate, pull the resamples that contradict it, not the ones that fit (`§9`).

## The mundane candidates you must include

A named checklist, because these are the ones a sophisticated investigator skips — and each is a live competitor to any interesting explanation:

1. **The model misread the instructions** — task misconstrual, with the divergence visible against the Mapper's instruction analysis.
2. **The task as written is impossible or contradictory** — the only way to "succeed" is the concerning action (task misspecification).
3. **The environment is broken in a way that makes the behaviour the only available action** — any Mapper `### Environment Defects` entry with defect-sufficiency enters here.
4. **The behaviour is a capability failure** — no disposition behind it; the model could not do the intended thing.
5. **The scorer rewards it and the model optimised the scorer** — the behaviour is what the reward channel actually pays for.

The source design's own framing (laziness, confusion, straightforward mistake) puts these at the centre, and the base-rate argument is plain: benign explanations have repeatedly turned out correct for behaviours that looked alarming — the source literature catalogues roughly ten such cases, including a blackmail rate that fell from **86% to 18%** when a single "analyse your situation" instruction was removed (`§1`, §3's seven motifs). Ruling these out is a substantial phase of the work, not a formality.

## Distinguishability

Two candidates with the same discriminating signature are one candidate. Before finalising the ledger, compare signatures pairwise: if no available manipulation separates a pair, merge them into a single candidate whose statement names the ambiguity — *"C2: the model read the setting as fictional (indistinguishable, with current surfaces, from reading it as consequence-free)"* — and record the merge in the log.

How to tell whether a distinction is real: ask what edit the two statements *disagree* about. If every answer is "none we can run", the distinction is metaphysics for this investigation, and the honest form is the merged candidate plus an equivalence-class entry in the final report naming the manipulation that was missing (`§9`).

The comparison runs again every time a candidate is proposed — by the Critic, by the open-ended scan, by evidence — *before* an id is assigned, and it is direction-for-direction on the available manipulations, not story-for-story: two statements that narrate different mechanisms but would move the same way under every edit this environment permits are one candidate here. A merged entry keeps both origins in its statement. The strictness is deliberate — a duplicate admitted at Critic time costs a column in every prediction table and a paragraph in every critique from then on.

## The ACH reordering rules

The ledger is a ledger, not an oracle (`§2` — no arithmetic over the matrix is the conclusion):

- **Evidence is scored against every candidate**, not only the one it was gathered for. Each pre-registered cell gets hit / miss / inconclusive / void.
- **Diagnosticity is what counts.** Evidence consistent with every live candidate has no value regardless of volume; rank by what is *inconsistent* with each candidate.
- **Only severe, pre-registered outcomes move ranks** — severe meaning at least one rival predicted differently (D12). Arguments go in the log as reasoning and move nothing.
- **Elimination requires confound enumeration first**, disconfirming evidence always, and — when the evidence is an absence — a positive control showing the test would have fired (D13, D14). Unproven is not disproved; a candidate lacking support stays `live (untested)`.
- **A candidate predicting nothing for any available manipulation is `inert`** — parked, not ranked, not eliminated; a later variant may make it testable (`§8`). The sweep runs every round over the *unrun* manipulations: a candidate whose every separator has already run and left it unmoved is not inert but a member of the equivalence class.

## Ledger economics

Generation outruns elimination structurally — four generators add, and only severe pre-registered outcomes remove, at roughly one per round — and the cost of a round scales with the ledger: every live candidate is a column in the prediction table, a derivation per cell, and a paragraph in every Critic critique. Left alone, ledgers of 12 grow to 18–25 with most candidates never resolved. Three disciplines, applied at 3d step 9 of `diagnostician_responsibilities.md`, keep the ledger affordable without lowering the admission bar:

- **Merge by signature**, strictly, at every admission (above).
- **Sweep for inertness** every round (above).
- **Tier the live candidates.** The *tested* tier — the top-*k* by rank (default k = 6, agreed at Phase 0), the must-exclude candidate always, and anything a round's variants directly target — carries full prediction rows. The rest are *carried*: still live, still ranked, still sent to the Critic, but with no rows and not sent to the Designer. A carried candidate cannot move on evidence it was not predicted on; it is promoted by rank or by being targeted, and demoted by rank only. The trade is explicit: the report's certifiable output is the best account with its alternatives named, and a candidate whose retirement would change nothing the report says belongs in the equivalence class, not in every table.

## Necessary vs sufficient

Label what each result licenses, in the PN/PS vocabulary of `model_forensics_principles.md §5`:

- **Ablation that removes the behaviour** → a lower bound on the **probability of necessity** (PN), for this context.
- **Amplification that induces it** → the **probability of sufficiency** (PS).
- Both are **bounds, not point estimates**, absent a monotonicity argument (no rollouts where the factor prevents the behaviour) — rarely defensible for language models. Only both together approach what "the cause" ordinarily means; never report "the cause" on an ablation alone.
- **The sufficiency follow-up obligation:** a PS result bears on the episode only once the feature is shown to have been present in the original transcript. Adding F and inducing the behaviour establishes F as *a* route, not *the* route taken.

## Non-exclusive candidates and recipes

Candidates may be jointly true (`§6`, INUS): a factor is typically an insufficient-but-necessary part of a recipe that is itself unnecessary-but-sufficient. Operationally:

- Record `Recipe membership` per candidate: `standalone` or `part of recipe R<n> with <ids>`, and maintain the `## Sufficient recipes under consideration` section of `CANDIDATES.md` with the interaction evidence for each.
- **Elimination runs over combinations, not pairwise:** retire a candidate only when it is neither necessary on its own *nor* a member of any surviving sufficient recipe.
- Expect the **collider artefact**: conditioning on "the behaviour occurred" makes independent causes look anti-correlated, so evidence for one candidate is not, by itself, evidence against another. The resample pool's negatives are the structural defence — reason about P(behaviour | context) across the whole pool, not only inside the positives.
- The **masking rule** is the recipe view applied to nulls: a factor with no effect in isolation is re-tested after the dominant factor is ablated, and the re-test is a carried-forward obligation if budget defers it (D10).

## Candidate origins matter

The ledger tracks origin because it is the scaffold auditing itself. If Critic-originated (`X<n>`) candidates never lead, the adversary may not be earning its cost — or may be being quietly under-ranked; if analyst-surfaced candidates repeatedly win, the initial CoT read is under-generating. The final report's *Candidate origins* section (Phase 5, item 8) aggregates exactly this, which is why origins are never overwritten and a merged candidate keeps both parents' origins in its statement.
