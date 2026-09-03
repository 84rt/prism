"""Transcript Analyst agent — prompt building and config."""

import json
from typing import Any

from subagents.runner import run_agent
from subagents.transcript_analyst.system_prompt import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_FORENSIC,
)

ALLOWED_TOOLS = ["Bash", "Read", "Write", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Edit", "WebSearch", "WebFetch"]

# Files this agent must not read. Mapping of filename pattern → reason.
RESTRICTED_FILES: dict[str, str] = {
    # Directory-level denial. Everything under investigator/ belongs to the
    # lead agent — methodological principles, pipeline responsibilities,
    # delegation guides, the invocation reference. Denying the directory
    # rather than listing filenames means a document added there later is
    # blinded automatically; a list of filenames is silently escaped by
    # every new file until someone remembers to add it.
    ".claude/docs/investigator/": (
        "Lead-investigator material: methodological principles, delegation "
        "logic, and hypothesis-informing guidance. The analyst must not "
        "access these — its blinding is the scaffold's central integrity "
        "guarantee, and reading them would supply exactly the expectations "
        "the blinding exists to withhold."
    ),
    ".claude/skills/": (
        "Skill definitions and their methodology documents belong to the "
        "lead investigator and carry its hypotheses and candidate framework."
    ),
}


async def run_transcript_analyst(cwd: str | None = None, **data: Any) -> str:
    """Run the transcript analyst agent and return its analysis report."""
    sections = [
        f"## Neutral Topic Description\n\n{data['topic']}",
        f"## Transcript Source\n\n```json\n{json.dumps(data['transcript_source'], indent=2)}\n```",
    ]

    if data.get("scanning_model"):
        sections.append(f"## Scanning Model\n\n{data['scanning_model']}")

    if data.get("constraints"):
        sections.append(
            f"## Constraints\n\n```json\n{json.dumps(data['constraints'], indent=2)}\n```"
        )

    # Model Forensics mode: a fixed grader for a named behaviour, applied
    # across all conditions. Additive and optional — an invocation without
    # this field behaves exactly as before, which is what keeps the
    # Orchestrator's pipeline unchanged.
    if data.get("behaviour_spec"):
        sections.append(
            "## Behaviour Spec\n\n"
            f"```json\n{json.dumps(data['behaviour_spec'], indent=2)}\n```"
        )

    if data.get("artefacts_dir"):
        sections.append(f"## Artefacts Directory\n\n{data['artefacts_dir']}")

    prompt = "\n\n".join(sections) + "\n"

    # Model Forensics investigations get an extended system prompt. The two
    # variants are selected here rather than branched inside a single prompt so
    # that an Orchestrator-mode analyst never receives the forensic text at
    # all — it cannot mistakenly adopt a mode it has not been told exists. The
    # forensic variant is the base prompt plus an overlay, so the shared
    # instructions have exactly one definition and the modes cannot drift.
    system_prompt = SYSTEM_PROMPT_FORENSIC if data.get("behaviour_spec") else SYSTEM_PROMPT

    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        agent_name="Transcript analyst",
        cwd=cwd,
        restricted_files=RESTRICTED_FILES,
        # The Transcript Analyst is deliberately stateless across invocations:
        # no memory file, so no findings can leak between investigations and
        # the hypothesis firewall holds by construction. Generalisable Scout
        # craft lives in `.claude/docs/references/scout_reference.md` instead. (The memory
        # mechanism in runner.py remains available for other sub-agents.)
        memory_file=None,
        restrict_writes_to_memory=False,
        model=data.get("subagent_model"),
        thinking=data.get("subagent_thinking"),
        route=data.get("subagent_route"),
    )
