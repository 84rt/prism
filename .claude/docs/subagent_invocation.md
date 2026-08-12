# Sub-Agent Invocation Reference

Sub-agents are invoked as CLI scripts via the Agent SDK. The orchestrator writes a JSON input file, runs the script via Bash, and captures the report from stdout.

## General Pattern

```bash
uv run --with claude-agent-sdk --with python-dotenv python subagents/<agent>/main.py <input.json> [--cwd <dir>]
```

- **Input**: JSON file with agent-specific fields (see below)
- **Output**: Structured markdown report printed to stdout
- **Errors**: Printed to stderr
- **Exit codes**: 0 = success, 1 = input validation error, 2 = agent error

The orchestrator should: (1) write the input JSON to a temp file, (2) run the command via Bash, (3) capture stdout as the report. All agents run fire-and-forget — no back-and-forth.

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
- `overrides` is optional; supported keys: `sample_limit`, `epochs`, `skip_preflight`, `max_parallel`, `max_connections`, `runs_per_condition`
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
