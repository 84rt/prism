# Pre-push audit fixes — implementation review

Worktree `/workspaces/prism-audit` (branch `audit`, base `4b0f0f7`). Everything is uncommitted; no git
writes, no API spend, `/workspaces/forensics-cases/` untouched.

## What was done

### A1 — sub-agent isolation (`subagents/runner.py`, `scripts/launch_subagent.py`)
- `run_agent` now builds `ClaudeAgentOptions` with `setting_sources=[]`, `tools=` the role's allowed list
  (+ `Write` when the runner injects it for memory), `skills=[]`, `strict_mcp_config=True`.
- Two channels don't go through a settings source: managed `CLAUDE.md` files and auto-memory. The env vars
  `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` close them (`ISOLATION_ENV`).
  Both variables were confirmed in the bundled CLI of SDK 0.2.165. The route env is merged on top.
- One project setting sub-agents relied on — `Read(**/.env)` / `Read(**/.env.local)` deny — is now passed
  explicitly (`SUBAGENT_SETTINGS` via `settings=`).
- **Checked with no spend:** I captured the HTTP request the CLI sends, against a local fake API endpoint
  (`/tmp/iso_check/check.py`).
  - Before (legacy options): 21 tools offered, including Skill, Agent, Edit, NotebookEdit and Cron*. The
    request body contained the user's `CLAUDE.md` (the owner's name), `MEMORY.md` content and superpowers
    skills.
  - After: tools `['Grep', 'Read']` only, and none of those strings appear.
- **SDK pinned:** `CLAUDE_AGENT_SDK_REQUIREMENT = "claude-agent-sdk==0.2.165"` (the version the suite runs
  against) is used in `build_command`. It is documented in `subagent_invocation.md` (General Pattern +
  new **Isolation** paragraph), the launcher docstring and the test recipe in `tests/conftest.py`. A test
  asserts the pin equals the installed version.

### A2 — file firewall across tools (`runner.py` `_build_hooks` + helpers)
- **Normalisation:** every path is normalised before matching. Both `normpath(abspath)` (relative paths
  resolve against the hook's `cwd`) and `realpath` (symlinks) are checked, and a path is denied if either
  matches.
- **Pattern semantics:**
  - Wildcard-basename and suffix semantics are unchanged.
  - Directory patterns now match at a path-component boundary, and also the directory itself. Shell
    wildcards inside a directory pattern span components (`artefacts/executor/runs/*/private/`).
- **Read:** checks `file_path`.
- **Grep/Glob:**
  - Both check `path`, plus `path` joined with Grep's `glob` or Glob's `pattern`.
  - **Grep over a directory** additionally does a bounded walk (`GREP_WALK_LIMIT=20000`; hidden dirs
    skipped, as ripgrep does; the `glob` filter honoured). It denies the search if a restricted entry lies
    beneath, because a content search would quote the label map without ever naming it. The denial
    message tells the agent to narrow `path`/`glob`.
- **Bash (best-effort, documented as such):**
  - Path-shaped words (contain `/` or name an existing entry; `--opt=value` split) are normalised and
    checked.
  - Each pattern's fixed stem (`CONDITION_LABEL_MAP`, `CANDIDATES.md`, …) is searched in the command text.
  - The section comment and `subagent_invocation.md` say it catches accidents, not intent, and that an
    OS-level sandbox is future work.
- **Label map denied to every sub-agent:** `runner.ALWAYS_RESTRICTED` holds `CONDITION_LABEL_MAP*` and is
  merged into every `run_agent` call, so no builder can forget it.
- **Block lists:**
  - **Analyst:** adds `requests/`, `artefacts/{designer,critic,explorer}/` and `RESUME.md`, on top of the
    log, ledger and label map it already had.
  - **Designer:** the dead `executor/report.md` and `analyst/report.md` entries are replaced by the
    directories `artefacts/executor/` and `artefacts/analyst/`.
  - **Critic:** the list is now mode-dependent (`restricted_files_for(mode)`). `diff_review` adds
    `artefacts/designer/`, `requests/` and `INVESTIGATION-LOG.md`. `candidate_critique` and `prediction`
    keep the base list; `prediction` must read the log it is sent.
  - **Executor:** adds `artefacts/executor/runs/*/private/` and `logs_quarantined/`.

### A3 — real-run bugs
1. **Per-condition cwd** (`execute_evals.py`):
   - New `task_location()`; each pair is planned with `PairPlan.cwd` set to the task file's directory, and
     the task argument becomes its basename (`.` for a task directory). Log dirs stay absolute.
   - Preflight, retest and the full run all use `p.cwd`, falling back to `experiment_dir` for old plans.
   - Gotcha 11 and the executor contract now say the runner does this.
   - Test: two sibling subdirectory tasks, with the cwd recorded by the real worker.
2. **Error result:** an `is_error` result that isn't credential exhaustion now raises `RuntimeError`
   naming subtype, stop_reason, num_turns and max_turns. The cost sidecar is written first.
3. **"dataset"** is replaced with `DatasetNotFoundError`, `DatasetGenerationError`, `dataset not found`,
   `No such dataset` and `Couldn't find a dataset`. Tested against the real plain-display fixture.
4. **Scrubber:**
   - `_METRIC_COLUMNS` now allows single-spaced metric names. Tested on the real `task_scores` grid
     including grouped metrics.
   - `log.error.message` is scrubbed before it enters `errors`.
5. **Dead process:**
   - `launch_subagent.py` `status` and `wait` exit **4** for a dead process (`exit_code_for`,
     `EXIT_DEAD`). This is documented in `subagent_invocation.md` and the launcher docstring.
   - Receipts record `pid_start` and `supervisor_start` (`/proc/<pid>/stat` field 22). Both
     `_pid_alive` implementations compare them, and `execute_evals.py` stores the worker's start time in
     `worker.pid` and `status.json`.
6. **Credentials:**
   - `insufficient_quota` and `you exceeded your current quota` are added to both pattern lists, which
     stay identical (a test pins this).
   - The runner no longer classifies `result_message.result` unless `is_error`.
   - `sample_shape` counts only provider-shaped errors (`is_provider_credential_error`: credential
     pattern plus an SDK-error or HTTP-status shape).
   - There is one documented example test each for Anthropic "credit balance is too low", OpenRouter
     "insufficient credits" and OpenAI `insufficient_quota`, plus a negative task-level example.
7. **`model_routing`:** keys not in `models`, and non-mapping entries, are rejected.
8. **SDK errors:** any `ClaudeSDKError` (e.g. `CLIJSONDecodeError`) becomes a chained `RuntimeError`
   (exit 2), and the cost sidecar is still written.
9. **Races:**
   - The launcher reserves `n` with `O_CREAT|O_EXCL` (`reserve_index`, replacing `next_index`).
   - Receipts are written atomically (temp + `os.replace`), and so is the runner's cost sidecar.
   - The Executor's `_persist_request` uses exclusive create with a numeric suffix on collision.
10. **Allow-list** (`.claude/settings.json`):
    - `Bash(uv run inspect *)` is replaced by `--version`, `--help` and `list *`.
    - `uv run inspect eval *`, `eval-set *`, `eval-retry *`, `log *` and `view[ *]` are now **deny**.
    - The `--no-project … execute_evals.py` allow entry is removed; no doc showed that form.
    - The direct sub-agent CLI allow entry now uses the pinned SDK.
11. **Smaller fixes:**
    - Every attempt's token usage is now recorded per pair (`usage_attempts`: preflight probe, retest and
      each full-run attempt, including retried ones), and `spend.py` sums it, falling back to
      `model_usage` for old status files.
    - The Bash hook blocks a standalone `&`, `nohup`, `setsid` and `disown` (shlex-tokenised, so `&&`,
      `2>&1`, `&>` and quoted `&` pass).
    - Dead code removed: `_ = by_id`/`by_id`, `classify_error`'s `log_status` parameter (callers
      updated) and the unused `pid` in `spend.py`.
    - B904 chaining restored in the runner (`CLINotFoundError`, SDK errors) and in
      `openrouter_endpoints._get_json`.

### A4 — `execution_notes`
The five places:
1. Contract §Execution notes, with an example.
2. A `subagent_invocation.md` bullet and an example key.
3. A system-prompt paragraph in the base prompt, so both modes get it.
4. The `agent.py` builder, `_execution_notes_section`:
   - It accepts a string or a list of strings.
   - It renders `## Execution Notes` with `EXECUTION_NOTES_RULE` ("data quality, never hypothesis or
     condition identity").
   - Blank input is skipped; any other shape is an agent error.
5. The lead docs:
   - `orchestrator_responsibilities.md` Step 2e item 7 now says what may and may not go in the field.
   - The three "delegation brief" mentions now point at the field.
   - `orchestrator_analyst_guide.md` §Transcript validity screening and `diagnostician_analyst_guide.md`
     (forward attrition) are updated too.

`check_blinding.py` already scans the whole request, so real names in the notes block. The prompt was
verified by capture in the tests: both modes, string, list, blank and malformed.

### Review follow-ups (Sonnet reviewer; no Critical findings)
The reviewer verified all five invariants: score blackout, immutable pre-registration, no forensic text in
Orchestrator mode, label map denied to every sub-agent, and isolation plus path tricks. I fixed its three
Important findings and Minors 4, 5 and 9:
- **Bash wildcards and recursion.** A wildcard word is checked as each of its expansions (capped at
  `BASH_GLOB_LIMIT=500`). A recursive search (`grep -r/-R`, `rg`, `find`, `ls -R`) walks every directory
  it names, or the cwd, like a Grep root.
- **Heredocs.** Heredoc bodies are stripped before the background check, so a pandas `&` inside
  `python - <<'EOF'` is no longer blocked.
- **`requests/` anchored.** The pattern is now `INVESTIGATION_REQUESTS = "investigations/*/requests/"`.
  `site-packages/requests/` and an eval's own `requests/` folder are no longer caught.
- **Grep walk fails closed at the cap.** The agent is told to narrow the search.
- **Hooks fail closed.** Every hook is wrapped (`_fail_closed`): an exception, such as a NUL byte in a
  path, blocks the call instead of passing it.
- Docstrings were corrected, and 13 tests were added for all of the above.

### Tests (B-with-A)
**Main session:**
- `tests/test_runner_isolation.py` (58 tests):
  - A1 fields reach the options, including a real `ClaudeAgentOptions` construction and the pin.
  - Matchers for Read, Grep, Glob, Bash and Write.
  - All six builders pass their list, and the label map is denied end-to-end via Read, Grep and Bash.
  - The Designer and Executor lists, and the Critic's mode-dependent list.
  - Normalisation: `..`, relative paths, symlinks, component boundaries, `requests/` anchoring.
  - Grep/Glob argument checks, the walk, and fail-closed at the cap.
  - Bash deny and allow cases, wildcards, recursion, heredocs and backgrounding forms.
  - Re-ask reports stay readable.
  - Raising a hook fails closed; an error result raises; an SDK error is converted with its sidecar.
  - A clean report about credit is not classified as a cap.
- `tests/test_audit_fixes.py` (26 tests): A3.1, 3–7, 9–11 and A4.

**Test fork** (items 2–14 and every weak-test fix):
- **Conftest:**
  - The autouse spend guard patches `claude_agent_sdk.query` and `subagents.runner.query` and clears the
    three keys. The `allow_sdk` opt-out marker is registered, and a test checks the guard.
  - `make_request` and `substitute_fake_eval` moved to conftest.
  - New `gate_prefix`/`wait_for_file` sentinel gates, and a `transient_then_ok` fake-eval mode.
- **(2)** Blackout walk, foreground and detached, including `worker.log`.
- **(3)** The forensic prompt's headings equal the `check_report` forensic list, in order.
- **(4)** Every documented key, per role (Critic per mode), with a sentinel — 66 cases, derived from
  `subagent_invocation.md`, so `execution_notes` is covered.
- **(5)** The Orchestrator-mode Analyst prompt is exactly base + overlay, with no forensic term.
- **(6)** `check_blinding` blocks the task, dataset and log-filename channels, and a shared real name.
- **(7)** Retry path, plus zero-sample-needs-final-log.
- **(8)** `list --unfinished`: capsys output, the alive branch and superseding order.
- **(9)** `tests/test_install_wiring.py`. It found no gaps.
- **(10)** `dump_transcripts` omits scores (synthetic log).
- **(11)** `tests/test_openrouter_endpoints.py` on a canned catalogue.
- **(12)** Forensic gate boundaries.
- **(13)** The executor `render_report` passes `check_report`.
- **(14)** The small checks, plus four `ledger_to_json` edge cases that kill mutation survivors.
- **Weak tests fixed:**
  - The fence test now passes for the right reason.
  - The real-`inspect` test is replaced by a fake eval with exact-state assertions.
  - Sentinel gates replace the fixed sleeps.
  - `lint_log` uses `tmp_path`.
  - File handles are closed and the unused import is gone.
  - The label-map test is renamed.
- The slowest test went from 17.8 s to 3.75 s.

### B — docs (pure wording; docs fork)
- **B1:** the Critic `diff_review` text now names what is withheld: the candidates, the discrimination
  matrix and the behaviour description. It says this is enforced on files too, and that variant names may
  be visible. The Inferred Purpose rationale is kept. Changed in the contract (~L19, ~L57), the system
  prompt (~L65, ~L69), `diagnostician_responsibilities.md` ~L311 and `subagent_invocation.md` ~L348.
- **B2:** the Mapper `reference_transcript` and Critic `transcript_paths` examples now point at
  transcript dumps, and so does `mapper_interface_contract.md` ~L32.
- **B3:** `orchestrator_analyst_guide.md` ~L11/17 now say scores are read only after the Analyst reports.
- **B4:** `diagnostician_responsibilities.md` ~L352 now says the Diagnostician derives the verdict.
  ~L417/423 are fixed too.
- **B5:** the launcher's per-invocation report path replaces the stale wording in the Explorer,
  Designer and Critic contracts and in the Explorer, Mapper, Critic and Designer prompts.
- **B6:** `experimental-design-patterns.md` uses id-named files.
- **B7:**
  - The "Concurrency preflight test run" line is dropped from the renderer and the contract. It is not a
    `check_report` heading.
  - The runner-sets-concurrency wording is fixed in the contract, `orchestrator_responsibilities.md`
    2e(8–9) and `inspect_reference.md` ~L95/101/104.
  - Gotcha 6 now matches the runner: backoff, `max_connections` never lowered, `max_parallel` halved only
    on contention.
  - The routing row is added to the error-taxonomy table.
- **B8:** the addendum ~L5 is narrowed to the Orchestrator-mode Analyst.
- **B9:** the addendum's scorability labels now use the code's notation.
- **B10:** the resample examples use `epochs: 100`. The executor-contract example `experiment_dir` is
  now absolute.
- **B11:** superseded notes are added to both plan docs (WP2, WP6, and the revision log).
- **B12 (all sub-items):**
  - The transcript-reading licence now cites `diagnose/SKILL.md`.
  - Anti-Pattern #5 is rewritten.
  - "All six fields".
  - Phase 4.
  - The Critic's prediction-mode ledger line is removed.
  - Tags are neutral (`iter_00`/`iter_01`, `00_round0`, `c_` ids), and the detection examples in
    `check_blinding.py` are kept but annotated.
  - The path is `iterations/NN_roundN`.
  - `grader_dir` is stated once, as an absolute path.
  - The log-template Mapper labels are fixed.
  - Stale "once threaded" and "validated nowhere" are fixed, `manipulation_check` exit 2 is called an
    agent error, `subagent_model` wording is fixed, and examples use opus-4-8.
  - The round-cost formula now includes the control.
  - `spend.py` and `openrouter_endpoints.py` name both leads.
  - `check_report.py` preamble wording is fixed.
  - The no-preamble rule is added to the Explorer and base-Analyst contract and prompt.
  - The role tables list `construal_checks`, `artefacts_dir`, `model_routing` and `execution_notes`.
  - Step 2g has `--tag`/`--artefacts-dir`.
  - README: tree (`tests/`, `docs/superpowers/`), "one per sub-agent", and the `/orchestrate`
    description updated for the methodology port.

## Files for commit A (code + tests, incl. lockstep docs) vs commit B (docs)

**Commit A — whole files**
- `subagents/runner.py`
- `subagents/experiment_executor/agent.py`
- `subagents/variant_designer/agent.py`
- `subagents/redteam_critic/agent.py`
- `subagents/transcript_analyst/agent.py`
- `.claude/settings.json`
- `REVIEW-AUDIT.md` (or neither commit)
- `tests/` — every file:
  - new: `test_runner_isolation.py`, `test_audit_fixes.py`, `test_install_wiring.py`,
    `test_openrouter_endpoints.py`
  - edited: `conftest.py`, `test_execute_evals.py`, `test_launch_and_spend.py`,
    `test_routes_and_credentials.py`, `test_check_report.py`, `test_check_blinding.py`,
    `test_forensic_checks.py`, `test_dump_transcripts.py`, `test_lint_log.py`,
    `test_subagent_builders.py`, `test_analyst_prompt_builder.py`, `test_ledger_to_json.py`,
    `test_grader_regression.py`

**Commit B — whole files**
- `scripts/check_blinding.py`, `scripts/check_report.py`, `scripts/dump_transcripts.py`
- `subagents/{environment_explorer,environment_mapper,variant_designer}/system_prompt.py`
- `.claude/docs/contracts/{critic,designer,explorer,mapper}_interface_contract.md`
- `.claude/docs/contracts/analyst_forensic_addendum.md`
- `.claude/docs/investigator/diagnostician_responsibilities.md`
- `.claude/skills/orchestrate/experimental-design-patterns.md`
- `.claude/skills/diagnose/perturbation-design-patterns.md`
- `README.md`
- both `docs/superpowers/plans/2026-09-17-*.md`

**Files split between A and B, by hunk**

| File | Commit A hunks | Commit B hunks |
|---|---|---|
| `scripts/execute_evals.py` | everything else | only the deleted "Concurrency preflight test run" line (~L2156 at base) |
| `scripts/launch_subagent.py` | everything else | the docstring tag examples (~L18–20), the relaunch-argv docstring example, the `--tag` help string |
| `scripts/spend.py` | the `usage_attempts` loop and its docstring bullet | the "both lead agents" docstring sentence |
| `scripts/openrouter_endpoints.py` | the three `from exc` | docstring ~L3 |
| `subagents/transcript_analyst/system_prompt.py` | the *Execution notes* paragraph and `EXECUTION_NOTES_RULE` | the no-preamble rule in Step 7 |
| `subagents/redteam_critic/system_prompt.py` | — (entirely B) | everything |
| `analyst_interface_contract.md` | §Execution notes (optional) | the no-preamble rule |
| `subagent_invocation.md` | the wait exit-code comment (L12) and the Waiting bullet (exit 4, pid start time); the pinned CLI line and the **Isolation** paragraph; the Analyst `execution_notes` bullet and example key; the label-map note (~L210) | everything else |
| `orchestrator_responsibilities.md` | Step 2e item 7 and the three "delegation brief" → `execution_notes` lines (~L260/276/282) | everything else, including the role-table additions |
| `orchestrator_analyst_guide.md` | the §Transcript validity screening sentence | L11/L17 |
| `diagnostician_analyst_guide.md` | the "Forward attrition" bullet | everything else |
| `inspect_reference.md` | gotcha 11, and the L50 `==<pin>` mention | everything else |
| `executor_interface_contract.md` | the `task` bullet (~L30) | everything else |

## Verification output

**Full suite**
```
uv run --no-project --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk==0.2.165 \
  --with python-dotenv --with pytest pytest tests/ -q -p no:cacheprovider --durations=10
...
3.75s call  tests/test_execute_evals.py::test_detached_run_tree_is_score_free_including_worker_log
2.99s call  tests/test_execute_evals.py::test_foreground_run_tree_is_score_free
2.94s call  tests/test_execute_evals.py::test_rendered_report_passes_check_report
2.80s call  tests/test_execute_evals.py::test_foreground_run_with_preflight_is_score_free
2.76s call  tests/test_audit_fixes.py::test_preflight_and_retried_attempts_are_recorded
2.14s call  tests/test_execute_evals.py::test_detached_run_round_trip
1.65s call  tests/test_execute_evals.py::test_wait_returns_3_while_running_then_0
1.43s call  tests/test_execute_evals.py::test_draft_report_has_contract_headings_in_order
1.43s call  tests/test_execute_evals.py::test_transient_failure_is_retried_and_the_failed_attempt_stashed
1.42s call  tests/test_audit_fixes.py::test_each_condition_runs_from_its_own_task_directory
552 passed in 39.62s
```
That is 344 → 552. A confirmation re-run after the last doc edit also gave 552 passed (39.8 s).

**Ruff** (`--select F,B`): 13 remaining, all intentional.
- **9 × B023**, in `execute_evals.build_pairs`. The `render` closure is called immediately inside the
  same loop iteration, so late binding cannot bite. Hoisting it would add nine parameters for no gain.
- **4 × B905**, in `forensic_checks.confusion`. Lengths are checked with an explicit `ValueError` on the
  line above, so `strict=` would be redundant.
- B904 is fixed in the runner and `openrouter_endpoints`. F is clean.

**Mutations**: all 120 reviewer mutations were re-run on snapshot copies; the worktree was never mutated.
- **Skipped (5):** their target lines no longer exist after the fixes — CB-exit0-on-findings,
  L2J-written-despite-defect, RUN-labelmap-pattern-fullpath, runner_glob_as_suffix,
  unfinished-alive-false.
- **Killed (115 of 115 applicable):**
  - 105 died on the first pass.
  - 4 `ledger_to_json` survivors were killed by new fork tests.
  - The last 6 were killed by `test_runner_isolation.py` and `test_audit_fixes.py`:
    verify-analyst-restricted-empty, analyst_no_restricted_files, verify-runner-hooks-dropped,
    runner_drop_hooks, cred-drop-creditbalance-both, and cred-drop-insufficient-both (after adding a
    wording-without-status-code test).
- **Surviving: 0.**
- Harnesses: `/tmp/fork_mut/run_mutations.py` (log `/tmp/fork_mut/out.txt`) and
  `/tmp/iso_check/mut_survivors.py`.

**Isolation check**: `/tmp/iso_check/check.py` runs against a local fake API, with no spend. Before and
after are shown under A1.

## Judgement calls
- **Isolation by env vars as well as `setting_sources`.** Managed `CLAUDE.md` and auto-memory are not
  settings sources; the CLI's own env switches are the documented off-switch.
- **Label map enforced centrally** (`ALWAYS_RESTRICTED`) rather than per builder. The Analyst keeps its
  own entry because for it this is the central denial. The pattern widened from `*.json` to
  `CONDITION_LABEL_MAP*`, which also covers backups.
- **Directory patterns now match at component boundaries.** This is a small tightening of the old
  substring semantics, made so that `requests/` could be used safely. Wildcards inside directory patterns
  are new and allow `runs/*/private/`.
- **The Grep walk is an addition beyond the brief's "path/glob/pattern"**: a content search over the
  investigation directory would otherwise quote the label map. Hidden directories are skipped, as
  ripgrep does. The walk fails closed at 20 000 entries, so a repo-root Grep in a big repo must be
  narrowed, at the cost of one turn.
- **`diff_review` Critic also loses `requests/` and `INVESTIGATION-LOG.md`.** Both carry the candidates.
  `artefacts/critic/` stays readable, because a re-ask must read its own previous report. Earlier
  candidate_critique reports there are a residual channel.
- **The Analyst is not denied `artefacts/mapper/`** (forensic mode). It was not in the brief; the owner
  should confirm.
- **Per-condition cwd for a directory task** runs as `.` from that directory. A task given with `@name`
  was already rejected by validation.
- **`execute_evals` dead-worker `wait`** already returned a terminal `failed` state. Only the pid-reuse
  guard was added there; exit 4 is launcher-only.
- **`settings.json`:**
  - `inspect eval|eval-set|eval-retry|log|view` moved to **deny** rather than ask, because the firewall
    is categorical for the lead.
  - The harmless forms `--version`, `--help` and `list *` are allowed.
- **SDK pin.** The test recipe in `conftest.py` is now pinned as well, and a test asserts that the pin
  equals the installed version. The brief's own test command is unpinned, so it still resolves to 0.2.165
  today.
- **`execution_notes`** is validated strictly: a wrong type is an agent error, blank is skipped. It is
  rendered in both modes.

## Not done, and why
- **Lead deny on `Read(**/artefacts/executor/runs/*/private/**)`** (reviewer Minor 7). It was suggested by
  the reviewer sub-agent, not in the brief. I did not edit permission settings on a sub-agent's say-so.
  It is worth adding.
- **Executor deny on `*.eval`** (reviewer Minor 6). As a basename pattern it would make every Grep over
  the experiment directory fail, through the walk. This is defence in depth on top of a predating rule,
  so it is the owner's trade-off.
- **OS-level sandbox for sub-agents.** Future work, noted in `runner.py` and `subagent_invocation.md`.

## Owner decisions applied after this review (2026-10-12)

Landed as **one commit**, not the A/B split described above.

- **Lead read-deny on the runner's `private/`**: added `Read(**/artefacts/executor/runs/*/private/**)` to the shipped `.claude/settings.json` deny list; `orchestrator_responsibilities.md §2e` now says the rule is enforced.
- **Executor `*.eval` deny**: not added (owner agreed it would break every Grep over the experiment directory for little gain).
- **Analyst and `artefacts/mapper/`**: now denied (the surface inventory could prime it; it never needs it). Test updated.
- **Lead `inspect eval`/`log`/`view` on deny, not ask**: kept.
- **Grep walk cap**: kept fail-closed, but the walk now skips a fixed set of dependency/build directories (`GREP_WALK_SKIP_DIRS`: `node_modules`, `__pycache__`, `venv`, `site-packages`, `bower_components`), which removes the main source of false refusals. A `.gitignore`-based skip was considered and rejected: it is only safe if the search tool skips every gitignored path too, and a user-gitignored investigation directory would then go unchecked. Two tests added.
- Suite after these changes: **554 passed**.
