"""Red-Teaming Critic agent — prompt building and config."""

import json
import os
from typing import Any

from subagents.redteam_critic.system_prompt import SYSTEM_PROMPT
from subagents.runner import reask_section, run_agent

ALLOWED_TOOLS = ["Read", "Glob", "Grep"]
DISALLOWED_TOOLS = ["Edit", "Write", "Bash", "WebSearch", "WebFetch"]

VALID_MODES = ("candidate_critique", "diff_review", "prediction")

# The contract's absolute path, resolved from the scaffold root rather than
# the working directory. In an installed target this module is reached
# through a gitignored symlink; `realpath` follows it back to the scaffold
# checkout, where the contract always exists, so the path injected into the
# prompt is valid whatever `--cwd` the Critic was launched with. A
# cwd-relative path failed twice in one run (the agent searched for it under
# the investigation directory, and searches skip gitignored symlinks).
# The other five sub-agents get the same injection via
# `runner.contract_section`; the Critic keeps its own constant because its
# tests and its mode-specific section ordering reference it directly.
_SCAFFOLD_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)
CONTRACT_PATH = os.path.join(
    _SCAFFOLD_ROOT, ".claude", "docs", "contracts", "critic_interface_contract.md"
)

# The report headings of each mode, verbatim and in order, as fixed by
# `critic_interface_contract.md §Report Format`. Rendered into every request
# as a checklist so a non-Claude model has the exact strings in front of it,
# and duplicated in `scripts/check_report.py`, which verifies the saved
# report (a test asserts the two copies agree).
REPORT_HEADINGS: dict[str, tuple[str, ...]] = {
    "candidate_critique": (
        "Independent Reading",
        "Blind Spots",
        "Assumption Audit",
        "Proposed Additional Candidates",
        "Candidates I Would Retire",
    ),
    "diff_review": (
        "Summary",
        "Per-Diff Analysis",
        "Cross-Diff Analysis",
        "Inferred Purpose",
    ),
    "prediction": (
        "Chosen Factor",
        "Prediction",
        "Reasoning",
        "What Would Falsify This",
    ),
}

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
        If ``mode`` is not one of ``VALID_MODES``, if a field required by
        the active mode is absent, or if the contract file is not where the
        scaffold root says it is. Mode-conditional requirements cannot be
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

    # A Critic that cannot find its contract fails as a gate, not as a
    # degradation, so a missing contract is an agent error before any spend.
    if not os.path.isfile(CONTRACT_PATH):
        raise RuntimeError(f"Critic contract not found at {CONTRACT_PATH}")

    # The active mode is stated first so the agent can select the right
    # section of its system prompt before reading anything else; the
    # contract's absolute path follows so the read-first instruction in the
    # system prompt has a path that resolves from any working directory.
    sections = [
        f"## Active Mode\n\n`{mode}`",
        "## Contract\n\n"
        f"Read `{CONTRACT_PATH}` with a direct Read of that exact absolute path "
        "before doing anything else. Do not search for it and do not read a "
        "relative path; the file is where this path says it is.",
    ]

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
        # Re-proposals are merged by signature on receipt, so they cost the
        # Critic a proposal slot and contribute nothing; said at the point of
        # use, next to the ledger, rather than only in the contract.
        sections.append(
            "## Before Proposing\n\n"
            "Every candidate above is already in the ledger. Do not re-propose one "
            "of them under a new id, and do not propose a candidate whose "
            "discriminating signature is the same as an existing one — the "
            "Diagnostician merges proposals by signature on receipt, so such a "
            "proposal is recorded as a duplicate and counts as no contribution. "
            "If an existing candidate is mis-stated, or should be split or merged, "
            "say so under `### Candidates I Would Retire`."
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

    # The verbatim-heading checklist, mode-specific, at the end where it is
    # closest to the act of writing. The saved report is checked against
    # exactly these strings by `scripts/check_report.py`.
    sections.append(
        "## Report Headings\n\n"
        "Your report consists of exactly these level-3 markdown headings, in "
        "this order, with this exact text (nothing before the first, nothing "
        "after the last section, no other headings at level 3 or shallower):\n\n"
        + "\n".join(f"- `### {h}`" for h in REPORT_HEADINGS[mode])
        + "\n\nThese headings are checked mechanically. A report with a "
        "substituted, merged, or reworded heading is sent back once with the "
        "defects listed, and a second failure is recorded against the Critic."
    )

    # A one-time re-ask carries the previous attempt's defects verbatim and
    # points at that report, which is restructured rather than rewritten
    # (validated in `reask_section`; malformed raises → exit 2).
    reask = reask_section(
        "Red-teaming critic",
        data.get("conformance_defects"),
        data.get("previous_report"),
        "A proposal the defects name as lacking a discriminating signature gets "
        "one now, written as its falsifier; nothing else is re-reasoned.",
    )
    if reask:
        sections.append(reask)

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
        :func:`_build_prompt`. Optional in every mode, and only together: the
        re-ask pair ``conformance_defects`` (the defect lines printed by
        ``scripts/check_report.py``) + ``previous_report`` (the failed
        report's absolute path), which turns the invocation into the
        contract's one re-ask (see `runner.reask_section`). The shared ``subagent_model`` /
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
        max_turns=data.get("subagent_max_turns"),
    )
