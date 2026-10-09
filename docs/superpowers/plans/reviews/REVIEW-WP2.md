# REVIEW — WP2 (revised 2026-10-08): routes, report conformance, credential suspension

Branch `wp2`, based on main `44316d8`. Everything is left uncommitted. I spent no API money: every test is offline, and the network is mocked.

## Status by deficiency

- **D27: closed.** The context-window environment variables are now set automatically, and the warning is documented.
- **D05: closed as redesigned by the owner.** No third route was added. The fix is detection, then degrading, then a resumable suspension, plus accurate wording about the two routes.
  - Provider caps still cannot be observed in advance, and the docs say so.
  - The underlying single point of failure (a correlated cap across both routes) remains by design. What changes is the answer to it: resume rather than terminate.

## What was done, by part

### Part 1: non-Claude route ergonomics (D27)

**`subagents/runner.py`**

For a non-Claude model on the `openrouter` route, the route env gains two variables:
- `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1`
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, set to the model's context window

How the window is found:
- It is the largest `context_length` that **at least half** of the model's endpoints serve (an upper median), read from OpenRouter's public `/models/<id>/endpoints` catalogue. *(Was the maximum; changed in the owner follow-ups below.)*
- The fetch uses a new stdlib function, `model_context_length` in `scripts/openrouter_endpoints.py`. That file also gained a `resolve_model_id` helper, which `select()` now shares.
- The module is loaded by path from the scaffold checkout, and the result is cached once per process (`lru_cache`).

How failures behave:
- Offline, HTTP error or an unlisted model: neither variable is set, so the CLI keeps its default enforcement, and one `[route] …` line goes to stderr. The invocation is never failed over this.
- A `CLAUDE_CODE_MAX_CONTEXT_TOKENS` the user exported takes precedence over the catalogue value.

**Docs:** `subagent_invocation.md §subagent_route` documents the benign `[claude-code:unrecognized_model]` warning and the automatic window setting.

### Part 2: report conformance for all six roles

**`scripts/check_report.py`**
- Adds `executor` and `analyst`. The base Analyst contract is the default, and `--forensic` (or `--mode forensic`) inserts the addendum's two sections directly after `### Quantified results`.
- For the Executor, it also checks the four `#### … (N condition-model pair(s))` sub-headings under Error Summary, matched by prefix.
- A base-mode check that finds forensic headings warns "re-run with --forensic".
- Explorer was already covered.

**Drift tests:**
- Each role's tuple must **equal**, in order, the `###` headings in its contract's `## … Report Format` section. The `*(only if applicable)*` markers must equal `OPTIONAL_HEADINGS`.
- The forensic tuple is checked against the addendum's "placed immediately after … and before …" sentences.
- Every role must be covered.

**Corpus:**
- Conformant Executor reports pass, from R01, R02, R04, R05 and R06.
- Forensic Analyst reports pass, from R01 to R06.
- Two reports fail with the right defect:
  - R06 `iter2_widen_actions_report.md`: the Executor asked for permission instead of reporting.
  - R01 `resample.report.md`: an ad-hoc runner's report.
- R06's Grok Analyst `…FAILED_grok_asked_permission.md` fails.
- My full-corpus sweep agreed: every sub-agent report passed, and every improvised or failed one failed.

**Rule** in `subagent_invocation.md §Report conformance` (new) and both lead docs' failure-handling sections: whenever a sub-agent ran on a non-Claude model, run `check_report.py` and re-ask once. A second failure is read structurally and recorded. *(Superseded by the owner follow-ups below: the re-ask now uses the dedicated `conformance_defects` + `previous_report` fields, and `constraints` carries no reformatting request. The original table is kept for the record.)*

| Role | Where the defect lines went (original WP2, superseded) |
|---|---|
| Critic | `conformance_defects` |
| Explorer, Mapper, Designer | appended to the `constraints` string |
| Analyst | a `report_conformance` key inside the `constraints` object. All prompt builders read `constraints`; the reviewer verified this. |
| Executor | no re-invocation, because that would re-run the evals. Regenerate the report with `execute_evals.py report <run_dir>`, which is score-free. |

### Part 3: credential exhaustion, suspension and resumption

**Mined strings.** All of these are verbatim from the archive:
- Anthropic: `API Error: 400 You have reached your specified workspace API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.`
  - Saved stderr: R05 `artefacts/executor/iter2_stderr.log` and R04 `iterations/01_round1/executor_stderr.txt`.
  - Logs: R06, R03, R01.
- OpenRouter: `Failed to authenticate. API Error: 403 Workspace lifetime budget of $150.00 exceeded.` (R02 log) and `HTTP 403 — "Workspace lifetime budget of $150.00 exceeded"` (R01 log).
- I added three strings from the providers' documentation, marked as such in comments: `credit balance is too low`, `requires more credits` and `insufficient credits`.
- A status regex catches `error code / API Error / HTTP / status(_code) [:=] 402`. A bare "402" is not matched, because it appears in sample ids and hashes.
- Inspect-side strings: none exist in the archive. R05's capped arms never ran, and its superseded logs are `success`. Inspect wraps the provider's own message, so the same substrings match; the tests use realistic `anthropic.BadRequestError` and `openai.PermissionDeniedError` wrappers.

**`runner.py`**
- `classify_failure()` is driven by `CREDENTIAL_PATTERNS`.
- On a ProcessError or connection error, or an `is_error` result whose text matches, it raises `RuntimeError("<agent>: credentials_exhausted (route=<r>, model=<m>): …")`.
  - It classifies both the exception text and the error result's text, because older SDKs raise a generic "exit code 1".
  - It writes the cost sidecar with `failure_class` and `route`. The sidecar is written even with no `ResultMessage`; usage fields are then null.
- **Behaviour change:**
  - An `is_error` result carrying credential text used to be returned *as the report*. It now raises.
  - A failed run's cost sidecar is now written whenever a result message arrived. Before, a ProcessError skipped it.

**`launch_subagent.py`**
- Receipts gain `failure_class` and `failure_route`. These come from the sidecar, falling back to the stderr marker.
- `describe` shows both.
- New **`list --unfinished`** prints, for each invocation not cleanly finished, a ready-to-run `launch` command for its persisted request, or "still running" for a live one.
- A failed invocation is superseded, and left out of the list, when a later run of the same role and tag with the same request (ignoring `subagent_*` and `conformance_defects`) exited 0. An unreadable request never supersedes.

**`execute_evals.py`**
- The credential check runs **before** every other class in `classify_error` and returns `structural`. There is no new taxonomy category.
- The evidence line becomes `[structural] [credentials] <scrubbed tail>`, and the draft report adds a `[credentials] Credentials exhausted` line to that pair.
- The tag is attached to the already-scrubbed `combine_output` text, so the blackout holds. The end-to-end test asserts that no `LEAK_TOKENS` appear.
- **Behaviour change:** OpenRouter `402` and credits failures were previously classed `routing` ("Pinned provider unavailable"). They are now structural plus `[credentials]`: an account that cannot pay is not a provider that is down. The Executor contract §Error Summary and the Executor system prompt are updated to match.

**Docs**
- Both lead docs, §Sub-Agent Failure Handling, cover: detection; degrade first (cross-referencing the degraded-mode policy, e.g. Claude via OpenRouter with a continuity check, which R02 did); and a five-step **Suspension** procedure:
  1. Let live work finish, and launch nothing new.
  2. Do not improvise around the gap, and leave every table untouched.
  3. Write a `### Suspension` block in the log.
  4. Write `RESUME.md`.
  5. Give the user a short status, not a report.
- A `RESUME.md` template is in each doc's §State Management. It covers: the step to resume at; credentials needed; relaunch commands pasted from `list --unfinished`; executed-but-ungraded rounds, with each table's log line range and `sed -n | sha256sum`; and a frozen-artefact checklist.
  - In the Diagnostician template, grader files are hashed with `find … -not -path '*/__pycache__/*' -exec sha256sum {} +`. Bytecode caches change on every load, which ruled out a simple manifest.
- "Suspended (resumable)" is distinct from Constrained Termination. It appears in the Diagnostician's Phase 5 primary-finding forms and stopping rule, and in the Orchestrator's Step 2j and Phase 3.
- Degraded-mode "Pause-forcing": a frozen route that is merely out of credit **suspends**; a *changed* model, route or environment still ends the investigation.
- Orchestrator Step 2e gains a `[credentials]` signature.

**Skills:** "How to Begin" in both SKILL.md files. If the argument is a directory containing `RESUME.md`:
1. Verify the frozen artefacts.
2. Confirm credentials with one minimal call.
3. Write a `### Resumed` block.
4. Relaunch.
5. Rename `RESUME.md` to `RESUMED-<n>.md`, so the directory is not resumed twice.

The precedents cited are R01 and R02 deferred scoring, plus the `RESUME.md` files that R01 *and* R06 both improvised.

### Part 4: route wording

Each two-route statement now says it is deliberate and points at suspension: `README.md §Prerequisites`, diagnostician `§Cross-family Critic gate`, `critic_interface_contract.md §Cross-family routing`, diagnose `SKILL.md` and `subagent_invocation.md` (the Critic paragraph, plus the `subagent_route` intro).

## Files changed

- **Code:** `subagents/runner.py`, `scripts/launch_subagent.py`, `scripts/execute_evals.py`, `scripts/check_report.py`, `scripts/openrouter_endpoints.py`, `subagents/experiment_executor/system_prompt.py`
- **Docs:** `.claude/docs/investigator/{diagnostician_responsibilities,orchestrator_responsibilities,subagent_invocation}.md`, `.claude/docs/contracts/{critic,executor}_interface_contract.md`, `.claude/skills/{diagnose,orchestrate}/SKILL.md`, `README.md`
- **Tests:** `tests/test_check_report.py` (extended) and `tests/test_routes_and_credentials.py` (new)

## Tests

`uv run --no-project --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/ -q -p no:cacheprovider`

```
........................................................................ [ 29%]
........................................................................ [ 58%]
........................................................................ [ 87%]
................................                                         [100%]
248 passed in 44.79s
```

That is 190 tests before, 248 now, with nothing skipped (the corpus is present).

## Propagation checklist

- [x] **No new request field** in the original WP2. *(Superseded: the follow-ups add `conformance_defects`/`previous_report`; see that section's propagation notes.)*
- [x] **New interface outputs** (the receipt fields `failure_class`/`failure_route`, the sidecar's `failure_class`, the `[credentials]` tag):
  - documented in `subagent_invocation.md §General Pattern`, the Executor contract and system prompt, and both lead docs (detection, Step 2e);
  - consumed by the Suspension procedure and `RESUME.md`.
- [x] **Spine walk:**
  - *Phase 0/1 scoping*: unchanged. The Critic gate wording is updated.
  - *Executor*: it relays the tag and never retries it.
  - *Analyst*: it receives only heading-defect lines, never scores or the mapping, so the firewall is intact.
  - *Log*: `### Suspension` and `### Resumed` blocks, with `(2)` for repeats so `lint_log` passes.
  - *Decision and termination*: "Suspended (resumable)" is not an exit.
  - *Anti-patterns*: suspension cites #23 (no improvising).
  - *Report*: no `FINDINGS-REPORT.md` while suspended.
- [x] **Five install surfaces:** no new scaffold file, so nothing changes. `install.sh`, `uninstall.sh`, the README tree and `settings.json` are untouched. Existing allow entries already cover `launch_subagent.py *`, `check_report.py *`, `find *`, `sed -n *` and `sha256sum *`. There is no overlap with WP6's `check_blinding.py` lines.
- [x] **WP6 no-touch list:** no edits there (the reviewer verified this). The Executor contract edits are in §Error Summary only.
- [x] **Invariants:**
  - *Blackout*: the tag rides on scrubbed text, and `list --unfinished` prints only paths and states.
  - *Tables*: the procedure forbids edits and RESUME hashes them.
  - *Orchestrator-mode Analyst*: it never sees forensic text, because `check_report` runs in the lead agent.

## Reviewer (Sonnet) findings and dispositions

There were no Critical findings.

**Important, all fixed:**
1. The credential check now also covers the error-result text, for older SDKs. A realistic test was added.
2. I dropped the claim that `execute_evals` tags the *scanning* model. The docs now say where a scanner cap surfaces.
3. Sample-level-only credential attrition is not tagged. **I documented this rather than fixing it** (see the judgement calls below).

**Minor:**
- Fixed:
  - Removed the generic "payment required" pattern, which risked overmatching.
  - The regex now accepts `status_code=402`.
  - Unreadable requests never supersede.
  - Added a prune note for `--unfinished`.
  - `### Resumed (2)` is now specified.
- Accepted as is:
  - A slug containing "claude" skips the window variables. The CLI knows those windows.
  - The 10-second synchronous catalogue fetch.

## Open judgement calls for the owner

1. **OpenRouter credit/402 moved from `routing` to `structural` + `[credentials]`.** I did this to follow the brief's "keep it structural". It changes the existing contract's "Pinned provider unavailable" criterion. Revert if you want "routing" kept for OpenRouter credit failures.
2. ~~**The context window is the maximum across endpoints, not the minimum.**~~ *Resolved by the owner: the upper median (follow-up 1).*
3. ~~**Sample-level credential failures go untagged.**~~ *Resolved by the owner: tagged (follow-up 2). The original note's premise was also wrong — the runner passes `--no-fail-on-error`, so a mid-eval cap was the usual case, not the exception.*
4. **No `resume_plan.py`.** `list --unfinished`, plus `sed | sha256sum` and `find -exec sha256sum` with existing allowlisted tools, made `RESUME.md` mechanical without adding a new file and its five install surfaces. A small `freeze/verify` helper would make the frozen-artefact check one command, if you want it.
5. ~~**The Analyst re-ask uses a `report_conformance` key inside `constraints`.**~~ *Resolved by the owner: dedicated `conformance_defects` + `previous_report` fields for every re-asked role, and no re-analysis (follow-up 3).*
6. **The Executor gets no re-ask.** Its report is instead regenerated deterministically by `execute_evals.py report`.

## Not done

- **There was no live check** that the CLI honours `CLAUDE_CODE_MAX_CONTEXT_TOKENS` for an OpenRouter model, because of the no-spend rule. The mechanism is taken from R01, which applied it by hand successfully.
- I could not find an Inspect-side credential error string in the archive. That classifier rests on the provider's message substring, which is shared with the runner path, which *is* archive-verified.

## Follow-ups (owner review, 2026-10-09)

Three changes the owner asked for after approving WP2. Everything is still uncommitted, and no API money was spent.

### 1. Context window is the upper median, not the maximum

- **What changed.** `scripts/openrouter_endpoints.py`:
  - `model_context_length` now delegates to a new `majority_context_length`. That function sorts the windows in descending order and takes index `ceil(n/2) - 1`, so the result is the largest window that at least half of the endpoints serve.
  - A rare small-window host is ignored. Note that a rare *large* one is ignored too.
- **Docs updated.** The docstrings, `subagent_invocation.md §subagent_route` and this file.
- **Tests.**
  - Llama 3.3 70B (11 endpoints, 12k–131k) gives 131072.
  - DeepSeek V3.2 (13 endpoints, 32k–163k) gives 163840.
  - n=1, n=2, a frontier model with identical windows, a majority-small case and an empty list are all covered, in both input orders.
  - An end-to-end runner test checks the Llama shape.

### 2. Mid-run credential exhaustion is tagged at sample level

**`scripts/execute_evals.py`**
- `sample_shape` runs `is_credential_exhaustion` on each summary's `error` message.
- Only the count is kept, as `credential_sample_errors` on each pair in `status.json`. The message itself is never stored, so neither scorer text nor raw error text can leak.
- In the draft report:
  - the Sample-level entry gains a `[credentials] Credentials exhausted (K of the X errored samples)` line;
  - the Summary's attrition note gets `(K [credentials])`.
- `status` prints `[credentials] K sample error(s)`.
- The preflight retest records the count, and an all-samples-deterministic exclusion caused by a cap is tagged too.

**Propagation**
- Executor contract, under §Error Summary → Sample-level.
- Executor system prompt (step 7): relay the line, never re-run.
- Orchestrator Step 2e Sample-level bullet: "Except…".
- Both lead docs' *Credential exhaustion and suspension*: it is a suspension trigger, the samples are not data, and the pair is not sent to the Analyst and is re-run after resuming.
- Both `RESUME.md` templates' relaunch notes: such a pair counts as not completed.
- `orchestrate/experimental-design-patterns.md §Individual Sample Errors`: a cap clusters in late-running conditions and is not a manipulation effect. The reviewer found this one.

**Tests**
- `test_mid_run_cap_is_tagged_at_sample_level_and_score_free`. It uses a fixture log whose samples carry the archive's Anthropic workspace-cap message in Inspect's wrapper, through a new `write_fixture_log(error_message=, errored=)`.
- The test asserts the per-pair counts (2/2 against 1/0 for "boom"), the report and status lines, no `LEAK_TOKENS`, and no raw message text in the report, `status.json` or status output.

### 3. Dedicated re-ask fields replace `constraints.report_conformance`

**New fields**
- `conformance_defects` (list of strings) and `previous_report` (absolute path).
- Roles: Explorer, Mapper, Designer and Analyst; the Critic gains `previous_report`.
- `constraints` no longer carries reformatting requests anywhere, and `report_conformance` is gone.

**`subagents/runner.py::reask_section`**
- This is one shared validator and renderer that each builder calls with `data.get("conformance_defects")` and `data.get("previous_report")`, so the keys stay visible to the invariant test.
- It requires both fields together, a non-empty list of non-empty strings, and an absolute path to an existing file. Otherwise it raises `RuntimeError`, which exits 2.
- The rendered section tells the agent to read the previous report and restructure it into the exact headings, re-deriving only sections that were genuinely missing. Each role adds its own rule:
  - **Explorer and Mapper:** re-read the environment only for a missing section.
  - **Designer:** keep variants and diffs unchanged.
  - **Analyst:** rebuild only from saved artefacts, and never re-run a scan, re-validate or rebuild the grader.
  - **Critic:** supply any missing discriminating signature.

**Executor**
- The Executor builder now **rejects** either field, with exit 2. This was reviewer Minor 3: the docs said "no field", but a mistaken send would have been dropped silently.

**Five places, per role**
1. Contract: a new `### Re-ask fields` request subsection (the Critic's §Optional is extended).
2. `subagent_invocation.md`: §Report conformance is rewritten, and each role section gains a bullet.
3. System prompt: an "If your request carries a `## Re-ask` section" paragraph (for the Critic, its existing heading paragraph).
4. Builder.
5. Lead docs: both "re-ask once" paragraphs. The Orchestrator doc names the Explorer and Analyst; the Diagnostician doc names the Mapper, Designer, Analyst and Critic.

`launch_subagent.py::INFRA_REQUEST_KEYS` also ignores `previous_report`, so a re-ask still supersedes the earlier invocation in `list --unfinished`.

**Tests**
- `tests/test_subagent_builders.py`:
  - the re-ask section is captured from the built prompt for each of the 5 roles: defects verbatim, path and role rule;
  - first attempts have no section;
  - 7 malformed shapes × 5 roles raise;
  - the Executor rejects both fields;
  - a **per-builder data-key ↔ invocation-doc invariant for all six builders**, stricter than the Analyst's original: a key must be documented in its own role section, and only `subagent_*` may rely on the General Pattern. It caught the missing per-role doc bullets, and later the Executor's rejection bullet.
- `tests/test_critic_prompt.py` is updated to the paired form, plus a half-re-ask error test.

### Files touched by the follow-ups

- **Code:**
  - `scripts/{openrouter_endpoints,execute_evals,launch_subagent}.py`
  - `subagents/runner.py`
  - `subagents/{environment_explorer,environment_mapper,variant_designer,transcript_analyst,redteam_critic,experiment_executor}/agent.py`
  - the system prompts of all six roles
- **Docs:**
  - the contracts for explorer, mapper, designer, analyst, critic and executor
  - `subagent_invocation.md`
  - both lead docs
  - `.claude/skills/orchestrate/experimental-design-patterns.md`
- **Tests:**
  - `conftest.py`
  - `test_routes_and_credentials.py`
  - `test_execute_evals.py`
  - `test_subagent_builders.py`
  - `test_critic_prompt.py`

WP6's no-touch list was verified untouched by the reviewer.

### Tests

```
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 69%]
........................................................................ [ 92%]
.......................                                                  [100%]
311 passed in 47.15s
```

That is 248 before the follow-ups and 311 now, with nothing skipped.

### Reviewer (Sonnet) and dispositions

There were no Critical or Important findings.

**Minor:**
1. The skill file did not mention the sample-level tag. **Fixed.**
2. The reviewer said the process-level tag tests the truncated tail rather than the full output. **Not a bug.** `classify_error` and the tag both test the same `diagnostic` string; only the *stored* text is cut to 2,000 characters.
3. The Executor silently dropped re-ask fields. **Fixed:** it now rejects them.
4. `previous_report` is not checked for being non-empty or `.md`. **Accepted as is.**

### Judgement calls

- **Both re-ask fields are required together, for the Critic too.** Before this change, `conformance_defects` alone was a valid Critic re-ask; now it is an agent error. This keeps one rule across five roles.
- **The upper median ignores rare large-window hosts as well as small ones.** That follows from "at least half", and avoids sizing the window to an endpoint OpenRouter may rarely route to.
- **`status.json` carries a count field (`credential_sample_errors`) rather than the literal `[credentials]` string.** The literal tag appears in the report and in `status` output.
- **A sample-level cap still leaves the pair's state as `sample-level-errors`.** There is no new matrix status; the tag is what distinguishes it.
