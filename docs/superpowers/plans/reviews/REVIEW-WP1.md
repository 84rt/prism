# REVIEW-WP1 — Execution layer & launch infrastructure

Worktree `/workspaces/prism-wp1`, branch `wp1`, base `82f7861`. Nothing committed or staged.

## Deficiencies

| ID | Status | Where |
|---|---|---|
| D01 / D24 | **closed** | `scripts/execute_evals.py` is now the batch runner: takes the contract request JSON, builds commands, preflights, launches detached (`start_new_session`, own process group per child), keeps `status.json`, renders the draft report (`run`/`wait`/`status`/`report`). Executor protocol is launch → poll → report (`system_prompt.py`, contract §Execution Mechanism). Live: a Docker eval completed detached (run B — ~4 min, not the >10 min the done-when named; see §Live verification) and the Executor agent returned a conformant, score-free report after a 9-minute detached session (run A). |
| Engine scorer leak | **closed** | `verify_eval_log` no longer extracts `results.scores[*].metrics` at all; sample summaries are read for shape fields only; raw per-command stdout (which Inspect fills with running metrics) is quarantined under `<run_dir>/private/` and the diagnostic tail is scrubbed (`scrub_scores`, tested against real `--display plain` output). No `.private.json` sidecar is written — metrics are simply never read (judgement call 1). |
| D02 | **closed** | `DEFAULT_MAX_PARALLEL = 1`, `DEFAULT_MAX_CONNECTIONS = 4`; request `overrides` win; the agent can raise only via `run --max-parallel/--max-connections --rationale`, refused without a rationale, rationale printed verbatim in Concurrency Decision. `inspect_reference.md §Concurrency heuristics` updated. |
| D03 | **closed, with a caveat** | Allow entries added for the runner, launcher, `spend.py` and the sub-agent CLI. Verified in a scratch target carrying the case dirs' deny rules: the Executor's `uv run python scripts/execute_evals.py run …` call was **not** denied (run A). Caveat: the SDK reports *"Ignoring 135 permissions.allow entries … workspace has not been trusted"* in a fresh target, so allow entries only matter once the target has been trusted; deny rules still apply. The exact rule that denied R06's call was never pinned; with the default deny list it does not reproduce (judgement call 4). |
| D04 | **closed** | New `scripts/launch_subagent.py`: persistent `investigations/<name>/requests/<tag>_<role>_<n>.json`, absolute paths, `start_new_session` (setsid semantics) under a supervisor that records the exit code, receipt JSON, `status`/`wait`/`list`. Documented in `subagent_invocation.md §General Pattern`; `diagnostician_responsibilities.md §3b` tells the Diagnostician to use it. |
| D28 | **closed** | Launcher names every output `report_<tag>_<role>_<n>.md` (and `stderr_`, `cost_`, `receipt_`), `<n>` auto-incremented per (artefacts dir, role, tag). Tested: two iter-0 Critics get distinct files. |
| D36 (guard) | **closed** | Runner flags any pair whose final log completed zero samples: `zero_sample_pairs` in status, a shouted line in `status`/`wait` output, a fixed **Zero-sample pairs** line at the end of the report Summary, and a count in Execution Summary. Contract §Summary documents it. Live: the `goal_explicit` condition in run A (a `-T` that changes the sample id) exercised it — see §Live verification. Doc half is WP7's. |
| D12 (mechanism) | **closed** | `runner.py` writes `cost_<…>.json` from the SDK `ResultMessage` (`total_cost_usd`, `usage`, `model_usage`, `num_turns`, `subtype`, `permission_denials`) to `$PRISM_COST_FILE`, set per invocation by the launcher; always logs one `[cost]` line to stderr. Runner records per-pair `model_usage` from the log header. `scripts/spend.py <investigation>` sums both. Policy is WP5-A's. |
| D14 (mechanism) | **closed for the Executor; one-liner pending elsewhere** | `subagent_max_turns` → `run_agent(max_turns=…)`, `DEFAULT_MAX_TURNS = 100`; a no-output exit now names `subtype`/`num_turns`/`max_turns`. Threaded through `experiment_executor/agent.py` and documented for all agents in `subagent_invocation.md`. The other five `agent.py` builders are outside WP1's ownership (`transcript_analyst/agent.py` is a named hotspot) so were **not** touched — see §Follow-ups. |

## Files changed

Modified: `scripts/execute_evals.py` (rewrite; engine functions preserved, legacy `commands` form still works), `subagents/runner.py` (cost sidecar, `max_turns`, no-output detail), `subagents/experiment_executor/agent.py` (persists request file, threads `subagent_max_turns`), `subagents/experiment_executor/system_prompt.py` (protocol rewrite), `.claude/docs/contracts/executor_interface_contract.md` (§Conditions, §Overrides, new §Execution Mechanism, §Summary zero-sample line, §Preflight quarantine, §Stdout convention, exclusion reason), `.claude/docs/investigator/subagent_invocation.md` (§General Pattern + `subagent_max_turns`), `.claude/docs/references/inspect_reference.md` (§Concurrency heuristics), `.claude/docs/investigator/diagnostician_responsibilities.md` (3b paragraph only), `.claude/settings.json` (allow entries), `install.sh` (link list + `GITIGNORE_ENTRIES`), `uninstall.sh`, `README.md` (file tree).

New: `scripts/launch_subagent.py`, `scripts/spend.py`, `tests/` (`conftest.py`, `test_execute_evals.py`, `test_launch_and_spend.py`, `fixture_inspect_plain_stdout.txt`).

## Tests

```
uv run --no-project --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/ -q
.............................                                            [100%]
29 passed in 31.54s
```

Coverage: scorer leak (verify_eval_log, sample_shape, scrubber on synthetic and on real captured Inspect output, legacy stdout, status/report/wait stdout against a scored fixture log); request→command rendering (policy flags, routing `-M` JSON, sample_id/epochs probe rule, multi-model log dirs, validation); D02 sources and rationale gate; unrecognised/`runs_per_condition` notes; stale-log refusal/quarantine; foreground worker with preflight, structural exclusion with evidence, sample-level mapping, zero-sample flag; draft report headings verbatim in order; detached round trip (different session id, `wait` exit 3 then 0, CLI receipt); launcher persistence/naming/exit-code/listing; cost sidecar from a `ResultMessage`; `max_turns` reaching `ClaudeAgentOptions` (default 100) and the Executor's builder reading it; `spend.py` sums.

Offline against real Inspect (mock model, case-3 frozen task, scratch target installed with `install.sh`): foreground and detached `run` both completed through preflight and full execution; preflight tree quarantined; report rendered. The captured plain-display stdout became `tests/fixture_inspect_plain_stdout.txt` and exposed a scrubber gap (column-style `accuracy  0.000`, not `name: value`) that is now covered.

## Live verification

Both runs were done in scratch targets built from the case dirs (`env/` copied, `.env`/`.venv` symlinked, `install.sh` from this worktree so the case dirs' deny rules and the new allow entries were the merged settings). Spend announced and approved beforehand; total ≈ $1.

**Run A — Executor agent, deepseek-r1 blackmail case (case 3), 2 conditions × 5 epochs, preflight on.** Launched with `launch_subagent.py launch executor … --tag 00_resample`; receipt, request copy and per-invocation report/stderr/cost files all landed where documented. The Executor (Opus 4.8) ran `execute_evals.py run`, polled with `wait`, read the draft, and returned a report in 6 turns, 9 min wall clock, **$0.66** (from the cost sidecar). Report conformance: every contract heading verbatim in order, `None.` sections present, Concurrency Decision fields populated, zero-sample line present, no scorer token anywhere. The `baseline` pair completed 5/5 (316 s, route `openrouter:novita`). The second condition, `goal_explicit`, was deliberately built to change the sample id under a pinned `sample_id` (the D36 scenario): Inspect exits 1 with "No matches in dataset … for sample_id filter", so the runner classified it structural at preflight and the Executor correctly diagnosed it as the orchestrator's to fix and did not alter `sample_id` or the `-T` argument. So D36's *live* manifestation on Inspect 0.3.263 is a preflight exclusion with a self-explanatory tail, and the zero-sample guard covers the other shape (a success log with 0 completed samples), which is unit-tested. One known cosmetic: the Opus preamble before `### Summary` appeared (the caller-side "read from the first heading" rule already handles this).

D03: the Executor's `uv run python scripts/execute_evals.py run …` was **not** denied under the deny rules. Its stderr shows `Ignoring 135 permissions.allow entries from .claude/settings.json: this workspace has not been trusted`, so in a fresh target the allow list is inert and only deny rules apply; the shipped deny list does not match the runner call.

Run A also exposed that the timing-based sandbox detector misfires on slow reasoning models (133 s API-only probe → "sandbox: yes"). Fixed: timing now only confirms a sandbox when compose/Docker files are present.

**Run B — one `reward_hacking` sample (case 1) on gpt-5 in Docker, runner only, detached.** `run` returned a receipt immediately; the launching shell exited; the worker ran in its own session and completed (1/1 samples, 193 s eval, ~4 min wall clock including image build); `wait` returned exit 0; the draft report is score-free and shows `Sandbox detected: yes`. **Shortfall against the done-when:** the brief asked for a >10 min eval; gpt-5 solved this sample in ~3 min. The detach property does not depend on duration (the worker outlived its launcher by design, exactly as in run A's 9-minute Executor session), but a >10 min completion has not been observed. A 30-epoch resample would exercise it at ~$10–30.

## Code review (superpowers:requesting-code-review) and what changed

Verdict was "with fixes". Applied: (1) scrubber name class widened to `\S+` for grouped/bracketed metric names, with probes in the test; (2) launcher no longer writes `status.json` after spawning the worker (`worker.pid` file instead), `wait`/`status` treat a dead worker as terminal (`failed`, with an error line), and `_pid_alive` no longer counts zombies as alive — new test kills a live worker and checks `wait` returns; (3) launcher's supervisor wrapped so a failed `Popen` writes `state: failed` into the receipt and stderr, the supervisor records its own pid, and `wait` has a 60 s grace period for a `launching` receipt; (5) `zero_samples` now requires a final log; (6) a failed attempt's `.eval` is moved to `private/failed_attempts/` before a retry; (7) the CLI `run` test uses `mockllm/model`; tautological assertion fixed; `.pytest_cache/` gitignored; `wait` honours SIGINT. Not applied: (4) threading `subagent_max_turns` into the five other builders — outside WP1's file ownership; the doc now states plainly that only the Executor honours it. Minor items left open: timed-out commands leave no capture under `private/`; `sample_id` given as a list is not rejected by validation; pid reuse after a host restart is not guarded; the shared `logs/_preflight/` root would collide if two runs of the same experiment preflight concurrently.

## Invariants

- **Score blackout.** No scorer output reaches stdout, `status.json`, `draft_report.md`, or the Executor's report: metrics are not read; sample summaries yield shape fields only; raw streams live under `private/` and the prompt forbids reading it; failed-command tails are scrubbed. Tested with a fixture whose metric names/values are asserted absent from every readable surface.
- **Immutable pre-registration table + deferred scoring.** Untouched: nothing here reads or writes `INVESTIGATION-LOG.md`/`CANDIDATES.md`, and the Analyst path is unchanged. The Executor still reports before any scoring happens.

## Propagation checklist

- [x] Contract (`executor_interface_contract.md`) — request fields, execution mechanism, report Summary line, conventions.
- [x] `subagent_invocation.md` — §General Pattern (launcher), `subagent_max_turns`.
- [x] System prompt (`experiment_executor/system_prompt.py`) — protocol rewritten around the runner.
- [x] `agent.py` prompt builder — reads `subagent_max_turns`; writes the request file the runner consumes (`**data` hazard checked by test).
- [x] Diagnostician doc — 3b paragraph only (launcher, polling, zero-sample line).
- [x] Report consumer — draft report carries every contract heading verbatim; the Diagnostician reads the same shape.
- [x] Mechanical surfaces for the two new scripts — `install.sh` link list, `GITIGNORE_ENTRIES`, `uninstall.sh`, README tree, `settings.json`.
- [ ] Places outside WP1 ownership that still say "save stdout to `artefacts/executor/report.md`" — see §Follow-ups.

## Open judgement calls for the owner

1. **No `.private.json` metrics sidecar.** The fixes doc said metrics go "if at all" to a sidecar; I chose not to extract them anywhere. Nothing in the pipeline consumed them. If an audit use exists, add it back behind `private/`.
2. **Preflight logs are quarantined, not deleted.** The contract said "deleted"; I moved them to `<run_dir>/private/preflight_logs/` (Orchestrator doc prefers quarantine) and updated the contract sentence.
3. **`command_timeout_seconds` override key added** (default 4 h). The old 1 h engine timeout would kill a 30-epoch sandboxed run.
4. **D03 root cause remains unpinned.** With the shipped deny list the Executor's runner call is not denied; R06's denial may have come from a compound command (`cd … && …`, a pipe) or a local override. The allow entries are correct for a trusted target; consider adding the trust note to the install output.
5. **Multi-model log layout** changed to `logs/<condition>/<model-sanitised>/` when a request names more than one model (single-model requests are unchanged). The old engine rejected duplicate log dirs, so the previous agent had to improvise here anyway.
6. **Stale-log policy.** `run` refuses a pair whose log dir already holds `.eval` files unless `--quarantine-existing-logs` (moves to `<experiment_dir>/logs_quarantined/<run_id>/`). Chosen over silent append (doubles the Analyst's pool) and over silent quarantine.
7. **Re-run affordance is `--only` + a second run directory**, and the Executor is told "one re-run, no more". The agent folds both runs into one report by hand; if that proves error-prone, a `merge` subcommand is the next step.
8. **The Executor still runs for the whole eval duration** (polling with `wait --timeout 540`, Bash tool timeout 600000 ms). At 100 turns that bounds a run to ~15 h of polling. An alternative — the Diagnostician launching the runner directly and invoking the Executor only for the report — was not taken because the brief keeps the Executor as the interface.

## Follow-ups outside WP1 ownership (not done)

- `subagent_max_turns` one-liner in the other five `agent.py` files: `max_turns=data.get("subagent_max_turns")` in each `run_agent(...)` call (`environment_explorer`, `transcript_analyst` [WP3/WP6 hotspot], `environment_mapper`, `variant_designer`, `redteam_critic`). Without it the field is inert for those roles.
- "Save the Executor's stdout report to `artefacts/executor/report.md`" wording in `orchestrator_responsibilities.md §Step 2d` (item 4), `diagnostician_responsibilities.md §1b` (L165, WP5's paragraph), `.claude/skills/orchestrate/SKILL.md` L52 and `.claude/skills/diagnose/SKILL.md` L44: the launcher now writes the per-invocation file directly. Same for "run sub-agents via Bash… capture stdout" in both SKILL.md files.
- `orchestrator_responsibilities.md §Step 2d` still says the orchestrator "launches via CLI script"; should point at the launcher.
