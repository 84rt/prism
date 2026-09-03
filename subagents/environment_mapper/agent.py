"""Environment Mapper agent — prompt building and config."""

from typing import Any

from subagents.environment_mapper.system_prompt import SYSTEM_PROMPT
from subagents.runner import run_agent

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
    "analyst_interface_contract.md": (
        "Analyst-Diagnostician interface contract. Not relevant to this agent."
    ),
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
        ``constraints``, and the shared ``subagent_model`` /
        ``subagent_thinking`` / ``subagent_route`` infrastructure fields.

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
    )
