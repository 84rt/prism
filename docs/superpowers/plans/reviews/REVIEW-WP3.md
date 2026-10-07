# REVIEW-WP3 — Analyst instruments & measurement validity

Worktree `/workspaces/prism-wp3`, branch `wp3`. Nothing committed or staged. No API spend. Nothing under `/workspaces/forensics-cases/` modified.

## D-IDs

| ID | Status | Where |
|---|---|---|
| D08 extraction provenance + exhibit | closed | addendum §Validation metrics *Extraction provenance*; system prompt (build + load paragraphs); `forensic_checks.extraction_provenance` / `format_extraction_provenance`; guide §Interpreting item 2; regression test re-derives R01's 30 persisted solutions byte-for-byte from the logs |
| D25 per-condition precision audit | closed | addendum *Per-condition precision audit* (required when `loaded`), Summary flag `[precision drift: …]`; system prompt; `forensic_checks.precision_audit` (drift = Wilson upper < build precision); guide item 4 gives the Diagnostician the conservative/non-conservative judgement (the Analyst cannot make it without the mapping) |
| D06 awareness scanners | closed | addendum `### Eval-awareness split`: R06 iter-1 wording fixed verbatim (κ=0.767 / 0.588), mandatory verbatim quote + grep check against fabricated quotes, gate → `unmeasurable by scanner — hand-read`; guide item 9: awareness-dependent candidates marked `inert (awareness unmeasurable)` up front |
| D07 construal-check validity | closed | addendum *Construal checks*: scorability labels (`unvalidated` / `constant` / `collinear phi≥0.8`), `[provisional]` deterministic proxy void-only, construct guidance; `forensic_checks.check_scorability`; guide verdict table |
| D30 combined-blind delivery | closed (5 places) | addendum §Behaviour spec `construal_checks`; `subagent_invocation.md §Forensic mode`; `system_prompt.py` (`CONSTRUAL_CHECKS_RULE`, construal paragraph); `agent.py` (`normalise_construal_checks`, `## Construal Checks` section); responsibilities 3c bullet. Verified by capturing the built prompt (`tests/test_analyst_prompt_builder.py`) |
| D14 (policy) work order + split | closed | `FORENSIC_WORK_ORDER` rendered in system prompt and every request (`## Work Order`); addendum §Work order + *Split protocol* (by check group, never by condition); guide §Workload. Pairs with WP1's `subagent_max_turns` (not wired here — runner is WP1's) |
| D15 harvest deliverable | closed | responsibilities 3d step 7: dispose of every entry; required `Harvest:` / `Harvest: nothing harvested — <reason>` line; guide item 8 and pitfalls |
| D16 (scanning-model half) | closed | addendum §Scanning model and target family + *Instrument family* report line; system prompt *Instrument family*; guide §Scanning model and target family; invocation reference bullet |

## Files changed

- `.claude/docs/contracts/analyst_forensic_addendum.md` (rewritten in place; base contract untouched, still no pointer to the addendum)
- `.claude/docs/investigator/diagnostician_analyst_guide.md`
- `.claude/docs/investigator/subagent_invocation.md` — §Forensic mode only
- `.claude/docs/investigator/diagnostician_responsibilities.md` — only the 3c `construal_checks` bullet and 3d step 7
- `subagents/transcript_analyst/agent.py`, `system_prompt.py`; **new** `forensic_checks.py`
- **new** `tests/` (conftest, prompt-builder, forensic_checks unit, grader regression)
- Propagation for the new file: `install.sh` link_file, `uninstall.sh`, README tree (`GITIGNORE_ENTRIES` already covers `subagents/`; nothing in settings.json runs it — the Analyst runs under bypassPermissions)

## Tests

`uv run --directory /workspaces/prism-wp3 --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/ -q`

```
.....................................                                    [100%]
37 passed in 3.85s
```

Regression harness (`tests/test_grader_regression.py`), all offline over the persisted packages:
- case-6 `shutdown_disabled`: sha256 of `scanner.py` = recorded hash; recorded parquet outputs joined to `validation_set.csv` reproduce TP/FP/FN/TN = 52/0/0/38, κ = 1.00, Wilson [0.931, 1.0]; per-condition rates 42/50 (Wilson 71.5–91.7%) and 0/30 reproduce report_1c; the persisted deterministic labeller `groundtruth.py` re-run from a scratch copy over the 90 recorded transcripts reproduces the validation set exactly.
- case-5 `behaviour_grader`: hash reproduces; CSV grader outputs = parquet; confusion 32/0/0/48, κ = 1.00; rates 32/40 and 0/40 `[floor]`; tool-sequence + post-text rule re-derived over the two recorded logs reproduces all 80 gold labels.
- case-1 keying grader: hash reproduces; confusion 20/0/0/10, κ = 1.00, Wilson [0.839, 1.0]; 20/30 (48.8–80.8%); `extraction_provenance(span="score_answer")` over the baseline log matches the 30 persisted `validation_solutions/*.py` by sha256 and length (the D08 exhibit, re-derived); `last_assistant` and `score_answer` spans provably differ.
- Unit values from the archive: R06 iter-1 awareness confusion → κ 0.767 / P 0.844 / R 0.794; R01 drift 11/15 flagged, 5/5 not; R02 ceiling-everywhere → not scorable; R05 φ ≈ −0.94 → collinear.

## Invariants

- Score blackout: no change to what the Diagnostician reads before the Analyst reports. The new checkpoint rule is Analyst-side (artefacts under `artefacts/analyst/`); the guide states they are not read before the report.
- Immutable pre-registration: unchanged. The guide adds that a check's intended direction must be recorded at 3a before rates are read — strengthening, not relaxing.
- `agent.py` reads no new request keys beyond `behaviour_spec` sub-fields; `test_every_data_key_read_by_agent_is_documented_in_invocation_reference` holds.

## Code review (one reviewer subagent, against the eight D-IDs)

- All eight D-IDs judged closed; five D30 surfaces consistent; no ownership violations; invariants intact; stats spot-checked correct.
- **HIGH (fixed):** `normalise_construal_checks` raised `ValueError`, but `cli.py` only maps `RuntimeError` → exit 2, so the documented "exit 2" was wrong. Now wrapped as `RuntimeError` in `agent.py`; regression test added (`test_malformed_spec_surfaces_as_runtime_error_for_exit_2`). 38 tests pass.
- **MED (outside ownership, flagged for WP5 / propagation pass):** `diagnostician_responsibilities.md` "On receipt…" paragraph after 3c and 3d step 1 still say the Analyst *states* the verdict / `void (manipulation failed)`; superseded vocabulary is in the guide's §Deriving the verdicts. See judgement call 2.

## Open judgement calls for the owner

1. **`manipulation_check` kept as a one-element alias.** Minimal-change choice so 3a's "pass it as `behaviour_spec.manipulation_check`" (WP5's paragraph, L292) and `perturbation-design-patterns.md` stay valid. Owner may prefer to retire it once WP5 lands.
2. **Verdict stated by the Diagnostician, never the Analyst.** The old addendum asked the Analyst for `succeeded/failed/ambiguous`, which it cannot compute without the mapping (R02's point). Two paragraphs outside my ownership still say "verdict is stated per variant" by the Analyst: responsibilities L322 ("On receipt…") and 3d step 1 ("read the manipulation-check verdict"). Both still *work* because the guide has the verdicts derived before 3d step 1, but the wording should say "derive" — flagging for WP5.
3. **"Both measurements, every invocation" relaxed to "every round"** under the split protocol (one invocation carries the open-ended scan; the others say so under the heading). Needed to make by-check-group splitting legal; the non-negotiable's purpose (no confirmation-only rounds) is preserved.
4. **Thresholds chosen, not derived:** collinearity |φ| ≥ 0.8; precision-drift rule = audit Wilson upper < build precision; audit sample = all positives if ≤ 10 else ≥ 10; split trigger ≈ 24 scanner×condition passes or > 8 conditions.
5. **Eval-awareness gate.** Failed scanners' rates still go under Provisional (base-contract behaviour) but the section headline becomes `unmeasurable`; the Diagnostician marks awareness-dependent candidates `inert` (reusing `candidate-methodology.md`'s inert status) rather than inventing a new status.
6. `subagent_max_turns` is referenced as "once threaded through `runner.py`" (WP1); nothing in this WP reads it.
7. `forensic_checks.py` is a new installed file (five surfaces done). It is optional tooling — the addendum says "use it or reproduce its fields exactly".
