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

- `task`: path to the task file relative to `experiment_dir` (never absolute — Inspect rejects absolute task paths). A condition may also be given as a bare string, which is read as `{"task": "<string>", "args": {}}`.
- `args`: key-value pairs passed as `-T key=value` to the `inspect eval` invocation for that condition. Non-string values are rendered as JSON.

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
| `sample_limit` | int | Maps to `--limit`. Cannot be combined with `sample_id` (Inspect rejects the pair; the runner refuses the request) |
| `sample_id` | str | Maps to `--sample-id` (comma-separated ids; runs only those dataset entries) |
| `epochs` | int | Maps to `--epochs` |
| `skip_preflight` | bool | If `true`, the runner does not run preflight and goes straight to full execution |
| `max_parallel` | int | Upper bound on concurrent eval subprocesses. **Default 1** (the runner's ceiling) |
| `max_connections` | int | Maps to `--max-connections` (concurrent API calls within an eval). **Default 4** |
| `command_timeout_seconds` | int | Wall-clock limit per `inspect eval` subprocess. Default 14400 (4 h) |

Unrecognised keys are ignored with a note in **Additional Notes**. Other flags (`--retry-on-error 3`, `--no-fail-on-error`, `--display plain`, and `--with "openai>=…"` on `openrouter/…` models) are applied to every invocation by the runner without being in overrides — these are policy, not configuration.

**The concurrency ceiling is the runner's, not the Executor's.** `max_parallel: 1` / `max_connections: 4` are sized for a shared host running one Docker sandbox per sample (see `inspect_reference.md §Concurrency heuristics`). A caller who knows better sets them in `overrides`. The Executor may raise them only by passing `--max-parallel` / `--max-connections` together with `--rationale` to the runner; the rationale is recorded verbatim in the report's **Concurrency Decision**. Raising them silently is not possible — the runner refuses the flags without a rationale.

**`runs_per_condition` has been removed.** It was documented as a key but never implemented — nothing ever multiplied the commands — so a caller setting it believed it had taken effect when it had not. The runner now refuses a request that carries it, naming `epochs` (with `sample_id`) as the repeated-sampling mechanism; see *Resample runs* below for the one case where repeated whole-eval runs would genuinely differ.

### Resample runs (Model Forensics)

A *resample* is the Model Forensics equivalent of a baseline measurement: the unmodified eval run *n* times to establish how often the behaviour under investigation occurs at all. It is a normal Executor invocation with a constrained shape, not a new mode.

The paragraphs below are marked **[caller]** or **[Executor]**. Caller-facing paragraphs constrain how the request is built; Executor-facing paragraphs constrain what the Executor does with it. The Executor need only act on the latter — but the former are here rather than in the caller's own documentation because they define what makes a request a resample at all.

**[caller] Request shape.** Exactly one condition, named `baseline`, pointing at the unmodified task file with **no `-T` overrides at all**. The point of the resample is to characterise the episode's own setting; any `-T` argument makes it a variant of that setting rather than the setting itself, and this run is the investigation's frozen anchor (each round's comparisons are drawn against a concurrent same-round control by default — `diagnostician_responsibilities.md §3a`). One model — the frozen target model for the investigation. The episode is isolated with `sample_id` and repeated with `epochs`.

```json
{
    "experiment_name": "mf_<investigation>_resample",
    "experiment_dir": "investigations/<name>/iterations/00_resample",
    "conditions": {"baseline": "<path/to/unmodified_task.py>"},
    "models": ["<frozen target model>"],
    "overrides": {"sample_id": "<id of the episode under investigation>", "epochs": 30}
}
```

**[caller] `sample_id` is required, not optional, whenever the task has more than one sample.** `--epochs` repeats the *whole dataset*, so a task with *k* samples and `epochs: n` produces *k × n* transcripts of which only *n* resample the episode under investigation. The rest are a different scenario entirely: they inflate cost by a factor of *k*, and — worse — they contaminate the resample pool the Analyst draws negative controls and borderline cases from, with transcripts that were never the thing being studied. Pinning `sample_id` yields exactly *n* trajectories of exactly the episode, in one log directory.

Note that `sample_id` selects which dataset entry runs; it does not alter the task's content, so it does not conflict with the no-`-T`-overrides rule above.

**[caller] Why `epochs` is the resample mechanism.** `epochs` re-runs each selected *sample* *n* times **inside one eval**, producing one log directory and one `.eval` file containing all *n* trajectories. Each epoch is an independent rollout — a fresh model context and, for sandboxed tasks, a fresh sandbox — so for the overwhelming majority of evals this is exactly the independent resampling the investigation wants, and it keeps the resample in a single log directory.

The one exception is an eval carrying state that survives across samples within a single run: a module-level accumulator, a shared external service, a dataset the task mutates, a solver that caches. There, epochs are not independent draws, and only separate whole-eval runs would be an honest resample — which the runner does not offer. That is a blocker for the caller to raise rather than work around: the resample would be measuring a sequence rather than a population, and no override available here fixes that.

**[Executor] Log organisation.** Resample logs go to their own directory under their own `experiment_dir`, exactly as any single-condition run does: `<experiment_dir>/logs/baseline/`. This is the ordinary per-condition rule, stated here only because the Analyst later receives that one path as one condition and the resample is compared against for the life of the investigation.

**[Executor] Zero successful runs is reported, never recovered.** If every command for the `baseline` condition fails — nothing ran, or things ran and no samples completed — report it plainly and completely: the normal taxonomy applies, with exit code, command and output tail per the structural-failure rules. Do not relax a pin, drop `-M` arguments, widen a limit, or otherwise adjust the invocation to make it succeed. Standard retry policy applies and nothing beyond it. The Executor makes no judgement about what a zero count means for the investigation.

**[caller] Zero successful runs and zero behaviour rate are different things.** The Executor can observe the first and structurally cannot observe the second — it reports execution outcomes and never scorer outputs. A resample that ran cleanly but in which the target behaviour never occurred is a *finding* about reproducibility and terminates the investigation; a resample that failed to run is a broken setup and is fixed and re-run. Only the caller, holding the Analyst's report, can tell these apart. See `diagnostician_responsibilities.md` for what follows from each.

---

## Execution Mechanism

The request is executed by a deterministic batch runner, `scripts/execute_evals.py`, which the Executor drives and supervises. The runner, not the Executor, constructs the `inspect eval` commands, runs preflight, applies the concurrency ceiling, retries transient failures, classifies errors, quarantines preflight logs, reads transcript shape from the logs, and assembles a draft report in the section shape below. It runs the batch **detached** — in its own session, surviving the Executor's own Bash timeout or teardown — and keeps a score-free status file current.

```bash
uv run python scripts/execute_evals.py run <request.json>              # validate, build commands, launch detached; prints a receipt
uv run python scripts/execute_evals.py wait <run_dir> --timeout 540    # block until terminal (exit 0) or timeout (exit 3)
uv run python scripts/execute_evals.py status <run_dir>                # score-free status snapshot
uv run python scripts/execute_evals.py report <run_dir>                # draft report in this contract's shape
```

The run directory is `<experiment_dir>/artefacts/executor/runs/<experiment_name>_<timestamp>/`. It holds `request.json`, `plan.json` (the rendered commands), `status.json`, `worker.log`, `draft_report.md`, and a `private/` subtree with each command's raw stdout/stderr and the quarantined preflight logs. **`private/` is score-bearing** (Inspect's plain display prints running metrics) and is never read by the Executor or the orchestrator.

**The Executor's protocol is launch, then report.** (1) Validate the request against this contract and call `run` with the request file as received. (2) Poll with `wait` until the run is terminal. (3) Read the draft report; for anything tagged structural, diagnose from the evidence and, if a fix within the rules exists (a transient cause that exhausted retries, a Docker pool that needed `inspect sandbox cleanup docker`), re-run those pairs once with `run … --only <pair_id,…> --quarantine-existing-logs` and fold the outcome into the report. (4) Emit the final report. The Executor never runs `inspect eval` directly, never edits `plan.json`, and never reads under `private/`.

**Stale logs are refused, not appended to.** If a pair's log directory already holds a `.eval` file, `run` refuses unless `--quarantine-existing-logs` is passed, which moves the stale files to `<experiment_dir>/logs_quarantined/<run_id>/<condition>/`. Appending would silently double the Analyst's pool.

**Log organisation.** One directory per condition, `<experiment_dir>/logs/<condition>/`; when the request names more than one model, one directory per pair, `<experiment_dir>/logs/<condition>/<model-sanitised>/`, so the newest-log lookup is unambiguous.

---

## Executor → Orchestrator: Report Format

Each report from the Executor to the orchestrator contains the following sections, in order. The summary comes first; the remaining sections provide supporting detail for drill-down. Empty sections display `None.` or `No failures.` — never omitted — so the top-level shape is fixed and scannable.

### Summary

A 3–5 sentence overview of the execution. State: how many condition-model pairs were attempted and their final disposition (completed / completed-with-sample-errors / excluded at preflight / failed during full execution); the chosen `max_parallel` level; and the most decision-critical observation for the orchestrator (e.g., asymmetric exclusions between conditions, unusually high sample-level attrition, notable retry activity). No recommendation — this section conveys information, not next-step advice.

If the orchestrator stops reading here, it should know whether the execution succeeded cleanly and whether any pair needs further investigation.

The Summary ends with one fixed line: **`**Zero-sample pairs:**`** — `none.`, or the list of condition-model pairs whose final log completed **zero samples**. This is an execution outcome (typically a `sample_id` that matches nothing after a `-T` argument changed the sample ids, or a dataset filter that selected nothing) and is distinct from a zero *rate*, which the Executor cannot see. It is a separate line so it cannot be lost in the matrix; the same pairs also appear in the status file's `zero_sample_pairs`.

### Parent Log Directory

The absolute path to `<experiment_dir>/logs/`, the root under which per-condition log subdirectories are organised. This is the path the orchestrator will later partition by condition when constructing the Analyst's `transcript_source` mapping.

### Preflight Exclusions

Condition-model pairs that did not proceed to full execution because preflight found an excluding failure. Displays `None.` if all pairs passed preflight, or `Preflight skipped (skip_preflight=true).` if preflight was skipped.

For each excluded pair:

- **`<condition> / <model>`**
  - **Reason**: `structural` | `all-samples-deterministic-sample-error` | `pinned-provider-unavailable (routing)`
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
- **Zero samples completed**: pairs named on the Summary's zero-sample line

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

After preflight, the runner moves the `_preflight/` log subdirectory to the run directory's `private/preflight_logs/`. Preflight logs must not appear when the Analyst later ingests the experiment's log directory; they are quarantined rather than deleted so the evidence behind an exclusion survives.

---

## Conventions

**Each invocation is self-contained.** Every delegation from the orchestrator to the Executor is a single-shot interaction: one request, one report, no follow-up within the same invocation. The Executor has no persistent memory across invocations; it rebuilds its understanding of the experiment from the request each time.

**Stdout-only reporting.** The report is written to stdout. The detached launcher (`scripts/launch_subagent.py`) captures it to the per-invocation path `<artefacts-dir>/executor/report_<tag>_executor_<n>.md`; there is no separate save step. Errors (from the wrapping CLI, not from eval subprocesses) go to stderr, captured alongside.

**Section headings are markdown, and their text is verbatim.** Emit each report section as a markdown heading at the level shown in the Report Format above (`### Condition-Model Execution Matrix`, `#### Structural`, …), using the exact heading text, in the order given. Plain-text section titles are not sufficient: the orchestrator locates sections structurally, so a report whose content is perfect but whose headings are unmarked is a report it cannot reliably read. This is stated explicitly rather than left to be inferred from the formatting of this document.

**The report is the whole of stdout.** Everything printed is captured verbatim as the saved artefact, so begin directly with the first section heading — no preamble, no narration of readiness, no sign-off, and no horizontal rules outside sections. A leading "here is the report" line becomes a permanent part of a document that downstream agents read by structure.

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
