# REVIEW-WP5 — Diagnostician governance, budget & ledger economics

Worktree `/workspaces/prism-wp5`, branch `wp5`, base `82f7861`. All changes uncommitted; no git operations beyond read-only `git status`/`git diff`. Nothing under `/workspaces/forensics-cases/` was modified; no API spend.

Prose/policy work only. No new files apart from this note, so the five-surface propagation rule (install.sh `link_file`, `GITIGNORE_ENTRIES`, uninstall.sh, README tree, settings.json) does not apply — checked and confirmed no new file was needed. Both invariants (score blackout; immutable pre-registration table + deferred scoring) are re-stated rather than weakened by the new text; see the per-half notes.

---

## Half A — cost & speed

### D-IDs closed

| ID | Status | Where |
|---|---|---|
| D29 | closed | `diagnostician_responsibilities.md` 3d step 9 (new: merge-by-signature / inertness sweep / tested-vs-carried tier); Phase 2 Critic bullet (merge before assigning an `X<n>` id); 3a pre-registration paragraph (rows for the tested tier only); §State Management `CANDIDATES.md` template (`*Tier*` bullet), log template (`Merged`, `Tier changes`, tested-tier note on the prediction table); Anti-Pattern #22. `candidate-methodology.md` gains a *Ledger economics* section, the tier in the anatomy, and the strict Critic-time merge rule under *Distinguishability*. `SKILL.md` Autonomy Agreement gains the tier size *k* as an agreed parameter. |
| D13 | closed | `diagnostician_responsibilities.md` §Budget defaults — table recalibrated (n = 100 default / 50 when episodes are expensive; match baseline; a sizing rule; 2–3 variants; concurrent control per round outside `variant_budget`; iteration cap unchanged; spend cap + Phase 4 reserve row), with the economics stated (episodes cost cents, invocations dollars, favour n over breadth). |
| D37 | closed | 1b bullet "Do not read determinism off the task's configuration" (`seed`/`temperature=0` no-ops); Budget table row "Noise-floor draws ≥ 3"; 3a "Comparators and the concurrent control" (the control is the cheap source of identical draws); 3d step 2 noise-floor gate (`inconclusive (within noise floor)` once k ≥ 3; `[floor uncharacterised]` tag below 2× the Newcombe–Wilson half-width until then; retroactive reversal via the current round's delta only); log template lines for identical draws / noise floor; Anti-Pattern #21. |
| D12 (policy) | closed | §Stopping rule, *Budget exit*: `scripts/spend.py` run before each round and before Phase 4; running total + next-round projection written into `### Decision`; degrade-in-order before exit; the exit "fires as a decision, never as an interruption"; an external stop is an interruption handled as deferred scoring. Phase 4 reserve introduced in the budget table and the closing sentence. Log template: `Cost basis` in Agreed Scope, `Spend:` line in `### Decision`. `SKILL.md` Autonomy: spend ceiling observed via `spend.py` + Phase 4 reserve. |
| D31 | closed | Phase 2 Critic invocation paragraph: "One Critic, by default…" — a second only when a decision turns on one Critic's judgement of the leading or must-exclude candidate. (Half B extends the trigger list with the same-family-Diagnostician case, D16.) |

Not closed: none in half A.

### Files changed (half A)

- `.claude/docs/investigator/diagnostician_responsibilities.md` — sole-writer file; the five paragraphs owned by WP1/WP2/WP3/WP6 verified byte-identical against HEAD (`git diff -U0` hunks grep'd for their text: no hits; each paragraph's opening string still present once in the working file).
- `.claude/skills/diagnose/candidate-methodology.md`
- `.claude/skills/diagnose/SKILL.md` (Autonomy Agreement only)

### Verification (half A)

- Doc-only: no tests to run. `git diff --stat` at review time: 3 files, +57/−24 (before review fixes).
- Untouchable-paragraph check: passed (see above).
- Cross-reference check: `grep` over `.claude/` for `3d step` references — only steps 1, 5 and 7 are cited elsewhere, so inserting step 9 (triage) and renumbering the ledger-update step to 10 breaks nothing.
- Template worked example: the new `CANDIDATES.md` template is additive (one `*Tier*` bullet; header line unchanged) against R06's ledger at `/workspaces/forensics-cases/case-6-quiz-agent-shutdown/investigations/shutdown_bypass/CANDIDATES.md`. R06's iteration-1 ledger (12 live, 3 tied at rank 1, C4 must-exclude at rank 12) under the rule "top-6 + must-exclude + targeted" would have tested C1/C8/X11/C6/C9/C2 + C4, and carried C3/C5/C7/C10/X12 — R06's iteration-2 design in fact targeted C4 and the C1/C8 split, so the tier would have matched what it ran while halving the table width. Its iteration-1 delta (`C7 → inert`, `X13 → X12` merge, `X14 → C9` merge) is exactly the triage step 9 now prescribes.
- Reviewer subagent outcome: the half-A reviewer was stopped by the coordinator before returning and was not re-spawned; half A is covered by the combined A+B review recorded in the closing section.

### Open judgement calls for the owner (half A)

1. **n = 100 default.** The register's evidence is that n = 30–50 leaves ~20pp effects inconclusive and R02's n = 60 still did; the Newcombe–Wilson half-width on a difference is ≈ ±14pp at n = 100 per arm. Eval cost at $0.03–0.06/episode makes this ~$6 per arm, but wall-clock on sandboxed evals at `max_parallel: 1` is the real constraint, hence the 50 fallback "when an episode is expensive". If you would rather keep a single number, 60 with the sizing rule is the conservative alternative.
2. **Carried candidates are not sent to the Designer.** The Designer contract says "only `live` candidates are sent", which the tested tier satisfies; but it means a carried candidate can only be promoted by the Diagnostician choosing to target it. Alternative: send carried candidates as compact one-liners. I chose the cheaper option because the Designer's `variant_budget` is 2–3 and the design space should be the tested tier's signatures.
3. **The `[floor uncharacterised]` provisional threshold (2× the Newcombe–Wilson half-width)** is my own heuristic — it is the smallest difference that would still resolve if either arm's true rate were off by one half-width. It is applied as a tag that blocks elimination/eager exit/Full Success on that row alone, not as a change to how the row is scored, so the table is still scored as written. R06's iteration-1 `remaining_one` (−36pp, half-width ≈ 23pp) would have carried the tag until a third identical draw existed; its `remaining_zero` (−84pp) would not.
4. **Concurrent control as default comparator.** This generalises what R01 adopted at iteration 2 and R05 used throughout. `executor_interface_contract.md §Resample runs` (WP1's file) still says "every later comparison is drawn against this run" — true of the anchor, slightly stale for the comparator. Flagged for WP1/Wave 3 rather than edited here.
5. **WP4 cross-reference.** `scripts/ledger_to_json.py` should read the new `*Tier*` bullet so the Designer request can be built from the tested tier only; the Critic request still takes the whole live ledger.

---

## Half B — governance, firewall policy, Phase 4

### D-IDs closed

| ID | Status | Where (all in `diagnostician_responsibilities.md` unless noted) |
|---|---|---|
| D17 | closed | §Sub-Agent Failure Handling, new *Degraded-mode policy*: three classes (substitutable/no pause; labelled substitute/round downgraded; pause-forcing), `### Deviation` record, deferral as the cheap option; cites WP2's route failover (`subagent_invocation.md §subagent_route`) and WP1's sanctioned batch runner (`scripts/execute_evals.py`). The "Critic failure is a gate failure" bullet now points at the policy. `SKILL.md` Autonomy: *Degraded-mode autonomy* bullet makes the run-to-conclusion vs pause resolution an agreed parameter. Anti-Pattern #23. |
| D18 | closed | Phase 4 *Without a Critic*: second cross-family route → pre-committed factor rule (agreed at Phase 0; line in the Agreed Scope template) → labelled self-chosen substitute; the lost agreement check stated. |
| D19 | closed | Phase 4 *What it is for* (both outcomes informative; a failed Phase 4 that downgrades to Constrained is the pipeline working) and *When it beats another Phase 3 round* (leader with ≥ 2 severe hits and only non-leading separators left, vs tied lead / untested must-exclude / outstanding masking re-test; the after-a-failed-Phase-4 rule). |
| D35 | closed | §Stopping rule *Termination precondition — the must-exclude candidate* (clean test defined; eager exit blocked while untested; quiet/budget exits carry "untested-dangerous" into the report); Phase 5 item 1 requires the must-exclude status in the primary finding; the `*Tier*` bullet marks the slot; Anti-Pattern #24. |
| D34 | closed | 3a *Check content invariance on register and framing variants* — stake-bearing propositions of retained content checked for intensity, not presence. |
| D20 | closed | 3d step 1 *Interpretation versus override* — an `ambiguous` verdict may be re-read only against a pre-registered purpose, on reported rates, recorded, narrowing never rescuing; `failed` never re-read. |
| D21 | closed | 3a *Comparators* paragraph, second part: derived-arm comparator with four validity conditions and its inherited cost; comparator column in the table. |
| D16 (Diagnostician half) | closed | Phase 0 *When the Diagnostician shares the target's family* — three mitigations; Agreed Scope line; D31's second-Critic trigger extended with this case. |

Not closed: none.

### Files changed (half B)

Same three files. Untouchable paragraphs re-verified after half B: `git diff -U0` hunks contain none of their text; the five opening strings each present once in the working file.

### Verification (half B)

- Doc-only; no tests. Final `git diff --stat` in the closing section.
- Cross-references: `subagent_invocation.md §subagent_route` exists (WP2 extends it under the same heading); `scripts/execute_evals.py` exists (WP1 grows it under the same name).

### Open judgement calls for the owner (half B)

6. **Critic-less rounds may be executed but not concluded.** A Critic-reviewed, pre-registered round may run and be scored without a Critic, but eliminations/retirements/Full Success are held as `pending critique`. This sits between R04's "continue" and R01's "defer". If you prefer R01's stricter stance, delete the "may still be executed and scored" clause.
7. **Pre-committed Phase 4 factor rule** ("highest-listed unvaried Mapper surface") is Critic-independent but weak — the Mapper's ordering is not a relevance ordering. Hence it is the *second* fallback, after a route swap, and the round is labelled.
8. **Eager exit blocked while the must-exclude candidate is untested**; quiet and budget exits are not blocked (they may lack the budget to build the test) but must carry the line into the primary finding. The stronger alternative — force one must-exclude-targeting variant by iteration 2 — is recorded as a per-round question in 3a, not a hard constraint.
9. **Content-invariance check is Diagnostician-side prose**, not a Designer-contract rule (WP6 owns that contract this pass). Worth a one-line mirror in `designer_interface_contract.md` at Wave 3.

---

## Combined A+B review outcome

One reviewer subagent covered both halves (the half-A reviewer was stopped before returning). Verdict: **ready with fixes** — no Critical; four Important, all applied. Per-deficiency table: all 13 IDs closed (D19 was "partial" pending Important #1, now fixed). Forbidden paragraphs (i)–(v): pass. Invariants: intact, reinforced. Statistical claims confirmed approximately right (Wilson ±16.5/±13.1/±9.4pp at n = 30/50/100; Newcombe–Wilson on a difference ±24/±19/±13.6pp). No register IDs or run names in added text; no scope creep.

Fixes applied after review:
1. Phase 4 timing vs "every exit proceeds to Phase 4": clause added — the timing guidance governs the *choice* to leave Phase 3; once an exit fires, Phase 4 runs on the reserve; a tied lead predicts from the top-ranked candidate alone.
2. Analyst-lost `[provisional]` proxies must be pre-registered at 3a, never built after the arms run; the Analyst-bypass bullet now names running the frozen grader as the one sanctioned exception.
3. `### Deviation` block added to the iteration log template.
4. 3a manipulation-check paragraph now requires the load-bearing contrast(s) to be written down; `Manipulation checks:` line added to the Designer Summary block (what 3d step 1's interpretation rule leans on).
5. Minors: "unmeasured" → "executed-but-ungraded"; the noise-floor reversal also re-records the row in the current Prediction Scoring block; the extra outcome tags are listed in the template note; `candidate-methodology.md` says the tier is first assigned at the Phase 2 ranking.

Not applied (owner's call): a consequence sentence for a drift finding (reviewer minor — I left the frozen-setting rule as the only hard consequence rather than inventing a threshold); a coordination-table line telling WP4's serialiser to read `*Tier*` (judgement call 5); where `REVIEW-WP5.md` should live.

Final `git diff --stat`: `diagnostician_responsibilities.md` +98/−31 across the three files (`SKILL.md` +4/−1, `candidate-methodology.md` +14/−2 included). Untouchable paragraphs re-verified after the fixes: no hunk contains any of their text.
