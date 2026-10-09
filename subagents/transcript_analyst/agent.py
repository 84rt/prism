"""Transcript Analyst agent — prompt building and config."""

import json
from typing import Any

from subagents.runner import contract_section, reask_section, run_agent
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

# The per-iteration condition label map (`CONDITION_LABEL_MAP_iter<N>.json`)
# holds opaque id → real condition name and the Analyst-time labels: it *is* the
# mapping the blinding withholds. One wildcard pattern, matched on the basename
# by the runner's read hook, covers every iteration's map, numbered or not,
# with no upper bound.
RESTRICTED_FILES["CONDITION_LABEL_MAP*.json"] = (
    "Private condition label map: opaque execution ids → real condition names "
    "and Analyst labels. The analyst must never learn which condition is which."
)


def normalise_construal_checks(spec: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Split a behaviour spec into the grader part and the construal-check set.

    The contract's only shape is `construal_checks`: a list of de-identified
    checks, each built as its own scanner and rated across every condition
    (combined-blind delivery, D30). The retired single-string
    `manipulation_check` field is rejected outright rather than ignored — an
    unread key would otherwise be dropped silently and the round would run
    with no check at all.

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
        On the retired `manipulation_check` field, a non-list
        `construal_checks`, an entry with no `property`, or duplicate ids —
        each of which would otherwise reach the Analyst as an ambiguous or
        silently degraded request.

    Examples
    --------
    >>> normalise_construal_checks({"description": "d", "construal_checks": ["x"]})[1]
    [{'id': 'M1', 'property': 'x'}]
    """
    if "manipulation_check" in spec:
        raise ValueError(
            "behaviour_spec.manipulation_check was removed; send the round's checks as "
            "behaviour_spec.construal_checks (a list, one entry per check)"
        )
    grader_spec = {k: v for k, v in spec.items() if k != "construal_checks"}
    raw = spec.get("construal_checks")
    if raw is None:
        raw = []
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


def _construal_checks_section(checks: list[dict[str, str]]) -> str:
    """Render a normalised check set as the request's `## Construal Checks` section.

    Parameters
    ----------
    checks : list[dict[str, str]]
        Output of :func:`normalise_construal_checks`, e.g.
        ``[{"id": "M1", "property": "..."}]``.

    Returns
    -------
    str
        The section, with the no-verdict rule attached so it travels with the set.
    """
    return (
        "## Construal Checks\n\n"
        f"```json\n{json.dumps(checks, indent=2)}\n```\n\n"
        f"{CONSTRUAL_CHECKS_RULE}"
    )


async def run_transcript_analyst(cwd: str | None = None, **data: Any) -> str:
    """Run the transcript analyst agent and return its analysis report.

    Parameters
    ----------
    cwd : str | None
        Working directory for the agent. Defaults to the project root.
    **data : Any
        Fields from the invocation JSON. Required: ``topic``,
        ``transcript_source``. Optional: ``scanning_model``, ``constraints``,
        ``construal_checks`` (Orchestrator mode), ``behaviour_spec`` (forensic
        mode), ``artefacts_dir``, the re-ask pair ``conformance_defects`` +
        ``previous_report`` (see `runner.reask_section`), and the shared
        ``subagent_*`` fields.

    Returns
    -------
    str
        The Analyst's markdown report, per `analyst_interface_contract.md`.
    """
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

    # --- Construal checks: one renderer, one place per mode ---
    # Orchestrator mode sends them as a top-level `construal_checks` list;
    # forensic mode keeps them inside `behaviour_spec` (normalised below). A
    # set in the wrong place for its mode is an error rather than a guess,
    # because an unread key would silently run the round with no check.
    top_level_checks = data.get("construal_checks")
    if top_level_checks is not None and data.get("behaviour_spec"):
        raise RuntimeError(
            "Transcript analyst: in forensic mode construal checks go inside "
            "behaviour_spec.construal_checks, not at the top level"
        )
    if top_level_checks is not None:
        try:
            _, orch_checks = normalise_construal_checks({"construal_checks": top_level_checks})
        except ValueError as exc:
            raise RuntimeError(f"Transcript analyst: malformed construal_checks — {exc}") from exc
        if orch_checks:
            sections.append(_construal_checks_section(orch_checks))

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
            sections.append(_construal_checks_section(checks))
        # The work order is restated per request, not only in the system
        # prompt: an explicit request-level ordering is what recovered the
        # runs whose Analyst spent its turns on open-ended scanners first.
        sections.append(f"## Work Order\n\n{FORENSIC_WORK_ORDER}")

    if data.get("artefacts_dir"):
        sections.append(f"## Artefacts Directory\n\n{data['artefacts_dir']}")

    # The one re-ask after a failed conformance check (validated; malformed
    # raises → exit 2). Re-running scans would cost a full analysis and could
    # move the numbers, so a missing section is rebuilt from saved artefacts.
    reask = reask_section(
        "Transcript analyst",
        data.get("conformance_defects"),
        data.get("previous_report"),
        "Rebuild such a section only from your own saved artefacts under the "
        "artefacts directory (scanner definitions, validation results, scan "
        "results); never re-run a scan, re-validate a scanner, or rebuild the grader.",
    )
    if reask:
        sections.append(reask)

    # Absolute paths of the documents to read first. The forensic addendum is
    # named only in forensic mode, so an Orchestrator-mode analyst is never
    # pointed at it (the base contract deliberately carries no pointer either).
    docs = [
        ".claude/docs/contracts/analyst_interface_contract.md",
        ".claude/docs/references/scout_reference.md",
    ]
    if data.get("behaviour_spec"):
        docs.insert(1, ".claude/docs/contracts/analyst_forensic_addendum.md")
    sections.insert(0, contract_section(*docs))

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
        max_turns=data.get("subagent_max_turns"),
    )
