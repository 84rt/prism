# Perturbation Design Patterns

Design craft for discriminating variants. The Designer proposes and the Diagnostician disposes — these patterns are for judging the Designer's proposals, steering its requests, and sanity-checking the round before anything runs. The formal backbone is `model_forensics_principles.md` §2 (diagnosticity), §5 (PN/PS and headroom), §6 (interactions), §8 (severity), §11 (manipulation checks); the request/report shapes are in `designer_interface_contract.md`.

## Ablate vs amplify

The regime is derived from the environment rate (the 1c baseline in round one; the current variant's measured rate when extending one):

| Rate | Regime | Reasoning |
|---|---|---|
| Wilson lower bound > 0.20 | **Ablate** | Enough headroom to fall; look for what is necessary |
| Wilson upper bound < 0.20 but rate > 0 | **Amplify** | No headroom to fall; look for what is sufficient, then show the feature was present in the baseline |
| Rate = 0 across all resamples | **Terminate (1x)** | Behaviour not reproducible |

What each regime buys: an ablation that removes the behaviour gives a **lower bound on necessity** (PN) for this context; an amplification that induces it gives **sufficiency** (PS) — and a sufficiency result carries the follow-up obligation to show the feature was present in the original episode before it bears on the explanandum at all (`§5`).

The headroom traps at each end:

- **Ablating near the floor.** An ablation from 6% has nowhere to fall: the null it produces describes the elicitation regime, not the candidate, and scoring it against the prediction table as a miss eliminates explanations for free. The Designer computes a Wilson interval on the rate it is handed and reports it on the `**Headroom:**` line — read it before trusting any predicted `↓`.
- **Amplifying near the ceiling.** Symmetric: at 92%, nothing can visibly rise, and every amplification prediction is untestable this round.
- **The mid-range ambiguity.** With a CI straddling the threshold, regime choice is a judgement call; expect and welcome the Designer flagging individual off-regime variants — it rejects nothing, you decline a variant by not applying its diff.

## Binary vs parameterised vs factorial

- **Binary** when the candidate predicts presence/absence. The cheapest kind, and the default.
- **Parameterised** when the candidate predicts a **monotone response** — and then three or more levels, because a two-level "parameterised" variant is a binary one wearing a costume. A dose-response curve is worth the extra runs precisely when two candidates agree on the endpoints but disagree on the shape (a threshold story vs a gradient story), or when a disposition candidate has been repaired into a dose commitment (see `candidate-methodology.md §Anatomy of a candidate`). Choose levels to spread the predicted response: level-zero as the matched control, one level where every candidate expects an effect, one where they diverge.
- **Factorial** when two or more factors are plausibly non-additive. One-factor-at-a-time designs are formally blind to interactions, and a factor with **zero main effect can have a large conditional effect**. The worked case from the source literature: a country swap moved the rate 65% → 59% on its own — nothing — but produced a **16-point drop** once a second cue was also removed. A single-factor null on that factor, read naively, retires a true cause. Cost is the constraint: a full factorial over *k* factors is 2^k cells, so name the suspected interaction and run the specific cells that test it — the two singles, the double, and the all-off control — not the grid.

The masking rule is the factorial insight applied to elimination: **a factor showing no effect in isolation is not eliminated; it is re-tested after the dominant factor is ablated** (`§6`, and 3d step 5 in `diagnostician_responsibilities.md`).

## Decorrelated edits for indirect variables

Some candidates turn on a variable with no direct switch — "the model perceives time pressure", "the model reads the setting as consequence-free". The pattern:

1. Propose **three or more edits that reach the latent by different routes** — for time pressure: remove the deadline sentence; keep the deadline but extend it tenfold; add an explicit "no time constraints" line. Each moves the target; each has *different* incidental effects (length, register, information content).
2. **State per-edit what else it changes** — the incidental effects are the point of the design, not a nuisance: they are what decorrelates.
3. **Convergence across the edits is the evidence.** If all three move the behaviour in the predicted direction, no single confound explains it, because the confounds differ by construction. If they split, the split localises which surface feature — not the latent — was doing the work.

Never rely on a single proxy for an indirect variable: one edit's confound is indistinguishable from the effect.

## Designing for discrimination

The design driver is the pairwise question: **what do C1 and C2 disagree about?** The union of the live candidates' discriminating signatures defines the design space; the places where two signatures look alike define the hard problems.

- **Drop the elegant variant all candidates agree about.** Its expected information gain is zero however clean the diff — evidence consistent with every explanation has no diagnostic value (`§2`).
- **The value-of-information gate, per variant:** would any outcome here change which explanation leads? If not, don't run it. Prefer variants expected to split the live set closest to in half whichever way the result falls.
- **A discrimination-matrix row identical across all live candidates is a variant to cut**, and a Designer padding to budget instead of cutting is violating its contract — push back rather than running the padding.
- **An `n/a`-heavy column is information about the candidate**, not the variants: a candidate that predicts nothing for anything is inert, and the ledger records it as such.

## Confound patterns specific to forensics

Beyond the standard dirty-paraphrase checks (length, formatting, position, register — `eval_science_principles.md`), the recurring forensic confounds:

- **Availability vs appeal** — the most common one. An edit that removes the *opportunity* for the behaviour (the tool, the channel, the file) produces a drop that says nothing about propensity. Structural-impossibility edits are legitimate as negative controls (1c) and illegitimate as discriminating variants — a candidate about motivation predicts nothing about a world where the action is impossible. Check every ablation: did this lower the behaviour's appeal, or delete its precondition?
- **Ablating something the eval needs to stay coherent.** A model that can no longer understand the task exhibits a lower rate of everything. The Designer's *Eval coherence* risk heading and the Critic's blind diff review both watch for this; a rate collapse alongside garbled transcripts is a broken variant, not an effect.
- **Variant labels leaking into the prompt.** Condition names become directory and file names; a name like `no_pressure_treatment` that surfaces anywhere the model can read is a demand characteristic. Filesystem-safe, semantically flat names.
- **Length co-variation across the set.** Every ablation shortens; a round of five ablations is systematically confounded with prompt length as a set even if each diff is clean alone. The Critic's `### Cross-Diff Analysis` exists for exactly this; consider a padding control (matched-length anodyne insertion) when the whole round pulls one way.
- **The perturbation moving eval-awareness.** Every edit changes the environment's surface, and awareness is one thing that surface controls — an intervention can reduce the behaviour by making the setting *look more like a test*. The `### Eval-awareness split` per condition is the check; divergence between awareness movement and behaviour movement is a confound to resolve before scoring (`§1`, §12).

## Designing the manipulation check

Every variant carries one (D11): what would show the model's **construal** shifted, not merely its input text.

- **The test of a good check: it could come out negative.** "The deadline sentence is absent" cannot — it is true by construction — and is therefore worthless. "The transcript shows the model reasoning about time pressure" can, and is a check.
- **State it as an observable transcript property**, exactly like a behaviour description, with no direction of expected behavioural effect attached. It is graded by its own validated scanner in a separate pass from the behaviour grader (the addendum specifies the mechanics).
- **Where the target construal has no direct handle, borrow the surrogate-intervention pattern** (`§1`): insert something that forces the model to state its reading — an explicit "assess your situation before acting" step — then grade both what it states and whether the stated reading drove the action. Use with care: the insertion is itself a perturbation, so it belongs in a dedicated variant, not silently added to every arm. Avoid probes that would *update* the model's theory of mind in a way absent from the original rollout — asking "why did you do X?" can create the belief being tested for.
- **A variant with no statable manipulation check is a variant whose effect will be uninterpretable.** The Designer is contractually obliged to say so rather than ship it silently; treat such a variant as a redesign candidate, not a budget item.
- Interpretation is asymmetric and settled at 3d step 1: a null under a **failed** check is uninterpretable, never disconfirming.

## Cost patterns

Round cost = variants × models × samples — with one model frozen, effectively variants × samples, plus one condition per positive control (outside the budget by contract) and the occasional structural or padding control.

Where to spend when the budget forces a choice:

- **The value-of-information gate is the first filter** — the cheapest experiment is the one correctly not run. Apply it before any sizing question.
- **Prefer discriminating power over sample size** — a variant that separates the top pair at n=30 beats two agreeing variants at n=60 — *until CIs stop straddling zero*: once a round's outcomes keep landing inconclusive, the binding constraint is n, and doubling samples on fewer, sharper variants is the correct trade.
- **Match every variant's n to the baseline's** (the budget-defaults table in `diagnostician_responsibilities.md §Phase 0`) — asymmetric n buys asymmetric CIs for no design reason.
- **Positive controls are bought by elimination intent**, not by round: name an `elimination_target` only when retiring that candidate would change the leading explanation or move the equivalence-class boundary. Retirement that changes nothing the report says is a condition spent on tidiness.
- **Parameterised and factorial variants are multi-cell purchases** — three levels or three cells where a binary costs one. Buy them where the signature genuinely calls for shape or interaction (see above), and buy the specific cells, not the grid.
