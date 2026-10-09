"""Environment Explorer agent — prompt building and config."""

from typing import Any

from subagents.environment_explorer.system_prompt import SYSTEM_PROMPT
from subagents.runner import contract_section, reask_section, run_agent

ALLOWED_TOOLS = ["Read", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Edit", "Bash", "WebSearch", "WebFetch"]

# Files this agent must not read. Mapping of filename pattern → reason.
# The Explorer must read its own contract (explorer_interface_contract.md);
# it is intentionally absent from this list.
RESTRICTED_FILES: dict[str, str] = {
    # Directory-level denial: everything under investigator/ is lead-agent
    # material, and denying the directory means later additions are blinded
    # automatically rather than escaping a list of filenames.
    ".claude/docs/investigator/": (
        "Orchestrator-only material: methodological references, pipeline "
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
    # installed, so no directory-level denial covers them. The Explorer
    # receives the current hypothesis via its request; the log and ledger
    # carry the wider history it must not absorb.
    "CANDIDATES.md": "Candidate-explanation ledger (Model Forensics mode).",
    "INVESTIGATION-LOG.md": "Investigation history: hypotheses, findings, rates.",
    "FINDINGS-REPORT.md": "Investigation synthesis: explanations and evidence.",
}


async def run_environment_explorer(cwd: str | None = None, **data: Any) -> str:
    """Run the environment explorer agent and return its markdown report.

    Parameters
    ----------
    cwd : str | None
        Working directory for the agent. Defaults to the project root.
    **data : Any
        Fields from the invocation JSON. Required: ``experiment_description``,
        ``hypothesis``, ``environment_path``. Optional: ``constraints``; the
        re-ask pair ``conformance_defects`` + ``previous_report`` (see
        `runner.reask_section`); and the shared ``subagent_*`` fields.

    Returns
    -------
    str
        The Explorer's markdown report, per `explorer_interface_contract.md`.
    """
    prompt = f"""\
## Experiment Description

{data["experiment_description"]}

## Hypothesis

{data["hypothesis"]}

## Environment Path

{data["environment_path"]}
"""
    constraints = data.get("constraints")
    if constraints:
        prompt += f"\n## Constraints\n\n{constraints}\n"

    # The one re-ask after a failed conformance check (validated; malformed
    # raises, so the CLI exits 2 rather than silently re-running from scratch).
    reask = reask_section(
        "Environment explorer",
        data.get("conformance_defects"),
        data.get("previous_report"),
        "Re-read the environment only for such a section.",
    )
    if reask:
        prompt += f"\n{reask}\n"

    # Absolute contract path first, so the read-first instruction resolves from
    # any working directory (see runner.contract_section).
    prompt = contract_section(".claude/docs/contracts/explorer_interface_contract.md") + "\n\n" + prompt

    # Memory is disabled for the Explorer: each invocation is self-contained,
    # per the interface contract. This also keeps the agent fully read-only
    # (runner.py only injects Write into allowed_tools when a memory_file is set).
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        agent_name="Environment explorer",
        cwd=cwd,
        restricted_files=RESTRICTED_FILES,
        memory_file=None,
        model=data.get("subagent_model"),
        thinking=data.get("subagent_thinking"),
        route=data.get("subagent_route"),
        max_turns=data.get("subagent_max_turns"),
    )
