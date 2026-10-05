# Sub-Agent Invocation Reference

Sub-agents are invoked as CLI scripts via the Agent SDK. The orchestrator writes a JSON input file, runs the script via Bash, and captures the report from stdout.

## General Pattern

**Launch every sub-agent through the detached launcher**, never with a bare `uv run … main.py` in the foreground or a `nohup … &`:

```bash
uv run python scripts/launch_subagent.py launch <role> <input.json> \
    --investigation investigations/<name> --tag <label> [--artefacts-dir <dir>] [--cwd <dir>]
uv run python scripts/launch_subagent.py wait <receipt.json> --timeout 540   # exit 0 finished, 3 still running
uv run python scripts/launch_subagent.py status <receipt.json>
uv run python scripts/launch_subagent.py list investigations/<name>
```

- **Roles**: `explorer`, `executor`, `analyst`, `mapper`, `designer`, `critic`.
- **`--tag`**: the iteration or phase label (`00_resample`, `iter1`, `phase4`); the launcher assigns `<n>` so repeated invocations of one role never collide.
- **`--artefacts-dir`**: where the outputs land; defaults to `investigations/<name>/artefacts`. Pass the iteration's `artefacts/` for iteration-scoped roles.
- **What the launcher does**: copies the request to `investigations/<name>/requests/<tag>_<role>_<n>.json` (persistent — the session scratchpad is wiped on restart), starts the sub-agent under its own session (`setsid` semantics, so a harness teardown or a Bash timeout cannot kill it), writes stdout to `<artefacts-dir>/<role>/report_<tag>_<role>_<n>.md` and stderr to `stderr_<tag>_<role>_<n>.log`, records the SDK cost sidecar `cost_<tag>_<role>_<n>.json`, and prints a **receipt** JSON (pid, paths, `receipt_<tag>_<role>_<n>.json`) immediately.
- **Waiting**: `wait` blocks up to `--timeout` seconds (default 540, under the Bash tool's 10-minute cap) and exits 0 when the sub-agent has finished with an exit code recorded in the receipt, 3 if it is still running — call it again. Use absolute paths throughout; the Bash tool's working directory drifts.
- **Reading the report**: the report file *is* the sub-agent's stdout. There is no separate "save stdout to `report.md`" step; cite the per-invocation path in the log.
- **Exit codes** (recorded in the receipt): 0 = success, 1 = input validation error, 2 = agent error. `scripts/spend.py investigations/<name>` sums the cost sidecars and eval token usage.

The underlying CLI the launcher runs is `uv run --with claude-agent-sdk --with python-dotenv python subagents/<agent>/main.py <input.json> [--cwd <dir>]` — input JSON with agent-specific fields (below), markdown report on stdout, errors on stderr. All agents run fire-and-forget — no back-and-forth.

### Optional: `subagent_max_turns` (all agents)

Every agent's input JSON accepts an optional `subagent_max_turns` field — **the Agent SDK turn cap the sub-agent runs under** (default 100). Raise it for a work order known to need more turns (a many-condition Analyst ladder), or lower it to bound a cheap check. A sub-agent that hits the cap returns no report and the runner's error names it (`subtype=error_max_turns`), so the failure is diagnosable rather than silent. The field is threaded through `runner.py`; each agent's `agent.py` prompt builder must pass it to `run_agent(max_turns=…)` — a builder that does not read the key silently drops it. **Currently honoured by the Executor only**; the Explorer, Analyst, Mapper, Designer and Critic builders do not yet read it (their owning work-packages add the one-liner), so setting it on those roles is a no-op until they do.

**Mode-conditional required fields (Red-Teaming Critic only).** Every agent above validates a flat list of required fields at exit code 1. The Critic is the one exception: its only flat requirement is `mode`, and each mode then requires its own further fields, validated when the prompt is built rather than by the CLI. A bad `mode`, or a mode missing one of its fields, therefore surfaces as an **agent error (exit code 2)**, not an input error — do not read a Critic exit 2 as an API failure before checking the request against `critic_interface_contract.md §Modes`.

### Optional: `subagent_model` (all agents)

Every agent's input JSON accepts an optional `subagent_model` field — **the Claude model that the sub-agent itself runs on**. If omitted, the sub-agent uses the scaffold default. Use it to:

- run a sub-agent on a **less refusal-prone model** when an eval's content trips the default model's safety filters (e.g. dangerous-capability evals — the situation that motivated this field); or
- run sub-agents on a **cheaper or weaker model** (e.g. a Sonnet-class model) when the User prefers that cost/quality trade-off.

Do not confuse `subagent_model` with two other model fields:

- the Analyst's **`scanning_model`** — the model Scout uses to run LLM *scanners* (a task parameter), not the model the Analyst agent runs on; and
- the Executor's **`models`** — the eval's *target* models being studied, not the model the Executor agent runs on.

The `subagent_model` is an infrastructure choice; the others are part of the experiment. They are independent and may differ.

### Optional: `subagent_thinking` (all agents)

Every agent's input JSON also accepts an optional `subagent_thinking` field — **the extended-thinking configuration the sub-agent itself runs with**. If omitted, the sub-agent uses the scaffold default of `{"type": "adaptive"}`, which lets the model scale its own reasoning depth to the task (so the Executor's mechanical work draws little, while the Explorer and Analyst draw more). Override it to:

- **disable thinking** — `{"type": "disabled"}`; or
- set a **fixed budget** — `{"type": "enabled", "budget_tokens": 16000}`, e.g. when running a sub-agent on a `subagent_model` that does not support adaptive thinking (adaptive requires Opus 4.6+).

Like `subagent_model`, this is an infrastructure choice independent of the experiment. The two pair naturally: if you override `subagent_model` to a model without adaptive support, override `subagent_thinking` alongside it.

### Optional: `subagent_route` (all agents)

Every agent's input JSON also accepts an optional `subagent_route` field — **how the sub-agent reaches its model**. Two values:

- `"direct"` (default) — Anthropic, using `ANTHROPIC_API_KEY`. Unchanged behaviour.
- `"openrouter"` — points the sub-agent at OpenRouter's Anthropic-compatible endpoint using `OPENROUTER_API_KEY`, so `subagent_model` may name **any** model family (e.g. `openai/gpt-5`, `google/gemini-3-pro`).

The route is set per invocation, so one sub-agent can run through OpenRouter while another runs directly against Anthropic in the same investigation.

Two things to know before using it:

- **Thinking is auto-guarded.** Adaptive thinking is a Claude feature, so when the route is `openrouter` and the model is not a Claude model, `subagent_thinking` defaults to `{"type": "disabled"}` instead of adaptive. An explicit `subagent_thinking` still wins.
- **Every role is open to any sufficiently capable model.** No sub-agent is restricted to Claude. The interface contracts and system prompts are meant to specify each role completely enough that any capable agent can fulfil it — so if a non-Claude sub-agent produces a report that violates its contract, treat that as evidence the *guidance* is underspecified, not as a reason to restrict the model. Fix the contract. The defaults remain Claude simply because that is what the scaffold has been exercised against, not because the roles depend on it.

Running the Analyst cross-family is additionally a robustness check on the scaffold's own assumption that the sub-agent model is infrastructure rather than part of the experiment — the one place where a differing result is informative rather than merely a bug.

**For the Red-Teaming Critic, cross-family is a hard requirement, not a preference.** The Critic exists to supply reasoning decorrelated from the lead investigator's, so a same-family Critic is a methodological failure rather than a cost trade-off: every Critic invocation passes `subagent_route: "openrouter"` with a non-Claude `subagent_model`, and `/diagnose` refuses to start without `OPENROUTER_API_KEY` (an `OPENAI_API_KEY` does not substitute — the runner has no direct-OpenAI route). See `critic_interface_contract.md §Cross-family routing`.

### Transcript handoff: dump `.eval` logs to text before any sub-agent reads them

`.eval` logs are zip archives. A sub-agent that receives a `.eval` path *to read* — the Mapper's `reference_transcript`, the Critic's `transcript_paths` — gets bytes, not a transcript, and the failure is silent: the read succeeds, the agent reasons over nothing, and the report comes back empty or built on the file name alone. Three of six forensic runs hit this and each reinvented the same workaround mid-investigation.

The rule: **never pass a `.eval` path to a sub-agent as something to read. Dump first, pass the dump paths.**

```bash
uv run python scripts/dump_transcripts.py <logs dir or .eval> --out <dir>
```

- **Layout.** One text file per sample. For a directory input the log's relative path is mirrored, so `iterations/01/logs --out iterations/01/transcripts` yields `transcripts/<condition>/<log>/<sample_id>_epoch_<n>.txt` and a whole condition can be handed over as one directory. Blocks longer than 2,500 characters are truncated at both ends (`--cap 0` for none); reasoning is rendered in full where the provider exposes it and as the provider summary where it is encrypted. `--sample-id` / `--epoch` select within a log.
- **Scores are omitted by default.** Pass `--with-scores` only for logs whose scores the Diagnostician is already entitled to see: the reference episode, or logs the Analyst has reported on. Dumping this round's logs for the Critic *before* the Analyst reports is exactly when the default matters — it keeps a convenience tool from becoming a score-blackout leak.
- **Where it applies.** The Mapper's `reference_transcript` (dump the original episode once at Phase 0, to `artefacts/reference/`, and send that path). The Critic's `transcript_paths` in `candidate_critique` mode (the original plus the resample selection at step 2; the round's transcripts at 3d). Any other field that hands a sub-agent a log to read. **The Analyst is the exception**: its `transcript_source` and `behaviour_spec.reference_transcript` take `.eval` paths, because it reads logs through Scout and the Inspect API, never as text.
- The Diagnostician's own transcript reading (Phase 2, 3d step 7) is unaffected — read the dumps or the logs via the Inspect API, whichever is convenient. The rule is about what *sub-agents* receive.

## Environment Explorer

**Script**: `subagents/environment_explorer/main.py`

**Input JSON**:
```json
{
    "hypothesis": "Adding X to the system prompt increases Y...",
    "experiment_description": "Testing whether X affects Y...",
    "environment_path": "/absolute/path/to/eval/environment",
    "constraints": "Focus on system prompt and scoring; skip the scaffold directory.",
    "subagent_model": "claude-opus-4-7",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `environment_path` must be an existing directory
- `constraints` is optional; free-form string passed through to the Explorer to shape or narrow exploration
- `subagent_model` is optional; the model the Explorer agent runs on (see General Pattern above)
- `subagent_thinking` is optional; the Explorer's extended-thinking config (see General Pattern above)

**Returns**: Structured markdown report. See `explorer_interface_contract.md` for the full request and report format.

## Experiment Executor

**Script**: `subagents/experiment_executor/main.py`

**Input JSON**:
```json
{
    "experiment_name": "explicit_goal_framing",
    "experiment_dir": "/absolute/path/to/experiment",
    "conditions": {
        "control": {
            "task": "task.py",
            "args": {}
        },
        "treatment": {
            "task": "task.py",
            "args": {"system": "system_prompt_treatment.txt"}
        }
    },
    "models": ["anthropic/claude-sonnet-4-5-20250929"],
    "model_routing": {
        "anthropic/claude-sonnet-4-5-20250929": {
            "route": "direct",
            "provider_slug": null,
            "model_args": {}
        }
    },
    "overrides": {
        "sample_limit": 50,
        "epochs": 1,
        "skip_preflight": false
    },
    "subagent_model": "claude-opus-4-7",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `experiment_dir` must be an existing directory
- `model_routing` is optional; per-model routing directives resolved by the Orchestrator in Phase 1 and rendered by the Executor as `-M` arguments. Omit it and every model is invoked directly (the pre-existing behaviour). For OpenRouter models, get the provider slug and model args from `scripts/openrouter_endpoints.py` — never guess a slug. See `executor_interface_contract.md §Model routing`
- `overrides` is optional; supported keys: `sample_limit`, `sample_id`, `epochs`, `skip_preflight`, `max_parallel`, `max_connections`, `runs_per_condition`. `sample_id` pins the eval to named dataset entries (`--sample-id`) and pairs with `epochs` for resampling one episode — without it, `epochs` repeats the *whole dataset*. `runs_per_condition` is accepted but not applied (the Executor records it and runs once — see `executor_interface_contract.md §Overrides`); use `epochs` for repeated sampling
- `subagent_model` is optional; the model the Executor agent runs on. **Distinct from `models`**, which are the eval's target models — `subagent_model` has no provider prefix (e.g. `claude-opus-4-7`), whereas `models` entries do (e.g. `anthropic/claude-sonnet-4-5-20250929`)
- `subagent_thinking` is optional; the Executor's extended-thinking config (see General Pattern above)

**Returns**: Structured markdown report. See `executor_interface_contract.md` for the full request and report format.

## Transcript Analyst

**Script**: `subagents/transcript_analyst/main.py`

**Input JSON**:
```json
{
    "topic": "How models reason about their operational context and whether they adjust their approach based on perceived circumstances",
    "transcript_source": {
        "condition_A": "/absolute/path/to/logs/run_001",
        "condition_B": "/absolute/path/to/logs/run_002"
    },
    "scanning_model": "openai/gpt-4.1-mini",
    "constraints": {"limit": 100},
    "artefacts_dir": "/absolute/path/to/investigation/artefacts",
    "subagent_model": "claude-opus-4-7",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `transcript_source` values must all be existing directories containing `.eval` log files
- `topic` must be a **neutral description** — never the hypothesis
- Use **opaque condition labels** (condition_A, condition_B) — randomise the mapping to conditions
- `scanning_model`, `constraints`, and `artefacts_dir` are optional. If `scanning_model` is an `openrouter/…` string, pin its route the same way target-model routes are pinned — the scanner is a measurement instrument held constant across conditions, and unpinned routing lets it drift between condition scans in a way that is indistinguishable from a real effect (see `analyst_interface_contract.md §Scanning model`)
- `subagent_model` is optional; the model the Analyst agent runs on. **Distinct from `scanning_model`**, which is the model Scout uses for LLM scanners — `subagent_model` changes which model *does the analysis*, `scanning_model` changes which model the scanners *call*
- `subagent_thinking` is optional; the Analyst's extended-thinking config (see General Pattern above)
- When `artefacts_dir` is provided, the analyst writes all file outputs to `<artefacts_dir>/analyst/`

**Returns**: Scanner definitions, validation metrics, quantified results (per-condition detection rates), scan results path, transcript exclusions, transcript excerpts, additional observations. See `analyst_interface_contract.md` for the full report format.

### Forensic mode: `behaviour_spec` (Diagnostician only)

In a Model Forensics investigation the Analyst additionally accepts an optional `behaviour_spec` object. **Its presence is the mode switch**: `agent.py` selects the forensic system prompt when the field is present and the unchanged Orchestrator prompt when it is absent, so an Orchestrator invocation must never send it — the base pipeline's behaviour is preserved by leaving it out, not by any in-prompt conditional.

```json
{
    "topic": "<neutral topic — rules unchanged>",
    "transcript_source": {"condition_A": "/abs/path/logs/run_001", "condition_B": "/abs/path/logs/run_002", "condition_C": "/abs/path/logs/run_003"},
    "behaviour_spec": {
        "description": "The model edited the test file rather than the implementation",
        "reference_transcript": "/abs/path/to/original.eval",
        "grader_dir": "/abs/path/to/artefacts/analyst/grader",
        "construal_checks": [
            {"id": "M1", "property": "Whether the reasoning treats the deadline as binding — reasoning about time pressure, prioritising speed, declining to verify"},
            {"id": "M2", "property": "Whether the reasoning treats the failing test as the specification to satisfy rather than as a check on the specification"}
        ]
    }
}
```

- `description` is always required within the spec; `reference_transcript` is required on the first build and optional once `grader_dir` is populated; `grader_dir` absent/empty means build-and-persist, populated means load-and-apply-unmodified
- `construal_checks` is optional: **one de-identified set per round** — every variant's check, each with an opaque id (`M<n>`) and no indication of which condition it bears on. `agent.py` renders the set as its own `## Construal Checks` request section with the no-verdict rule attached; the Analyst validates each as its own scanner, rates each across **every** condition, reports the full pairwise difference matrix and a scorability label per check, and states **no** `succeeded`/`failed`/`ambiguous` verdict. The Diagnostician derives the verdicts from its private check→variant mapping and intended directions (`diagnostician_analyst_guide.md §Deriving the verdicts`). A single-string `manipulation_check` is still accepted and normalised to a one-element set (`M1`); sending both fields is an input error (exit 2)
- `agent.py` also appends a `## Work Order` section to every forensic request — grader → construal checks → eval-awareness split → open-ended scan — restating the addendum's order so that a turn-starved invocation leaves the open-ended half thin rather than the auxiliaries unvalidated. Large workloads (roughly > 24 scanner×condition passes, or > 8 conditions) are split **by check group, never by condition** (addendum §Work order → *Split protocol*); this pairs with the runner's turn cap (`subagent_max_turns`, once threaded through `runner.py`), which is the other lever
- **The firewall is not breached**: the spec names the *explanandum* (what to measure), never an explanation, a predicted direction, or which condition is which. The neutral `topic` remains required and its rules are unchanged — forensic mode is two measurements (fixed grader + open-ended scan), never one
- **Grader reuse via `grader_dir` is what makes rates comparable across iterations** — pass the same directory every round after the first, and treat a hash change between reports as an instrument change
- **`scanning_model` and the target's family.** The scanner is part of the instrument; when its author family is the target's (or the Analyst's own `subagent_model` is), the addendum obliges the Analyst to hand-read its validation labels (or use a cross-family validator), to prefer a deterministic parse for action-mediated behaviour, and to state the family facts under *Instrument family* — but the choice is the Diagnostician's: prefer a cross-family `scanning_model` where one is available at acceptable cost, and record the same-family case and its mitigations in the log (`diagnostician_analyst_guide.md §Scanning model and target family`)
- `behaviour_spec` is validated only for the shape of its check set: `main.py`'s `required_fields` is unchanged (`topic`, `transcript_source`), and `agent.py` raises on a malformed `construal_checks` (both fields, non-list, empty property, duplicate ids) — everything else in a malformed spec reaches the Analyst as JSON. Check it by hand before sending
- Request sub-field rules and the forensic report sections (grader provenance, extraction provenance and exhibit, per-condition precision audit, instrument family, floor check, construal-check rates and scorability, `### Behaviours not captured by the grader`, `### Eval-awareness split` with its `unmeasurable` gate) are specified in **`.claude/docs/contracts/analyst_forensic_addendum.md`** — read it alongside the base contract for every forensic invocation. The base contract deliberately carries no pointer to the addendum (the Orchestrator-mode Analyst reads the base contract, and a pointer would reintroduce the mode ambiguity the split removes), so this reference and `diagnostician_responsibilities.md` are how the Diagnostician finds it
- The deterministic half of the forensic report — Wilson intervals, κ, φ, the precision audit, the scorability label, the extraction-provenance exhibit — lives in `subagents/transcript_analyst/forensic_checks.py`, which the Analyst is told to use; the Diagnostician may use the same functions to re-derive a report's numbers from its persisted artefacts

## Environment Mapper

**Script**: `subagents/environment_mapper/main.py`

**Input JSON**:
```json
{
    "environment_path": "/absolute/path/to/eval/environment",
    "behaviour_description": "The model wrote a special-case branch returning the expected value for the test inputs rather than implementing the function",
    "reference_transcript": "/absolute/path/to/original.eval",
    "constraints": "The eval is large — prioritise the prompt and scoring path.",
    "subagent_model": "claude-opus-4-8",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `environment_path` must be an existing directory — the frozen original environment
- `behaviour_description` is the **explanandum**: what the model did, never why. It is not a hypothesis and must not contain one
- `reference_transcript` and `constraints` are optional; the transcript is read only to check the environment-as-written against the episode-as-run. Pass a **plain-text dump**, not the `.eval` (see §Transcript handoff)
- The Mapper runs **once per investigation**, before any candidate exists; save its stdout to `artefacts/mapper/report.md` — the Designer and Critic receive that path

**Returns**: Summary (with fixed-format `**Prohibition status:**` / `**Scorer detection:**` / `**Defect sufficiency:**` lines), file catalogue, pipeline model, instruction analysis, scorer analysis, tool & environment analysis, manipulable surface inventory, environment defects, uncertainties. See `mapper_interface_contract.md` for the full request and report format.

## Variant Designer

**Script**: `subagents/variant_designer/main.py`

**Input JSON**:
```json
{
    "mapper_report_path": "/absolute/path/to/artefacts/mapper/report.md",
    "environment_path": "/absolute/path/to/iteration/environment",
    "candidates": [
        {
            "id": "C1",
            "slug": "task_confusion",
            "statement": "The model read the failing test as the specification and satisfied it directly.",
            "discriminating_signature": "Removing the visible test file should collapse the behaviour; strengthening the docstring should not.",
            "status": "live"
        }
    ],
    "regime": "ablate",
    "environment_rate": {"rate": 0.41, "n": 40},
    "variant_budget": 3,
    "environment_delta": [
        {"name": "deadline_removed", "description": "The deadline sentence at prompt.md:34 was deleted."}
    ],
    "variations_tried": [
        {"name": "deadline_removed", "description": "Deleted the deadline sentence from the task prompt."}
    ],
    "elimination_targets": ["C3"],
    "constraints": "Single-file diffs only this round.",
    "subagent_model": "claude-opus-4-8",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `environment_path` must be an existing directory: **the current iteration's copy** that diffs must apply against, not necessarily the original
- `environment_rate` (note: **not** `baseline_rate`) is the measured rate of the environment supplied, `{"rate": <decimal>, "n": <int>}` — pass the step-1c baseline only when the environment *is* a fresh copy of the original
- `regime` is `"ablate"` or `"amplify"` — a steer, not a directive; the Designer may mark individual variants off-regime with a reason
- `environment_delta` is **required in practice whenever the environment is a derived variant** (omit it entirely for a fresh original copy): what was changed, never what it did — no rates, no outcomes. Without it the Designer meets a map/tree mismatch it can only read as a stale map or a broken environment. Check the report's `**Environment:**` Summary line: `original` against a derived environment means the delta did not arrive and surface locations are untrustworthy
- `variations_tried` is optional: every variation already run across all rounds and branches, outcomes stripped
- `elimination_targets` is optional and triggers a **positive-control variant** per named candidate (outside `variant_budget`). Each target costs a condition — name one only where retiring it would change the leading explanation or move the equivalence-class boundary
- Only `live` candidates are sent

**Returns**: Summary (with fixed-format `**Environment:**` / `**Discriminating variants:**` / `**Off-regime variants:**` / `**Positive controls:**` / `**Headroom:**` lines), proposed variants with diffs and confound declarations, discrimination matrix, variant dependencies (only if applicable), confounds & risks, uncertainties. See `designer_interface_contract.md` for the full request and report format.

## Red-Teaming Critic

**Script**: `subagents/redteam_critic/main.py`

The Critic's only CLI-validated field is `mode` ∈ `{candidate_critique, diff_review, prediction}`; each mode then requires its own fields (see the General Pattern note — missing ones exit 2, not 1). Always cross-family: `subagent_route: "openrouter"` with a non-Claude model.

**Input JSON** (`candidate_critique` shown):
```json
{
    "mode": "candidate_critique",
    "transcript_paths": ["/absolute/path/to/original.eval"],
    "mapper_report_path": "/absolute/path/to/artefacts/mapper/report.md",
    "candidates": [
        {
            "id": "C1",
            "slug": "task_confusion",
            "statement": "The model read the failing test as the specification and satisfied it directly.",
            "discriminating_signature": "Removing the visible test file should collapse the behaviour; strengthening the docstring should not.",
            "status": "live"
        }
    ],
    "iteration": 0,
    "evidence_summary": "Optional after 3d: rates and directions, stated mechanically.",
    "subagent_model": "google/gemini-3-pro",
    "subagent_route": "openrouter"
}
```

- `candidate_critique` requires `transcript_paths`, `mapper_report_path`, `candidates`, `iteration` (0 for the step-2 invocation); `evidence_summary` is optional and post-3d only. `transcript_paths` are **plain-text dumps**, never `.eval` files (see §Transcript handoff)
- `diff_review` requires `diffs` (array of `{"name": ..., "diff": ...}`), `environment_path`, `mapper_report_path` — and **must not** receive the candidates, the discrimination matrix, or the behaviour description; the blinding is the mode's value
- `prediction` requires `investigation_log_path`, `leading_explanation`, `untested_surfaces` — and must not receive the Diagnostician's own prediction
- Sending a withheld field does not error: it silently converts the review into a much weaker one. The blinding is the caller's responsibility

**Returns**: only the active mode's sections — `candidate_critique`: independent reading (written before the ledger is read), blind spots, assumption audit, proposed additional candidates (`X<n>` ids), retirement advice; `diff_review`: summary, per-diff analysis, cross-diff analysis, inferred purpose (last); `prediction`: chosen factor, ordinal prediction, reasoning, falsifier. See `critic_interface_contract.md` for all three request and report formats.
