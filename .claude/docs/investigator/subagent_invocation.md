# Sub-Agent Invocation Reference

Sub-agents are CLI scripts on the Agent SDK, always run through the detached launcher below. The lead agent (Orchestrator or Diagnostician) writes a JSON input file and launches it; the launcher persists the request and writes the report — the sub-agent's stdout — to a per-invocation file. Every agent is fire-and-forget: no back-and-forth. How the runner isolates and configures sub-agents is documented for maintainers in `docs/subagent_internals.md`; nothing there is needed to run an investigation.

**Where the input JSON is drafted.** Write every request (and any file it is built from, such as a candidate ledger exported by `ledger_to_json.py`) with the Write tool under `investigations/<name>/requests/drafts/` — never a session scratchpad (wiped on restart) and never a sibling directory such as `request_drafts/`. The Analyst's file firewall denies `investigations/*/requests/` and everything beneath it, so a hypothesis in an Explorer request or a ledger in a Critic request cannot reach it from there; elsewhere it could.

## General Pattern

**Launch every sub-agent through the detached launcher**, never with a bare `uv run … main.py` in the foreground or a `nohup … &`:

```bash
uv run python scripts/launch_subagent.py launch <role> <input.json> \
    --investigation investigations/<name> --tag <label> [--artefacts-dir <dir>] [--cwd <dir>]
uv run python scripts/launch_subagent.py wait <receipt.json> --timeout 3300  # in the background; exit 0 finished, 3 still running, 4 died (terminal)
uv run python scripts/launch_subagent.py status <receipt.json>
uv run python scripts/launch_subagent.py list investigations/<name> [--unfinished]
```

- **Roles**: `explorer`, `executor`, `analyst`, `mapper`, `designer`, `critic`.
- **`--tag`**: the iteration or phase label (`iter_00`, `iter_01`, `phase4`) — keep it neutral, since it is written into every report, stderr, cost and request filename in the artefacts tree; the launcher assigns `<n>` so repeated invocations of one role never collide.
- **`--artefacts-dir`**: where the outputs land; defaults to `investigations/<name>/artefacts`. Pass the iteration's `artefacts/` for iteration-scoped roles.
- **What the launcher does**: copies the request to `investigations/<name>/requests/<tag>_<role>_<n>.json`, starts the sub-agent detached (a Bash timeout or harness teardown cannot kill it), writes stdout to `<artefacts-dir>/<role>/report_<tag>_<role>_<n>.md`, stderr to `stderr_<tag>_<role>_<n>.log` and the cost sidecar to `cost_<tag>_<role>_<n>.json`, and prints a **receipt** JSON (pid, paths, `receipt_<tag>_<role>_<n>.json`) immediately.
- **Waiting in the background**: run each `wait` as a background Bash command (`run_in_background: true`) with `--timeout 3300`. The harness tells you when it exits; until then, get on with independent work or end your turn — do not poll it, read its output file or sleep. If it exits 3 (still running), start another background `wait`. Every foreground poll is a turn that re-reads your whole context, and a 55-minute wait stays inside your one-hour prompt cache. Only if your harness cannot run background commands, poll in the foreground with `--timeout 540`.
- **Say what you are waiting for.** Before each background `wait`, tell the user in one line what it is waiting on, when it should finish and when you will next check — e.g. *"Waiting on the Executor's 200-sample resample; expected around 09:35; next check by 09:40."* Estimate the finish from measured durations (a resample's episodes per minute, a role's earlier invocations), or say "finish time unknown" rather than guess; the next check is the `wait`'s timeout from now. A user returning to a quiet session then knows whether it is working or stuck.
- **`wait` semantics**: exit 0 when the sub-agent has finished (or failed to start), 3 if it is still running, and **4 if its process died without recording an exit** (supervisor killed, host restart): that is terminal, so stop waiting and handle it as a failed invocation (`list --unfinished` gives its relaunch command). `status` uses the same codes. Use absolute paths throughout; the Bash tool's working directory drifts.
- **Reading the report**: the report file *is* the sub-agent's stdout; cite the per-invocation path in the log. Locate sections by their verbatim headings and disregard any stray text before the first one.
- **Exit codes** (recorded in the receipt): 0 = success, 1 = input validation error, 2 = agent error. `scripts/spend.py investigations/<name>` gives the running total: sub-agent cost sidecars, plus the lead session's own cost at API prices, plus eval token usage.
- **Credential exhaustion** is reported distinctly: an exit 2 whose provider refused on account credit or a usage cap carries `credentials_exhausted (route=<route>, model=<model>)` in its stderr, and the receipt and cost sidecar record `failure_class: credentials_exhausted` plus the route (`status` and `list` show it). Re-invoking on the same route fails identically; when no live route is left it triggers the lead doc's Suspension procedure (§Sub-Agent Failure Handling).
- **`list --unfinished`** prints the invocations not yet cleanly finished, each with a ready-to-run `launch` command for its persisted request — or "still running" for live ones. A failed invocation drops out once a later one of the same role and tag, asking for the same work, has exited 0 (a request fixed and relaunched under a different task is not recognised as superseding it, so prune those by hand). `RESUME.md`'s relaunch section is pasted from it.

**Isolation, in brief.** A sub-agent sees only what its request and contract give it: no `CLAUDE.md`, memory, hooks, skills or project settings, only its role's tools, and file hooks that deny its restricted files — the condition label map (`CONDITION_LABEL_MAP*`) to every role. A content search over a directory holding a restricted file is denied with a request to narrow it. The hooks catch the accidental read, not a determined one.

### Optional: `subagent_max_turns` (all agents)

The Agent SDK turn cap the sub-agent runs under (default 100). Raise it for a work order known to need more turns (a many-condition Analyst ladder), or lower it to bound a cheap check. A sub-agent that hits the cap returns no report and the runner's error names it (`subtype=error_max_turns`).

**Mode-conditional required fields (Red-Teaming Critic only).** Every other agent validates a flat list of required fields at exit code 1. The Critic's only flat requirement is `mode`; each mode's further fields are validated when the prompt is built. A bad `mode`, or a mode missing one of its fields, therefore surfaces as an **agent error (exit code 2)** — check the request against `critic_interface_contract.md §Modes` before reading a Critic exit 2 as an API failure.

### Optional: `subagent_model` (all agents)

**The model the sub-agent itself runs on** (a Claude model by default; any family via `subagent_route: "openrouter"`). Override it to run a sub-agent on a less refusal-prone model when an eval's content trips the default's safety filters, or on a cheaper model when the User prefers that cost/quality trade-off. Do not confuse it with the Analyst's **`scanning_model`** (the model Scout's LLM scanners call — part of the instrument) or the Executor's **`models`** (the eval's target models — the thing studied). `subagent_model` is an infrastructure choice; the others are part of the experiment.

### Optional: `subagent_thinking` (all agents)

**The extended-thinking configuration the sub-agent runs with.** The default `{"type": "adaptive"}` lets the model scale its reasoning to the task. Alternatives: `{"type": "disabled"}`, or a fixed budget `{"type": "enabled", "budget_tokens": 16000}`. The default model, Opus 5.5, accepts only adaptive thinking — both alternatives are rejected for it — so use them only alongside a `subagent_model` that takes them. Conversely, adaptive thinking needs Opus 4.6 or later: if you override `subagent_model` to a model without it, override `subagent_thinking` alongside. Claude sub-agents also run at a pinned reasoning effort of `high`.

### Optional: `subagent_route` (all agents)

**How the sub-agent reaches its model.** Deliberately only two values:

- `"direct"` (default) — Anthropic, using `ANTHROPIC_API_KEY`.
- `"openrouter"` — OpenRouter's Anthropic-compatible endpoint using `OPENROUTER_API_KEY`, so `subagent_model` may name **any** family (e.g. `openai/gpt-5`, `google/gemini-3-pro`).

The route is set per invocation. On `openrouter` with a non-Claude model, thinking defaults to disabled (an explicit `subagent_thinking` still wins), the runner sets the model's context window itself, and a `[claude-code:unrecognized_model]` line in stderr is benign. No role is restricted to Claude: if a non-Claude sub-agent's report violates its contract, treat that as evidence the contract is underspecified, not as a reason to restrict the model.

### Report conformance (every role on a non-Claude model)

**Whenever a sub-agent runs on a non-Claude model, run `scripts/check_report.py` on its report before reading it, and re-ask once on failure.** It checks the contract's section headings — verbatim, at level 3, in order, once each — plus the Critic's discriminating signatures and the Executor's four Error Summary sub-headings:

```bash
uv run --no-project python scripts/check_report.py <role> <report.md> [--mode <critic mode>] [--forensic]
```

`--mode` is required for the Critic; `--forensic` checks an Analyst report against the forensic addendum (the request carried `behaviour_spec`). Exit 0 conformant, 1 defects (printed one per line), 2 usage error. The re-ask is a fresh invocation of the **identical request** plus two fields, sent together and never on a first attempt:

- **`conformance_defects`** — array of strings: the defect lines `check_report.py` printed, verbatim.
- **`previous_report`** — absolute path to the non-conformant report.

The agent **restructures** that report into the contract's headings, re-deriving content only for a section that was genuinely missing — not redoing the work, which would cost a full run and could change the findings. Never put a reformatting request in `constraints`: that is a scope limit. A malformed pair (empty list, relative or missing path, one without the other) is an agent error (exit 2).

| Role | Re-ask |
|---|---|
| Explorer, Mapper, Critic | `conformance_defects` + `previous_report`; the source is re-read only for a missing section (the Critic supplies a missing discriminating signature) |
| Designer | the same; every proposed variant and its diff are carried over unchanged, never re-designed |
| Analyst | the same, with `artefacts_dir` (and any `grader_dir`) unchanged: a missing section is rebuilt from its own saved artefacts, and no scan is re-run, no scanner re-validated and no grader rebuilt |
| Executor | **no re-invocation** — that would re-run the evals. Regenerate the report with `uv run python scripts/execute_evals.py report <run_dir>`, where `<run_dir>` is the newest `<experiment_dir>/artefacts/executor/runs/<experiment_name>_<stamp>/` (score-free, already in the contract's shape); check it and use it, noted in the log |

A second failure is read structurally and recorded in the log, never re-asked again. Non-Claude models substitute and merge headings, and an omitted section is otherwise indistinguishable from "nothing to report".

**The Critic is always cross-family**: every Critic invocation passes `subagent_route: "openrouter"` with a non-Claude `subagent_model` (`critic_interface_contract.md §Cross-family routing`; the gate itself is in the lead skill).

### Transcript handoff: dump `.eval` logs to text before any sub-agent reads them

`.eval` logs are zip archives. A sub-agent handed a `.eval` path *to read* — the Mapper's `reference_transcript`, the Critic's `transcript_paths` — gets bytes, and the failure is silent: the read succeeds and the report comes back built on nothing. **Never pass a `.eval` path to a sub-agent as something to read. Dump first, pass the dump paths.**

```bash
uv run python scripts/dump_transcripts.py <logs dir or .eval> --out <dir>
```

- **Layout.** One text file per sample. For a directory input the log's relative path is mirrored, so `iterations/01_round1/logs --out iterations/01_round1/transcripts` yields `transcripts/<condition id>/<log>/<sample_id>_epoch_<n>.txt`, and a whole condition can be handed over as one directory. Blocks over 2,500 characters are truncated at both ends (`--cap 0` for none); reasoning is rendered in full where the provider exposes it and as the provider summary where it is encrypted. `--sample-id` / `--epoch` select within a log.
- **Dumps stay score-free.** Never pass `--with-scores` on anything that is, or will become, a transcript source — the reference episode and the resample pool included: a scored dump invites choosing what to read by score. Sample which transcripts to read at random. **Critic dumps are always score-free** — Critics given scored dumps argue about the eval's scorer; if the instrument matters, describe it in the Critic's `evidence_summary`. The flag is only for inspecting logs the Analyst has already reported on.
- **The reference episode is never a condition.** The Analyst receives it only as `behaviour_spec.reference_transcript`; its transcript source at 1c is the fresh resample pool only. `check_blinding.py` blocks a source log that is the reference file or a byte-identical copy.
- **Where it applies.** The Mapper's `reference_transcript` (dump the original episode once at Phase 0, to `artefacts/reference/`). The Critic's `transcript_paths` in `candidate_critique` mode. Any other field that hands a sub-agent a log to read. **The Analyst is the exception**: its `transcript_source` and `behaviour_spec.reference_transcript` take `.eval` paths, because it reads logs through Scout and the Inspect API.
- The Diagnostician's own transcript reading (steps 2 and 3d) may use the dumps or the Inspect API; the rule is about what *sub-agents* receive.

## Environment Explorer

**Script**: `subagents/environment_explorer/main.py`

**Input JSON**:
```json
{
    "hypothesis": "Adding X to the system prompt increases Y...",
    "experiment_description": "Testing whether X affects Y...",
    "environment_path": "/absolute/path/to/eval/environment",
    "constraints": "Focus on system prompt and scoring; skip the scaffold directory.",
    "subagent_model": "claude-opus-5-5",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `environment_path` must be an existing directory
- `constraints` is optional; free-form string passed through to the Explorer to shape or narrow exploration
- `conformance_defects` and `previous_report` are optional and sent only together, only for the one re-ask after `check_report.py explorer` fails (§Report conformance)

**Returns**: Structured markdown report. See `explorer_interface_contract.md` for the full request and report format.

## Experiment Executor

**Script**: `subagents/experiment_executor/main.py`

**Input JSON**:
```json
{
    "experiment_name": "iter_01",
    "experiment_dir": "/absolute/path/to/experiment",
    "conditions": {
        "c_8b21": {
            "task": "c_8b21/task.py",
            "args": {}
        },
        "c_3f9a": {
            "task": "c_3f9a/task.py",
            "args": {}
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
    "subagent_model": "claude-opus-5-5",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `experiment_dir` must be an existing directory
- Condition names are **opaque execution ids** (`c_<4 hex>`) and `experiment_name` is neutral: the runner writes both into the log directory, tags and metadata the Analyst later reads. The id → real-name map is the caller's private `CONDITION_LABEL_MAP_iter<N>.json`
- `model_routing` is optional; per-model routing directives resolved at scoping and rendered by the Executor as `-M` arguments. Omit it and every model is invoked directly. For OpenRouter models, get the provider slug and model args from `scripts/openrouter_endpoints.py` — never guess a slug (`executor_interface_contract.md §Model routing`)
- `overrides` is optional; supported keys: `sample_limit`, `sample_id`, `epochs`, `skip_preflight`, `max_parallel`, `max_connections`, `command_timeout_seconds`. `sample_id` pins the eval to named dataset entries and pairs with `epochs` for resampling one episode — without it, `epochs` repeats the *whole dataset*. The removed `runs_per_condition` key is refused; use `epochs`
- `subagent_model` (no provider prefix, e.g. `claude-opus-5-5`) is the model the Executor agent runs on — **distinct from `models`**, the eval's target models, which carry one (e.g. `anthropic/claude-sonnet-4-5-20250929`)
- `conformance_defects` / `previous_report` are **not accepted**: the Executor is never re-asked, so sending either is an agent error (exit 2). Regenerate a non-conformant report with `execute_evals.py report <run_dir>` (§Report conformance)

**Returns**: Structured markdown report. See `executor_interface_contract.md` for the full request and report format.

## Transcript Analyst

**Script**: `subagents/transcript_analyst/main.py`

**Input JSON**:
```json
{
    "topic": "How models reason about their operational context and whether they adjust their approach based on perceived circumstances",
    "transcript_source": {
        "condition_A": "/absolute/path/to/iter_01/logs/c_3f9a",
        "condition_B": "/absolute/path/to/iter_01/logs/c_8b21"
    },
    "constraints": {"limit": 100},
    "execution_notes": ["condition_B: 7 of 100 samples hit the message limit"],
    "artefacts_dir": "/absolute/path/to/investigation/artefacts",
    "subagent_model": "claude-opus-5-5",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `transcript_source` values must all be existing directories containing `.eval` log files
- `topic` must be a **neutral description** — never the hypothesis
- Use **opaque condition labels** (condition_A, condition_B), with the mapping to conditions randomised, pointing at directories named by opaque condition ids — never by a real condition name or a role word
- **Before every launch, run `uv run python scripts/check_blinding.py <CONDITION_LABEL_MAP_iter<N>.json> <request.json>`; a non-zero exit blocks the invocation.** Exit 1 lists each blocking hit (channel — label, path, symlink target, file name, `task_file`, tags, metadata — file, and offending token); exit 2 is a usage error; warnings (a differing `task_args`, mixed-iteration layouts) print without changing the exit code and are recorded in the log and the report's caveats
- `scanning_model`, `constraints`, and `artefacts_dir` are optional. Omit `scanning_model` unless you have a reason to set it (a cross-family scanner for a Claude target, say): the Analyst then uses the project default, Haiku 5.5 where it validates and Sonnet 5.5 where it doesn't (`analyst_interface_contract.md §Scanning model`). An `openrouter/…` scanning model gets its route pinned like a target model's — the scanner is an instrument held constant across conditions, and unpinned routing lets it drift between condition scans indistinguishably from a real effect
- `subagent_model` is the model the Analyst agent runs on — **distinct from `scanning_model`**, the model the scanners call
- When `artefacts_dir` is provided, the analyst writes all file outputs to `<artefacts_dir>/analyst/`
- `execution_notes` is optional (both modes): a string or list of strings carrying **non-directional execution facts only** — per-condition retries, sample errors, preflight exclusions, termination counts — under this invocation's opaque labels (e.g. `"condition_B: 7 samples hit the message limit"`). Never a real condition name, a role word, the hypothesis or an expected direction. Any other shape is an agent error (exit 2); `check_blinding.py` scans it with the rest of the request
- `conformance_defects` and `previous_report` are optional and sent only together, only for the one re-ask after `check_report.py analyst` fails (§Report conformance). Keep `artefacts_dir` (and any `grader_dir`) unchanged
- `construal_checks` is optional (**Orchestrator mode only**): a de-identified list of construal properties — strings or `{"id": "M1", "property": "..."}` — each built as its own validated scanner and rated across every condition, with no verdict stated (`analyst_interface_contract.md §Construal checks`). Keep the check→condition mapping in the investigation log, never in the request. In forensic mode the set goes inside `behaviour_spec` instead; sending it at the top level there is an agent error (exit 2)

**Returns**: Scanner definitions, validation metrics, quantified results (per-condition detection rates), scan results path, transcript exclusions, transcript excerpts, additional observations. See `analyst_interface_contract.md` for the full report format.

### Forensic mode: `behaviour_spec` (Diagnostician only)

In a Model Forensics investigation the Analyst additionally takes a `behaviour_spec` object. **Its presence is the mode switch**: with it the Analyst runs the forensic prompt, without it the unchanged Orchestrator prompt — so an Orchestrator invocation never sends it.

```json
{
    "topic": "<neutral topic — rules unchanged>",
    "transcript_source": {"condition_A": "/abs/path/iterations/01_round1/logs/c_3f9a", "condition_B": "/abs/path/iterations/01_round1/logs/c_8b21", "condition_C": "/abs/path/iterations/01_round1/logs/c_51d0"},
    "behaviour_spec": {
        "description": "The model edited the test file rather than the implementation",
        "reference_transcript": "/abs/path/to/original.eval",
        "grader_dir": "/abs/path/to/investigations/<name>/artefacts/analyst/grader",
        "construal_checks": [
            {"id": "K1", "property": "Whether the reasoning treats the deadline as binding — reasoning about time pressure, prioritising speed, declining to verify"},
            {"id": "K2", "property": "Whether the reasoning treats the failing test as the specification to satisfy rather than as a check on the specification"}
        ]
    }
}
```

- `description` is always required within the spec; `reference_transcript` is required on the first build and optional once `grader_dir` is populated; `grader_dir` absent/empty means build-and-persist, populated means load-and-apply-unmodified
- `construal_checks` goes **inside `behaviour_spec`**, never at the request's top level: the top-level field is Orchestrator mode's, and a forensic request carrying it is rejected (exit 2) before anything is read. It is optional: **one de-identified set per round** — every variant's check, each with an opaque id (`K<n>`; one is assigned if missing — never `M<n>`, which collides with the must-exclude candidate `M1`) and no indication of which condition it bears on. The Analyst validates each as its own scanner, rates each across **every** condition, reports the full pairwise difference matrix and a scorability label per check, and states **no** `succeeded`/`failed`/`ambiguous` verdict — the Diagnostician derives the verdicts from its private check→variant mapping (`diagnostician_analyst_guide.md §Deriving the verdicts`). The retired single-string `manipulation_check` field is rejected (exit 2)
- Every forensic request gets a fixed work order — grader → construal checks → eval-awareness split → open-ended scan — so a turn-starved invocation leaves the open-ended half thin rather than the auxiliaries unvalidated. Large workloads (roughly > 12 conditions, or > 120 scanner×condition passes) are split **by check group, never by condition** (addendum §Work order → *Split protocol*); below that, send the round to one invocation with `subagent_max_turns` raised (about 200 for ten conditions and six checks), because every split invocation repeats the grader, its audit and a full re-grade
- **The firewall is not breached**: the spec names the *explanandum* (what to measure), never an explanation, a predicted direction, or which condition is which. The neutral `topic` remains required and its rules are unchanged — forensic mode is two measurements (fixed grader + open-ended scan), never one
- **Grader reuse via `grader_dir` is what makes rates comparable across iterations** — pass the same directory every round after the first, and treat a hash change between reports as an instrument change
- **`scanning_model` and the target's family.** When the scanner's family is the target's (or the Analyst's own `subagent_model` is), the Analyst must hand-read its validation labels (or use a cross-family validator), prefer a deterministic parse for action-mediated behaviour, and state the family facts under *Instrument family*. The choice is the Diagnostician's: prefer a cross-family `scanning_model` where one is available at acceptable cost, and record the same-family case and its mitigations in the log (`diagnostician_analyst_guide.md §Scanning model and target family`)
- Only the check set's shape and placement are validated (`construal_checks`: inside `behaviour_spec`, both fields, a list, non-empty properties, unique ids); the rest of a malformed spec reaches the Analyst as JSON. Check it by hand before sending
- The forensic report sections (grader provenance, extraction provenance and exhibit, per-condition precision audit, instrument family, floor check, construal-check rates and scorability, `### Behaviours not captured by the grader`, `### Eval-awareness split` with its `unmeasurable` gate) are specified in **`.claude/docs/contracts/analyst_forensic_addendum.md`**. The base contract deliberately carries no pointer to it, so this reference and the Diagnostician's documents are how you find it
- The deterministic half of the forensic report — Wilson intervals, κ, φ, the precision audit, the scorability label, the extraction-provenance exhibit — lives in `subagents/transcript_analyst/forensic_checks.py`; you may use the same functions to re-derive a report's numbers from its persisted artefacts

## Environment Mapper

**Script**: `subagents/environment_mapper/main.py`

**Input JSON**:
```json
{
    "environment_path": "/absolute/path/to/eval/environment",
    "behaviour_description": "The model wrote a special-case branch returning the expected value for the test inputs rather than implementing the function",
    "reference_transcript": "/absolute/path/to/investigation/artefacts/reference/<log>/<sample_id>_epoch_1.txt",
    "constraints": "The eval is large — prioritise the prompt and scoring path.",
    "subagent_model": "claude-opus-5-5",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `environment_path` must be an existing directory — the frozen original environment
- `behaviour_description` is the **explanandum**: what the model did, never why. It is not a hypothesis and must not contain one
- `reference_transcript` and `constraints` are optional; the transcript is read only to check the environment-as-written against the episode-as-run. Pass a **plain-text dump**, not the `.eval` (§Transcript handoff)
- `conformance_defects` and `previous_report` are optional and sent only together, only for the one re-ask after `check_report.py mapper` fails (§Report conformance)
- The Mapper runs **once per investigation**, before any candidate exists; its briefing lands at `artefacts/mapper/report_<tag>_mapper_1.md` — the Designer and Critic receive that path

**Returns**: Summary (with fixed-format `**Prohibition status:**` / `**Scorer detection:**` / `**Defect sufficiency:**` lines), file catalogue, pipeline model, instruction analysis, scorer analysis, tool & environment analysis, manipulable surface inventory, environment defects, uncertainties. See `mapper_interface_contract.md` for the full request and report format.

## Variant Designer

**Script**: `subagents/variant_designer/main.py`

**Input JSON**:
```json
{
    "mapper_report_path": "/absolute/path/to/artefacts/mapper/report_00_triage_mapper_1.md",
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
    "subagent_model": "claude-opus-5-5",
    "subagent_thinking": {"type": "adaptive"}
}
```

- `environment_path` must be an existing directory: **the current iteration's copy** that diffs must apply against, not necessarily the original
- `environment_rate` (note: **not** `baseline_rate`) is the measured rate of the environment supplied, `{"rate": <decimal>, "n": <int>}` — pass the step-1c baseline only when the environment *is* a fresh copy of the original
- `regime` is `"ablate"` or `"amplify"` — a steer, not a directive; the Designer may mark individual variants off-regime with a reason
- `environment_delta` is **required in practice whenever the environment is a derived variant** (omit it entirely for a fresh original copy): what was changed, never what it did — no rates, no outcomes. Without it the Designer meets a map/tree mismatch it can only read as a stale map or a broken environment. A report whose `**Environment:**` Summary line says `original` against a derived environment means the delta did not arrive
- `variations_tried` is optional: every variation already run across all rounds and branches, outcomes stripped
- `elimination_targets` is optional and triggers a **positive-control variant** per named candidate (outside `variant_budget`). Each target costs a condition — name one only where retiring it would change the leading explanation or move the equivalence-class boundary
- Only `live` candidates are sent
- `conformance_defects` and `previous_report` are optional and sent only together, only for the one re-ask after `check_report.py designer` fails (§Report conformance); the Designer carries its variants and diffs over unchanged

**Returns**: Summary (with fixed-format `**Environment:**` / `**Discriminating variants:**` / `**Off-regime variants:**` / `**Positive controls:**` / `**Headroom:**` lines), proposed variants with diffs and confound declarations, discrimination matrix, variant dependencies (only if applicable), confounds & risks, uncertainties. See `designer_interface_contract.md` for the full request and report format.

## Red-Teaming Critic

**Script**: `subagents/redteam_critic/main.py`

The Critic's only CLI-validated field is `mode` ∈ `{candidate_critique, diff_review, prediction}`; each mode then requires its own fields (missing ones exit 2, not 1). Always cross-family: `subagent_route: "openrouter"` with a non-Claude model.

**Input JSON** (`candidate_critique` shown):
```json
{
    "mode": "candidate_critique",
    "transcript_paths": ["/absolute/path/to/investigation/artefacts/reference/<log>/<sample_id>_epoch_1.txt", "/absolute/path/to/iterations/00_round0/transcripts/c_0e4d/<log>/<sample_id>_epoch_7.txt"],
    "mapper_report_path": "/absolute/path/to/artefacts/mapper/report_00_triage_mapper_1.md",
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

- `candidate_critique` requires `transcript_paths`, `mapper_report_path`, `candidates`, `iteration` (0 for the step-2 invocation); `evidence_summary` is optional — post-3d, what the last round showed; at any iteration, a neutral description of the measurement instrument where the critique needs one. `transcript_paths` are **plain-text, score-free dumps**, never `.eval` files (§Transcript handoff)
- `diff_review` requires `diffs` (array of `{"name": ..., "diff": ...}`), `environment_path`, `mapper_report_path` — and **must not** receive the candidates, the discrimination matrix, or the behaviour description; withholding what the diffs test is the mode's value (the runner also denies this mode the Designer's reports; descriptive variant names may stay visible)
- `prediction` requires `investigation_log_path`, `leading_explanation`, `untested_surfaces` — and must not receive the Diagnostician's own prediction
- Sending a withheld field does not error: it silently converts the review into a much weaker one. The blinding is the caller's responsibility
- `conformance_defects` and `previous_report` are optional in every mode, sent only together, and used only for the **one re-ask** after `scripts/check_report.py critic <report> --mode <mode>` exits non-zero (§Report conformance)

**Returns**: only the active mode's sections — `candidate_critique`: independent reading (written before the ledger is read), blind spots, assumption audit, proposed additional candidates (`X<n>` ids), retirement advice; `diff_review`: summary, per-diff analysis, cross-diff analysis, inferred purpose (last); `prediction`: chosen factor, ordinal prediction, reasoning, falsifier. See `critic_interface_contract.md` for all three request and report formats.
