# Experiment Executor: Interface Contract

This document defines the interface between the orchestrator and the Experiment Executor. Both agents read this document. It specifies what the orchestrator provides, what the Executor returns, and the format of each. This is the single source of truth for the contract; neither agent should redefine these structures independently.

---

## Orchestrator → Executor: Request Format

Each request from the orchestrator to the Experiment Executor contains the following fields.

### Experiment name (required)

A short `snake_case` identifier for the experiment (e.g., `explicit_goal_framing`). Used in log tags and directory naming; must be filesystem-safe.

### Experiment directory (required)

An absolute filesystem path to the experiment directory. Must exist, be readable, and contain the evaluation environment with all modifications already applied by the orchestrator (per Step 2c of `orchestrator_responsibilities.md`). The Executor creates a `logs/` subtree within this directory.

### Conditions (required)

A mapping from condition names (`snake_case`) to per-condition invocation parameters:

```json
{
    "control":   {"task": "task.py", "args": {}},
    "treatment": {"task": "task.py", "args": {"system": "system_prompt_treatment.txt"}}
}
```

- `task`: path to the task file relative to `experiment_dir`.
- `args`: key-value pairs passed as `-T key=value` to the `inspect eval` invocation for that condition.

Patterns A/B/C (parameter-based, separate task files, model variation only) are determined by how the orchestrator populates this mapping. The Executor does not need to classify the pattern.

### Models (required)

A list of `provider/model-name` strings (e.g., `["anthropic/claude-sonnet-4-5-20250929"]`). The Executor runs every condition against every model; `len(conditions) × len(models)` is the number of condition-model pairs.

### Model routing (optional)

A mapping from model string to the routing directives for that model. The orchestrator resolves routing during Phase 1 and passes it fully specified; **the Executor renders it and reports it, but never chooses or changes it.** Route selection affects internal validity, which makes it a scientific judgement and therefore not the Executor's to make.

```json
"model_routing": {
    "openrouter/meta-llama/llama-3.3-70b-instruct": {
        "route": "openrouter",
        "provider_slug": "crusoe",
        "model_args": {
            "provider": {
                "order": ["crusoe"],
                "allow_fallbacks": false,
                "require_parameters": true
            },
            "default_headers": {"X-OpenRouter-Metadata": "enabled"}
        }
    }
}
```

- `route`: `direct` | `openrouter`.
- `provider_slug`: the pinned upstream provider, or `null` for an unpinned OpenRouter route.
- `model_args`: rendered one-to-one as `-M key=value` arguments on that model's `inspect eval` invocations.

**Rendering `model_args` correctly.** Object values must be **JSON** (double-quoted keys, lowercase `true`/`false`), wrapped in single quotes for the shell:

```
-M provider='{"order": ["crusoe"], "allow_fallbacks": false, "require_parameters": true}' -M default_headers='{"X-OpenRouter-Metadata": "enabled"}'
```

Do not emit Python-style dicts (`{'order': ...}`, `False`) — the single quotes collide with the shell quoting and `False` is not a valid boolean to the parser. A malformed `-M` value either errors out or, worse, parses to a string, which silently disables the pin.

Models absent from the mapping are invoked with no `-M` arguments. When `model_routing` is omitted entirely, every model is invoked directly — the pre-existing behaviour.

**Pinning is not advisory.** `allow_fallbacks: false` means the invocation fails rather than silently rerouting to a different upstream provider. That is the intended behaviour: a hard failure is recoverable, whereas a silent reroute corrupts the comparison between conditions without leaving a trace. The Executor must never relax a pin, drop `-M` arguments to make a failing command succeed, or substitute a provider.

### Overrides (optional)

A mapping of execution-parameter overrides. Supported keys:

| Key | Type | Meaning |
|---|---|---|
| `sample_limit` | int | Maps to `--limit` |
| `epochs` | int | Maps to `--epochs` |
| `skip_preflight` | bool | If `true`, the Executor does not run preflight and goes straight to full execution |
| `max_parallel` | int | Upper bound on concurrent eval subprocesses |
| `max_connections` | int | Maps to `--max-connections` (concurrent API calls within an eval) |
| `runs_per_condition` | int | Number of full-run repetitions per condition-model pair |

Unrecognised keys are ignored with a note in **Additional Notes**. The Executor may apply other defaults (e.g., `--retry-on-error 3`, `--no-fail-on-error`, `--display plain`, and `--with "openai>=…"` on `openrouter/…` models) without them being in overrides — these are policy, not configuration.

---

## Executor → Orchestrator: Report Format

Each report from the Executor to the orchestrator contains the following sections, in order. The summary comes first; the remaining sections provide supporting detail for drill-down. Empty sections display `None.` or `No failures.` — never omitted — so the top-level shape is fixed and scannable.

### Summary

A 3–5 sentence overview of the execution. State: how many condition-model pairs were attempted and their final disposition (completed / completed-with-sample-errors / excluded at preflight / failed during full execution); the chosen `max_parallel` level; and the most decision-critical observation for the orchestrator (e.g., asymmetric exclusions between conditions, unusually high sample-level attrition, notable retry activity). No recommendation — this section conveys information, not next-step advice.

If the orchestrator stops reading here, it should know whether the execution succeeded cleanly and whether any pair needs further investigation.

### Parent Log Directory

The absolute path to `<experiment_dir>/logs/`, the root under which per-condition log subdirectories are organised. This is the path the orchestrator will later partition by condition when constructing the Analyst's `transcript_source` mapping.

### Preflight Exclusions

Condition-model pairs that did not proceed to full execution because preflight found an excluding failure. Displays `None.` if all pairs passed preflight, or `Preflight skipped (skip_preflight=true).` if preflight was skipped.

For each excluded pair:

- **`<condition> / <model>`**
  - **Reason**: `structural` | `all-samples-deterministic-sample-error`
  - **Final exit code** (for structural): integer
  - **Command**: the exact `uv run inspect eval ...` invocation attempted
  - **Evidence**:
    - *structural*: output tail (up to ~50 lines) from the final attempt, combining stderr **and stdout** — Inspect reports prerequisite failures (missing API key, unresolvable model) on stdout
    - *all-samples-deterministic-sample-error*: all 3 sample error traces from the `--limit 3` retest

This section is a gate on the Execution Matrix: pairs appearing here are absent from the matrix below.

### Condition-Model Execution Matrix

A table of every condition-model pair that entered full execution (or `skipped` if overridden). Columns:

| Column | Meaning |
|---|---|
| Condition | `snake_case` identifier |
| Model | `provider/model-name` |
| Route | `direct`, `openrouter:<slug>` for a pinned route, or `openrouter:unpinned`. Records how the request actually reached the model, which `--metadata model=` alone cannot capture |
| Log Path | Absolute path to the `.eval` log file produced by this pair |
| Status | `success` \| `sample-level-errors` \| `failed` \| `excluded-at-preflight`* \| `skipped` |
| Samples (done/total) | Completed / intended sample count |
| Process Retries | How many subprocess-level retries were needed in full execution |
| Sample-Level Retries | How many samples inside the completed eval were retried by `--retry-on-error` |
| Preflight Retries | `--limit 3` retest fires count: `0` if preflight passed cleanly, `1` if the retest was triggered |
| Duration | Wall-clock seconds for the final attempt |

*`excluded-at-preflight` rows reference the Preflight Exclusions section; they may be omitted from the matrix entirely if that section is canonical (implementation choice, but the matrix must not silently drop them without a pointer).

**This column set is exhaustive.** The Execution Matrix reports execution mechanics only. The Executor must not add columns — and in particular must never add a scorer-derived column (`Accuracy`, `Mean`, `Score`, `Metrics`, or any value computed by the eval's scorer). Scorer outputs are downstream of execution and belong to the Analyst's stage; surfacing them here forces the orchestrator to read results before the Analyst reports, in violation of `orchestrator_responsibilities.md §2e`. See the non-negotiable requirement below.

### Error Summary

Failures observed during **full execution**. Organised into four categories with observable criteria:

#### Structural (N condition-model pair(s))

Criterion: all process-level retries exhausted; no usable log produced. Displays `None.` if no structural failures.

For each:
- **`<condition> / <model>`**
  - **Final exit code**: integer
  - **Command**: the exact `uv run inspect eval ...` invocation attempted
  - **Stderr (last ~50 lines)**: the relevant error message

The Executor does not diagnose root cause (experiment-setup bug vs. infrastructure issue); the orchestrator does this from the evidence.

#### Sample-level (N condition-model pair(s))

Criterion: eval process exited successfully; one or more samples errored in the log's per-sample entries. Displays `None.` if no sample-level failures.

For each:
- **`<condition> / <model>`**: `X` of `Y` samples errored.

#### Transient, recovered (N condition-model pair(s))

Criterion: error occurred during execution, but a subsequent retry succeeded; net outcome was success. Displays `None.` if no transient failures were absorbed.

For each:
- **`<condition> / <model>`**: N process retries required; final attempt succeeded.

#### Pinned provider unavailable (N condition-model pair(s))

Criterion: `execute_evals.py` classified the failure as **`routing`** — its error entries are tagged with the class, so an entry beginning `[routing]` maps to this section. The script applies that class to OpenRouter failures that no retry can fix: no available providers for the selected model, or an account-credit failure. Displays `None.` if no pinned routes failed this way.

For each:
- **`<condition> / <model>`**: the pinned `provider_slug`, the OpenRouter error message, and the number of process retries attempted.

This is reported separately from *structural* because the remedy is different and the choice is the orchestrator's, not the Executor's. A structural failure means the experiment is misconfigured; a pinned-provider failure means the experiment is correct but the chosen upstream is down. The orchestrator can then wait, re-pin and restart that model's conditions, or drop the model.

**The Executor never re-pins.** Anything already run against that model was measured on a different upstream provider than a re-pinned run would use, so silently switching would split a condition across two instruments and destroy the comparison without leaving a trace in the report.

### Transcript Termination Metadata

Per condition-model pair that reached full execution, a breakdown of transcript endpoints. This section enables the orchestrator to sanity-check transcript quality before delegating to the Analyst without reading log contents.

| Condition | Model | No-Assistant-Msg | Limit-Hit | Error-Terminated |
|---|---|---|---|---|

- **No-Assistant-Msg**: transcripts in which the model produced no assistant turns.
- **Limit-Hit**: transcripts that terminated because a token / message / time limit was reached.
- **Error-Terminated**: transcripts that terminated with a process-level error flag in the log.

These are counts of transcript *shape*, not *quality*. A transcript with assistant messages present, no limit hit, and no termination error is reported as unremarkable here regardless of content — that judgement belongs to the Analyst.

### Concurrency Decision

Structured fields documenting how the Executor chose the full-execution concurrency level:

- **Chosen `max_parallel`**: integer
- **Orchestrator override provided**: `yes (<value>)` | `no`
- **Resources observed**: e.g., `nproc=8, free_mem_mb=14336`
- **Sandbox detected**: `yes` | `no` (evidence: preflight timing, e.g., `~18s per command suggests sandbox setup`)
- **Concurrency preflight test run**: `yes (<result>)` | `no` | `not applicable (only 1 pair)`
- **Reductions applied during execution**: list with reasons, or `none`
- **Rationale**: one to two sentences tying the fields above to the chosen level

### Execution Summary

Counts recap:

- **Total attempted**: condition-model pairs submitted by the orchestrator
- **Completed successfully**: reached `status=success`
- **Completed with sample errors**: reached `status=sample-level-errors`
- **Excluded at preflight**: see Preflight Exclusions
- **Failed during full execution**: reached `status=failed`

### Additional Notes

A catch-all for anything observed during execution that does not fit elsewhere but the orchestrator should know. Examples: unrecognised keys in `overrides`, unusual stderr patterns that did not cause failures, `--help` lookups that revealed flag behaviour differing from `inspect_reference.md`. Displays `None.` if empty.

---

## Error Taxonomy

Failures are classified by **observable criteria**, not by judgement about cause. The Executor applies these criteria uniformly; the orchestrator interprets from the evidence.

| Category | Criterion | Orchestrator action gated |
|---|---|---|
| **Transient** | Error occurred but a subsequent retry succeeded | None — noted for completeness |
| **Sample-level** | Eval process exited successfully; ≥1 sample errored in the log | Proceed to Analyst; retry-distribution-shift caveat applies |
| **Structural** | All process-level retries exhausted; no usable log produced | Must investigate before proceeding; re-running will not help |

Every **structural** failure record includes: final exit code, exact command, and output tail (~50 lines, stderr and stdout combined). These three pieces are sufficient for the orchestrator to distinguish experiment-setup bugs (ImportError, missing task file) from infrastructure issues (missing API key, Docker unavailable). Both streams are required: Inspect writes provider-initialisation and missing-key errors to stdout, so a stderr-only tail would report the most common structural failures with no evidence at all.

Edge-case handling:
- **Subprocess exited 0 but no usable log produced** → Structural.
- **First retry succeeded after earlier failures** → Transient (retries recovered in aggregate).
- **Some attempts partially succeeded** → Classify by final state: log contains some samples → sample-level; no samples → structural.

---

## Preflight-Taxonomy Interaction

Preflight (run via `--limit 1` unless `skip_preflight=true`) is a structural probe. The Executor maps preflight outcomes to actions as follows:

| Preflight outcome | Category | Executor response |
|---|---|---|
| Command succeeds on first attempt | — | Proceed to full execution |
| Command succeeds after process-level retries within budget | Transient (recovered) | Proceed; note retry count |
| Exit 0, single sample errored → `--limit 3` retest, 0 of 3 error | Sample-level transient | Proceed; note retest outcome |
| Exit 0, single sample errored → `--limit 3` retest, 1–2 of 3 error | Sample-specific deterministic | Proceed; flag expected attrition rate |
| Exit 0, single sample errored → `--limit 3` retest, 3 of 3 error | All-samples deterministic | **Exclude**; record in Preflight Exclusions with all 3 error traces |
| All process-level retries exhausted, no usable log | Structural | **Exclude**; record in Preflight Exclusions with structural evidence |
| Pinned OpenRouter provider unavailable (routing error, no usable log) | Pinned provider unavailable | **Exclude**; record in Preflight Exclusions naming the pinned slug. Do **not** relax the pin to get the model through preflight — an unpinned run measures with a different instrument, and the orchestrator needs to decide whether to wait, re-pin, or drop the model |

Only the last two outcomes cause exclusion. Transient and sample-specific preflight outcomes are informational — they do not gate execution.

After preflight, the `_preflight/` log subdirectory is deleted. Preflight logs must not appear when the Analyst later ingests the experiment's log directory.

---

## Conventions

**Each invocation is self-contained.** Every delegation from the orchestrator to the Executor is a single-shot interaction: one request, one report, no follow-up within the same invocation. The Executor has no persistent memory across invocations; it rebuilds its understanding of the experiment from the request each time.

**Stdout-only reporting.** The report is written to stdout and captured by the orchestrator, which saves it to `artefacts/executor/report.md`. Errors (from the wrapping CLI, not from eval subprocesses) go to stderr.

**Section headings are markdown, and their text is verbatim.** Emit each report section as a markdown heading at the level shown in the Report Format above (`### Condition-Model Execution Matrix`, `#### Structural`, …), using the exact heading text, in the order given. Plain-text section titles are not sufficient: the orchestrator locates sections structurally, so a report whose content is perfect but whose headings are unmarked is a report it cannot reliably read. This is stated explicitly rather than left to be inferred from the formatting of this document.

**No scientific judgement on data quality.** The Executor reports objective observable properties — exit codes, sample counts, transcript termination shape, retry counts. It never decides whether a transcript is "good enough," whether a sample error is "acceptable," or whether an attrition rate is "too high." Those judgements belong to the orchestrator and the Analyst.

**Log organisation is per-condition.** Every condition gets its own subdirectory under `<experiment_dir>/logs/<condition_name>/`. Every invocation includes `--metadata condition=<condition_name>`, `--metadata model=<model_string>` and `--metadata route=<route>` so condition, model and route labels are embedded in the log file itself, providing redundancy against file reorganisation. The route label is not redundant with the model string: for an OpenRouter model, `model=` records what was *requested* while `route=` records how it was reached.

**Tag namespacing.** Tags use namespaced prefixes to avoid substring ambiguity: `--tags "exp:<experiment_name>,cond:<condition_name>"`.

**Inspect CLI knowledge.** Stable semantic and pattern-level content lives in `inspect_reference.md`. Flag-specific questions (exact spelling, default values, argument format) are resolved at invocation time via `uv run inspect <cmd> --help`, not from memory. This applies to every subcommand the Executor uses (`inspect eval`, `inspect log list`, etc.).

**Total wall-clock budget is not an Executor concern.** The Executor runs what it is asked to run. Cost and time budget management belong to the orchestrator and the user.

---

## Non-negotiable requirements

The Executor's report **must** satisfy these commitments. They are non-negotiable because failures on these dimensions propagate silently through the downstream pipeline and cannot be recovered by subsequent stages.

- **Full accountability.** Every condition-model pair submitted by the orchestrator must appear somewhere in the report — in the Execution Matrix, in Preflight Exclusions, or (if the pair was malformed) in Additional Notes. Silently discarding a pair is a silent failure.

- **Evidence with every structural failure.** Structural failures must include exit code, exact command, and the combined stdout+stderr tail. Reporting a structural failure without evidence robs the orchestrator of the material needed to diagnose root cause, forcing re-runs instead of fixes. If a command exits non-zero with genuinely no output on either stream, say so explicitly rather than omitting the evidence line.

- **Correct category assignment.** The four-category taxonomy is observable and rule-bound. Mis-classifying a structural failure as transient would lead the orchestrator to re-run instead of investigate; mis-classifying transient as structural would lead to unnecessary diagnostic work. The Executor applies the criteria literally, not by judgement.

- **Transcript termination metadata for every fully-executed pair.** The Analyst downstream assumes these counts are available. Omitting them forces the orchestrator to read log contents itself, which it is explicitly not allowed to do before the Analyst reports (per `orchestrator_responsibilities.md` Step 2e).

- **Structured concurrency fields.** The Concurrency Decision section's named fields must all be populated. A narrative-only rationale without the fields breaks the orchestrator's ability to audit the concurrency choice.

- **No scorer outputs anywhere in the report.** The Executor reports whether each condition-model pair *ran*, never how it *scored*. Accuracy, mean scores, metric values, per-sample scores, or any scorer-derived quantity must not appear in any section — not as an Execution Matrix column, not in the Summary, not in Additional Notes, nowhere. This is the load-bearing half of the information firewall: `orchestrator_responsibilities.md §2e` forbids the orchestrator from reading eval results between the Executor's report and the Analyst's report, so that its interpretation is anchored to the Analyst's qualitative findings rather than to a headline number. A score leaked here cannot be un-seen once the report is read, collapsing the firewall for the entire iteration. Reporting transcript *shape* (sample counts, termination metadata) is required and is not a scorer output; reporting what the scorer *computed* from those transcripts is forbidden.
