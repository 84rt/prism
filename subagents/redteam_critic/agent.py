"""Red-Teaming Critic agent — prompt building and config."""

import json
from typing import Any

from subagents.redteam_critic.system_prompt import SYSTEM_PROMPT
from subagents.runner import run_agent

ALLOWED_TOOLS = ["Read", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Edit", "Write", "Bash", "WebSearch", "WebFetch"]

VALID_MODES = ("candidate_critique", "diff_review", "prediction")

# Fields each mode requires beyond `mode` itself. Mode-conditional
# requirements cannot be expressed in AgentCLIConfig.required_fields, which
# validates one flat list for every invocation, so they are enforced in
# _build_prompt instead — see its Raises section.
MODE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "candidate_critique": (
        "transcript_paths",
        "mapper_report_path",
        "candidates",
        "iteration",
    ),
    "diff_review": ("diffs", "environment_path", "mapper_report_path"),
    "prediction": (
        "investigation_log_path",
        "leading_explanation",
        "untested_surfaces",
    ),
}

# Files this agent must not read. Mapping of filename pattern → reason.
# The Critic reads its own contract; it is intentionally absent from this list.
# The blinding here is mode-independent and deliberately conservative: the
# Critic's value comes from reasoning that is decorrelated from the
# Diagnostician's, and the Diagnostician's methodological references are
# precisely the shared priors we are trying not to install.
#
# Known maintenance risk, flagged rather than solved: blocking the principles
# doc also blocks the Critic from the admissibility bar it is held to (every
# proposed candidate needs a discriminating signature). That requirement is
# therefore restated in this agent's own system prompt and in
# critic_interface_contract.md, and the two could drift apart from the
# principles doc over time. The fix is to keep them in sync, never to relax
# the blinding — the blinding is what the mode is for.
RESTRICTED_FILES: dict[str, str] = {
    # Directory-level denial: everything under investigator/ is lead-agent
    # material, so documents added there later are blinded automatically
    # rather than escaping a list of filenames.
    ".claude/docs/investigator/": (
        "Diagnostician-only material: methodological principles, pipeline "
        "responsibilities, delegation guides. The Critic's independence "
        "depends on not sharing the Diagnostician's analytical framework — "
        "a critic reasoning from the same priors inherits the same blind "
        "spots, which is precisely what it exists to counter."
    ),
    ".claude/skills/": (
        "Skill definitions and their methodology documents carry the "
        "Diagnostician's candidate framework."
    ),
    # Investigation runtime state — with one deliberate exception. The ledger
    # arrives via the request in candidate_critique and must stay unseen in
    # diff_review, so the file itself is denied in every mode. The
    # investigation log is NOT listed: prediction mode receives
    # `investigation_log_path` and must read it, and this list is static
    # across modes — in the other two modes the Diagnostician simply never
    # sends the path.
    "CANDIDATES.md": (
        "Candidate ledger file. candidate_critique receives the ledger via "
        "its request; diff_review must not see it at all."
    ),
    "FINDINGS-REPORT.md": "Investigation synthesis: explanations and evidence.",
}


def _build_prompt(data: dict[str, Any]) -> str:
    """Render the mode-specific user prompt for the Critic.

    Parameters
    ----------
    data : dict[str, Any]
        The invocation JSON, already validated for the presence of ``mode``.

    Returns
    -------
    str
        The markdown prompt for this invocation.

    Raises
    ------
    RuntimeError
        If ``mode`` is not one of ``VALID_MODES``, or if a field required by
        the active mode is absent. Mode-conditional requirements cannot be
        expressed in ``AgentCLIConfig.required_fields``, so they are checked
        here and surfaced as an agent error (exit code 2) by ``run_cli``.
    """
    mode = data["mode"]
    if mode not in VALID_MODES:
        raise RuntimeError(
            f"Unknown critic mode: {mode!r} (expected one of {', '.join(VALID_MODES)})"
        )

    for field in MODE_REQUIRED_FIELDS[mode]:
        if field not in data:
            raise RuntimeError(f"mode {mode!r} requires field: {field}")

    # The active mode is stated first so the agent can select the right
    # section of its system prompt before reading anything else.
    sections = [f"## Active Mode\n\n`{mode}`"]

    if mode == "candidate_critique":
        # Ordering is load-bearing, not cosmetic: the transcripts are listed
        # before the candidate ledger and carry the read-first instruction
        # inline, so the sequencing discipline is visible at the point of use
        # rather than only in the contract.
        sections.append(
            "## Transcripts To Read First\n\n"
            + "\n".join(f"- `{p}`" for p in data["transcript_paths"])
            + "\n\nRead these and write your `### Independent Reading` section "
            "before reading the candidate list below."
        )
        sections.append(f"## Mapper Briefing\n\nRead: `{data['mapper_report_path']}`")
        sections.append(f"## Iteration\n\n{data['iteration']}")
        sections.append(
            "## Candidate Explanations\n\n"
            f"```json\n{json.dumps(data['candidates'], indent=2)}\n```"
        )
        if data.get("evidence_summary"):
            sections.append(f"## Evidence So Far\n\n{data['evidence_summary']}")

    elif mode == "diff_review":
        # No behaviour description, no candidates, no discrimination matrix:
        # this mode's blinding is enforced by what the prompt omits.
        sections.append(f"## Environment Path\n\n{data['environment_path']}")
        sections.append(f"## Mapper Briefing\n\nRead: `{data['mapper_report_path']}`")
        sections.append(
            "## Diffs Under Review\n\n"
            f"```json\n{json.dumps(data['diffs'], indent=2)}\n```"
        )

    else:  # prediction
        sections.append(
            f"## Investigation Log\n\nRead: `{data['investigation_log_path']}`"
        )
        sections.append(
            "## Leading Explanation\n\n"
            f"```json\n{json.dumps(data['leading_explanation'], indent=2)}\n```"
        )
        sections.append(
            "## Untested Surfaces\n\n"
            + "\n".join(f"- {s}" for s in data["untested_surfaces"])
        )

    return "\n\n".join(sections) + "\n"


async def run_redteam_critic(cwd: str | None = None, **data: Any) -> str:
    """Run the red-teaming critic agent and return its markdown report.

    Parameters
    ----------
    cwd : str | None
        Working directory for the agent. Defaults to the project root.
    **data : Any
        Fields from the invocation JSON. Always required: ``mode``.
        Mode-conditional requirements are documented in
        `.claude/docs/contracts/critic_interface_contract.md` and enforced in
        :func:`_build_prompt`. The shared ``subagent_model`` /
        ``subagent_thinking`` / ``subagent_route`` infrastructure fields apply
        as they do to every sub-agent; the Critic is routed cross-family by
        default, so callers pass ``subagent_route="openrouter"`` and a
        non-Claude ``subagent_model``.

    Returns
    -------
    str
        The Critic's markdown report for the active mode.
    """
    # Memory is disabled: each critique is self-contained, per the contract.
    # Continuity reaches the Critic only through the artefacts it is given —
    # the ledger, the evidence summary, the log — which is what keeps its
    # reading of each round from being anchored on its own previous one. This
    # also keeps the agent fully read-only (runner.py only injects Write into
    # allowed_tools when a memory_file is set).
    return await run_agent(
        prompt=_build_prompt(data),
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        agent_name="Red-teaming critic",
        cwd=cwd,
        restricted_files=RESTRICTED_FILES,
        memory_file=None,
        model=data.get("subagent_model"),
        thinking=data.get("subagent_thinking"),
        route=data.get("subagent_route"),
    )
