"""Experiment Executor agent — prompt building and config."""

import json
from typing import Any

from subagents.experiment_executor.system_prompt import SYSTEM_PROMPT
from subagents.runner import run_agent

ALLOWED_TOOLS = ["Bash", "Read", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Edit", "WebSearch", "WebFetch"]

# Files this agent must not read. Mapping of filename pattern → reason.
# The Executor must read its own contract (executor_interface_contract.md);
# it is intentionally absent from this list.
RESTRICTED_FILES: dict[str, str] = {
    # Directory-level denial: everything under investigator/ is lead-agent
    # material, and denying the directory means later additions are blinded
    # automatically rather than escaping a list of filenames.
    ".claude/docs/investigator/": (
        "Lead-investigator material: methodological references, pipeline "
        "responsibilities, delegation guides, invocation reference."
    ),
    ".claude/skills/": "Skill definitions belong to the lead investigator.",
    "analyst_interface_contract.md": (
        "Analyst-orchestrator interface contract. Not relevant to this agent."
    ),
}


async def run_experiment_executor(cwd: str | None = None, **data: Any) -> str:
    """Run the experiment executor agent and return its execution report."""
    models = data["models"]
    prompt = f"""\
## Experiment Name

{data["experiment_name"]}

## Parent Experiment Directory

{data["experiment_dir"]}

## Condition Specifications

```json
{json.dumps(data["conditions"], indent=2)}
```

## Model Specifications

{chr(10).join(f"- `{m}`" for m in models)}

## Model Routing

```json
{json.dumps(data.get("model_routing") or {}, indent=2)}
```

Render each model's `model_args` as `-M key=value` arguments on that model's \
invocations, using JSON for object values. Models absent from this mapping take \
no `-M` arguments. Never relax or substitute a pin to make a command succeed.

## Execution Parameter Overrides

```json
{json.dumps(data.get("overrides") or {}, indent=2)}
```
"""
    # Memory is disabled for the Executor: each invocation is self-contained,
    # per the interface contract. Within-investigation execution learnings
    # flow through the contract's `overrides` channel; cross-investigation
    # patterns are curated into the reference doc or system prompt.
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        agent_name="Experiment executor",
        cwd=cwd,
        restricted_files=RESTRICTED_FILES,
        memory_file=None,
        model=data.get("subagent_model"),
        thinking=data.get("subagent_thinking"),
        route=data.get("subagent_route"),
    )
