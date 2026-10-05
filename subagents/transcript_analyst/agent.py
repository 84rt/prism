"""Transcript Analyst agent — prompt building and config."""

import json
from typing import Any

from subagents.runner import run_agent
from subagents.transcript_analyst.system_prompt import (
    CONSTRUAL_CHECKS_RULE,
    FORENSIC_WORK_ORDER,
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
    # Investigation runtime state. Created per investigation rather than
    # installed, so no directory-level denial covers them — and they carry
    # exactly what the analyst's blinding withholds: the hypothesis or
    # candidate ledger, the condition mapping, and prior findings.
    "CANDIDATES.md": (
        "Candidate-explanation ledger. The analyst must never learn any "
        "candidate explanation."
    ),
    "INVESTIGATION-LOG.md": (
        "Investigation history: the hypothesis, condition mappings, rates."
    ),
    "FINDINGS-REPORT.md": "Investigation synthesis: explanations and evidence.",
}


def normalise_construal_checks(spec: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Split a behaviour spec into the grader part and the construal-check set.

    The contract's default shape is `construal_checks`: a list of de-identified
    checks, each built as its own scanner and rated across every condition
    (combined-blind delivery, D30). The older single-string `manipulation_check`
    is accepted as a one-element set so requests written to the previous shape
    keep working; both together is an error rather than a guess.

    Parameters
    ----------
    spec : dict
        The request's `behaviour_spec`, as sent.

    Returns
    -------
    tuple[dict, list[dict]]
        `(grader_spec, checks)` — the spec with both check fields removed, and
        the checks as `[{"id": "M1", "property": "..."}, ...]` (possibly empty).

    Raises
    ------
    ValueError
        On both fields present, a non-list `construal_checks`, an entry with no
        `property`, or duplicate ids — each of which would otherwise reach the
        Analyst as an ambiguous request.

    Examples
    --------
    >>> normalise_construal_checks({"description": "d", "manipulation_check": "x"})[1]
    [{'id': 'M1', 'property': 'x'}]
    """
    grader_spec = {k: v for k, v in spec.items() if k not in ("construal_checks", "manipulation_check")}
    raw = spec.get("construal_checks")
    legacy = spec.get("manipulation_check")

    if raw is not None and legacy is not None:
        raise ValueError("behaviour_spec carries both construal_checks and manipulation_check; send one")
    if raw is None:
        raw = [legacy] if legacy else []
    if not isinstance(raw, list):
        raise ValueError("behaviour_spec.construal_checks must be a list")

    checks: list[dict[str, str]] = []
    for i, item in enumerate(raw, start=1):
        # A bare string is a property with an assigned id; a dict may carry its own.
        if isinstance(item, str):
            item = {"property": item}
        if not isinstance(item, dict) or not str(item.get("property", "")).strip():
            raise ValueError(f"construal check #{i} has no non-empty 'property'")
        checks.append({"id": str(item.get("id") or f"M{i}"), "property": str(item["property"]).strip()})

    ids = [c["id"] for c in checks]
    if len(set(ids)) != len(ids):
        raise ValueError(f"construal check ids are not unique: {ids}")
    return grader_spec, checks


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
        # `run_cli` maps RuntimeError → exit 2 (agent error); a bare ValueError
        # would escape as a traceback with exit 1 and be misread as an SDK crash.
        try:
            grader_spec, checks = normalise_construal_checks(data["behaviour_spec"])
        except ValueError as exc:
            raise RuntimeError(f"Transcript analyst: malformed behaviour_spec — {exc}") from exc
        sections.append(
            "## Behaviour Spec\n\n"
            f"```json\n{json.dumps(grader_spec, indent=2)}\n```"
        )
        # The construal checks are rendered as their own section, never inside
        # the spec, so there is exactly one place the analyst reads them from
        # and the no-verdict rule sits next to the set it applies to.
        if checks:
            sections.append(
                "## Construal Checks\n\n"
                f"```json\n{json.dumps(checks, indent=2)}\n```\n\n"
                f"{CONSTRUAL_CHECKS_RULE}"
            )
        # The work order is restated per request, not only in the system
        # prompt: an explicit request-level ordering is what recovered the
        # runs whose Analyst spent its turns on open-ended scanners first.
        sections.append(f"## Work Order\n\n{FORENSIC_WORK_ORDER}")

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
