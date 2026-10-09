"""Environment Mapper agent — prompt building and config."""

from typing import Any

from subagents.environment_mapper.system_prompt import SYSTEM_PROMPT
from subagents.runner import contract_section, reask_section, run_agent

ALLOWED_TOOLS = ["Read", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Edit", "Bash", "WebSearch", "WebFetch"]

# Files this agent must not read. Mapping of filename pattern → reason.
# The Mapper must read its own contract (mapper_interface_contract.md); it is
# intentionally absent from this list. It is kept away from the Diagnostician's
# methodological references for the same reason the Explorer is kept away from
# the Orchestrator's: a descriptive agent that has absorbed the investigator's
# framework starts producing explanations instead of descriptions.
RESTRICTED_FILES: dict[str, str] = {
    # Directory-level denial: everything under investigator/ is lead-agent
    # material, so documents added there later are blinded automatically
    # rather than escaping a list of filenames.
    ".claude/docs/investigator/": (
        "Diagnostician-only material: methodological principles, pipeline "
        "responsibilities, delegation guides. The Mapper must describe the "
        "environment without reasoning about explanations."
    ),
    ".claude/skills/": (
        "Skill definitions and their methodology documents carry the "
        "Diagnostician's candidate-explanation framework."
    ),
    # The analyst contract and its forensic addendum travel together: both
    # describe the blinded instrument's interface, and blocking one while the
    # other stays readable defeats the entry's purpose.
    "analyst_interface_contract.md": (
        "Analyst-Diagnostician interface contract. Not relevant to this agent."
    ),
    "analyst_forensic_addendum.md": (
        "Forensic addendum to the analyst contract. Not relevant to this agent."
    ),
    # Investigation runtime state. These are created per investigation rather
    # than installed, so no directory-level denial covers them — and they
    # carry exactly what the blinding withholds: the candidate ledger, the
    # iteration history with rates, and the synthesis.
    "CANDIDATES.md": "Live candidate-explanation ledger. The Mapper is hypothesis-free.",
    "INVESTIGATION-LOG.md": "Investigation history: candidates, predictions, rates.",
    "FINDINGS-REPORT.md": "Investigation synthesis: explanations and evidence.",
}


async def run_environment_mapper(cwd: str | None = None, **data: Any) -> str:
    """Run the environment mapper agent and return its markdown briefing.

    Parameters
    ----------
    cwd : str | None
        Working directory for the agent. Defaults to the project root.
    **data : Any
        Fields from the invocation JSON. Required: ``environment_path``,
        ``behaviour_description``. Optional: ``reference_transcript``,
        ``constraints``, the re-ask pair ``conformance_defects`` +
        ``previous_report`` (see `runner.reask_section`), and the shared
        ``subagent_model`` / ``subagent_thinking`` / ``subagent_route``
        infrastructure fields.

    Returns
    -------
    str
        The Mapper's markdown briefing, per `mapper_interface_contract.md`.
    """
    # The behaviour description leads the prompt because it conditions two of
    # the report's sections (scorer analysis, and the provocation note on each
    # environment defect). It is the explanandum, not a hypothesis; the system
    # prompt's Cognitive Boundaries section is what keeps it from being read
    # as one.
    prompt = f"""\
## Behaviour Under Investigation

{data["behaviour_description"]}

## Environment Path

{data["environment_path"]}
"""

    reference_transcript = data.get("reference_transcript")
    if reference_transcript:
        prompt += f"\n## Reference Transcript\n\n{reference_transcript}\n"

    constraints = data.get("constraints")
    if constraints:
        prompt += f"\n## Constraints\n\n{constraints}\n"

    # The one re-ask after a failed conformance check (validated; malformed
    # raises, so the CLI exits 2 rather than silently re-running from scratch).
    reask = reask_section(
        "Environment mapper",
        data.get("conformance_defects"),
        data.get("previous_report"),
        "Re-read the environment only for such a section.",
    )
    if reask:
        prompt += f"\n{reask}\n"

    # Absolute contract path first, so the read-first instruction resolves from
    # any working directory (see runner.contract_section).
    prompt = contract_section(".claude/docs/contracts/mapper_interface_contract.md") + "\n\n" + prompt

    # Memory is disabled: each mapping is self-contained, per the contract.
    # This also keeps the agent fully read-only (runner.py only injects Write
    # into allowed_tools when a memory_file is set).
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        agent_name="Environment mapper",
        cwd=cwd,
        restricted_files=RESTRICTED_FILES,
        memory_file=None,
        model=data.get("subagent_model"),
        thinking=data.get("subagent_thinking"),
        route=data.get("subagent_route"),
        max_turns=data.get("subagent_max_turns"),
    )
