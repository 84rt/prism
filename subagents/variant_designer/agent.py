"""Variant Designer agent — prompt building and config."""

import json
from typing import Any

from subagents.runner import contract_section, reask_section, run_agent
from subagents.variant_designer.system_prompt import SYSTEM_PROMPT

ALLOWED_TOOLS = ["Read", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Edit", "Bash", "WebSearch", "WebFetch"]

# Files this agent must not read. Mapping of filename pattern → reason.
# The Designer must read its own contract (designer_interface_contract.md) and
# the Mapper's briefing; both are intentionally absent from this list. It does
# receive the candidate explanations — it cannot separate what it cannot see —
# but not the framework for adjudicating between them, because an agent that
# has absorbed the elimination rules starts designing for the verdict it
# expects rather than for the comparison it was asked to make possible.
RESTRICTED_FILES: dict[str, str] = {
    # Directory-level denial: everything under investigator/ is lead-agent
    # material, so documents added there later are blinded automatically
    # rather than escaping a list of filenames.
    ".claude/docs/investigator/": (
        "Diagnostician-only material: methodological principles, pipeline "
        "responsibilities, delegation guides. The Designer separates "
        "candidates; it does not adjudicate between them."
    ),
    ".claude/skills/": (
        "Skill definitions and their methodology documents carry the "
        "Diagnostician's framework for ranking and retiring candidates."
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
    # than installed, so no directory-level denial covers them. The Designer's
    # request fields (environment_delta, variations_tried, environment_rate)
    # deliberately strip outcomes; these files carry the outcomes.
    "CANDIDATES.md": (
        "Full candidate ledger with history and evidence. The Designer "
        "receives live candidates via its request, never the ledger."
    ),
    "INVESTIGATION-LOG.md": "Iteration history with rates and prediction outcomes.",
    "FINDINGS-REPORT.md": "Investigation synthesis: explanations and evidence.",
    "executor/report.md": (
        "Past execution reports. The Designer receives no run history."
    ),
    "analyst/report.md": (
        "Past measurement reports with rates. A Designer that has seen which "
        "edits moved rates designs toward repeating them."
    ),
}


async def run_variant_designer(cwd: str | None = None, **data: Any) -> str:
    """Run the variant designer agent and return its markdown design report.

    Parameters
    ----------
    cwd : str | None
        Working directory for the agent. Defaults to the project root.
    **data : Any
        Fields from the invocation JSON. Required: ``mapper_report_path``,
        ``environment_path``, ``candidates``, ``regime``, ``environment_rate``,
        ``variant_budget``. Optional: ``environment_delta``,
        ``variations_tried``, ``elimination_targets``, ``constraints``, the
        re-ask pair ``conformance_defects`` + ``previous_report`` (see
        `runner.reask_section`), and the shared ``subagent_model`` /
        ``subagent_thinking`` / ``subagent_route`` infrastructure fields.

    Returns
    -------
    str
        The Designer's markdown report, per `designer_interface_contract.md`.
    """
    # The Mapper's briefing is passed by path rather than inlined so the
    # Designer reads the full report — the surface inventory's coupling column
    # and the pipeline model are both load-bearing and both easy to lose in a
    # summary.
    prompt = f"""\
## Environment Path

{data["environment_path"]}

## Mapper Briefing

Read the Mapper's briefing in full before designing anything: \
`{data["mapper_report_path"]}`
"""

    # Present only when the supplied environment is a derived variant rather
    # than a fresh copy of the original. Without it the Designer meets a
    # mismatch between the Mapper's map and the tree it is holding, and the
    # readings available to it — stale map, broken environment — would have it
    # report the investigation's own manipulation as an environment defect.
    # Outcomes are deliberately absent: the changes are needed for coherence,
    # their effects would bias the Designer toward edits that already worked.
    environment_delta = data.get("environment_delta")
    if environment_delta:
        prompt += (
            "\n## Environment Delta\n\n"
            "The environment above is **not** a fresh copy of the original eval. "
            "The Mapper's briefing describes the original, so its file locations "
            "are against that tree, not this one. These modifications have "
            "already been applied here:\n\n"
            f"```json\n{json.dumps(environment_delta, indent=2)}\n```\n"
        )

    prompt += f"""
## Candidate Explanations

```json
{json.dumps(data["candidates"], indent=2)}
```
"""

    # Ground already covered, outcomes stripped for the same reason. Broader
    # than the delta: it includes variations from branches this environment
    # does not descend from.
    variations_tried = data.get("variations_tried")
    if variations_tried:
        prompt += (
            "\n## Variations Already Tried\n\n"
            "Across all rounds and branches of this investigation. Re-proposing "
            "one of these is permitted, but say why re-testing it in the current "
            "environment is discriminating:\n\n"
            f"```json\n{json.dumps(variations_tried, indent=2)}\n```\n"
        )

    prompt += f"""
## Regime

{data["regime"]}

## Environment Rate

The measured rate of the target behaviour in the environment supplied above.

```json
{json.dumps(data["environment_rate"], indent=2)}
```

## Variant Budget

{data["variant_budget"]}
"""

    # Present only when the Diagnostician intends to retire a candidate on
    # absence of evidence this round. It is what triggers the positive-control
    # obligation, so it must be rendered — an unrendered field would leave the
    # contract requiring controls the Designer was never told to build.
    elimination_targets = data.get("elimination_targets")
    if elimination_targets:
        prompt += (
            "\n## Elimination Targets\n\n"
            "The Diagnostician intends to retire these candidates on *absence* of "
            "evidence this round. Supply a positive-control variant for each, per "
            "the contract:\n\n"
            + "\n".join(f"- `{c}`" for c in elimination_targets)
            + "\n"
        )

    constraints = data.get("constraints")
    if constraints:
        prompt += f"\n## Constraints\n\n{constraints}\n"

    # The one re-ask after a failed conformance check (validated; malformed
    # raises, so the CLI exits 2 rather than silently re-running from scratch).
    # Re-designing would change the variants, so the diffs are carried over.
    reask = reask_section(
        "Variant designer",
        data.get("conformance_defects"),
        data.get("previous_report"),
        "Keep every proposed variant and its diff exactly as proposed; do not re-design.",
    )
    if reask:
        prompt += f"\n{reask}\n"

    # Absolute contract path first, so the read-first instruction resolves from
    # any working directory (see runner.contract_section).
    prompt = contract_section(".claude/docs/contracts/designer_interface_contract.md") + "\n\n" + prompt

    # Memory is disabled: each design round is self-contained, per the
    # contract. Continuity across rounds arrives through the candidate ledger
    # and the `constraints` field, both supplied by the Diagnostician. This
    # also keeps the agent fully read-only (runner.py only injects Write into
    # allowed_tools when a memory_file is set).
    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        agent_name="Variant designer",
        cwd=cwd,
        restricted_files=RESTRICTED_FILES,
        memory_file=None,
        model=data.get("subagent_model"),
        thinking=data.get("subagent_thinking"),
        route=data.get("subagent_route"),
        max_turns=data.get("subagent_max_turns"),
    )
