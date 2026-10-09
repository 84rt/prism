"""Experiment Executor agent — prompt building and config."""

import json
import os
from datetime import datetime, timezone
from typing import Any

from subagents.experiment_executor.system_prompt import SYSTEM_PROMPT
from subagents.runner import contract_section, run_agent

ALLOWED_TOOLS = ["Bash", "Read", "Glob", "Grep"]
# Write is disallowed outright, not merely left off ALLOWED_TOOLS: sub-agents
# run under bypassPermissions, so an unlisted tool is still offered, and the
# runner's write hook then blocks every call to it (no memory file). The
# Executor never needs to write a file — the prompt builder persists the
# request before the agent starts — so offering Write only invited blocked
# calls (an older prompt's "write an input JSON" wording produced exactly that).
DISALLOWED_TOOLS = ["Edit", "Write", "WebSearch", "WebFetch"]

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
    # The analyst contract and its forensic addendum travel together: both
    # describe the blinded instrument's interface, and blocking one while the
    # other stays readable defeats the entry's purpose.
    "analyst_interface_contract.md": (
        "Analyst-orchestrator interface contract. Not relevant to this agent."
    ),
    "analyst_forensic_addendum.md": (
        "Forensic addendum to the analyst contract. Not relevant to this agent."
    ),
    # Investigation runtime state. Created per investigation rather than
    # installed, so no directory-level denial covers them. The Executor is
    # mechanical and receives everything it needs in its request.
    "CANDIDATES.md": "Candidate-explanation ledger (Model Forensics mode).",
    "INVESTIGATION-LOG.md": "Investigation history: hypotheses, findings, rates.",
    "FINDINGS-REPORT.md": "Investigation synthesis: explanations and evidence.",
}


async def run_experiment_executor(cwd: str | None = None, **data: Any) -> str:
    """Run the experiment executor agent and return its execution report.

    Parameters
    ----------
    cwd : str | None
        Working directory for the agent. Defaults to the project root.
    **data : Any
        Fields from the invocation JSON (see `executor_interface_contract.md`).
        The re-ask pair ``conformance_defects`` / ``previous_report`` is
        rejected: the Executor is never re-asked.

    Returns
    -------
    str
        The Executor's markdown report.

    Raises
    ------
    RuntimeError
        If a re-ask field was sent (CLI exit 2).
    """
    # The Executor is never re-asked — a re-invocation would re-run the evals;
    # its report is regenerated with `execute_evals.py report`. Reject the
    # re-ask pair rather than silently dropping it (the `**data` hazard).
    if data.get("conformance_defects") is not None or data.get("previous_report") is not None:
        raise RuntimeError(
            "Experiment executor: conformance_defects/previous_report are not accepted — "
            "regenerate the report with `scripts/execute_evals.py report <run_dir>` instead"
        )
    models = data["models"]

    # Persist the contract request as a file: the batch runner takes it
    # directly, so the agent never re-types a command or a JSON payload. Keys
    # the runner does not know (subagent_*) are ignored by it.
    request_path = _persist_request(data)

    prompt = f"""\
## Request File

`{request_path}`

The request below, saved verbatim. Pass this path to \
`scripts/execute_evals.py run`; do not rewrite it.

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
    # Absolute paths for the contract and the Inspect reference first, so the
    # read-first instruction resolves from any working directory.
    prompt = (
        contract_section(
            ".claude/docs/contracts/executor_interface_contract.md",
            ".claude/docs/references/inspect_reference.md",
        )
        + "\n\n"
        + prompt
    )

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
        max_turns=data.get("subagent_max_turns"),
    )


def _persist_request(data: dict[str, Any]) -> str:
    """Write the request JSON under the experiment's executor artefacts.

    Parameters
    ----------
    data : dict
        The request as received by the CLI (minus `cwd`).

    Returns
    -------
    str
        Absolute path of the written file, e.g.
        ``<experiment_dir>/artefacts/executor/request_20260917T120000Z.json``.
    """
    out_dir = os.path.join(os.path.abspath(data["experiment_dir"]), "artefacts", "executor")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(out_dir, f"request_{stamp}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path
