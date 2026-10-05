#!/usr/bin/env python3
"""Check a sub-agent report for conformance to its interface contract.

Every sub-agent contract fixes the report as a sequence of level-3 markdown
headings with **verbatim** text, in a given order, because the lead agent
locates sections structurally. Non-Claude models in particular substitute
their own headings or merge sections, and when that happens "no retirements
advised" becomes indistinguishable from an omitted section. This script makes
the check mechanical so the caller can re-ask once on failure instead of
reading around the defect.

Checks
------
* Every required heading of the role's active mode is present, at level 3,
  with the exact contract text, in the contract's order, once.
* No other heading at level 3 or shallower appears (a substituted heading
  shows up here, named, so the re-ask can quote it). Deeper headings
  (``####`` and below) are sub-structure and are ignored.
* **Critic, `candidate_critique` only:** every candidate proposed under
  `### Proposed Additional Candidates` carries a non-empty discriminating
  signature — the Critic contract's one non-negotiable.

Text before the first heading is reported as a warning, not a defect: the
contracts forbid it, but the Diagnostician disregards it by rule and a re-ask
for a preamble alone would waste an invocation.

Roles covered: ``critic`` (modes ``candidate_critique``, ``diff_review``,
``prediction``), ``mapper``, ``designer``, ``explorer``. The Executor and
Analyst contracts are being revised in parallel work-packages and carry
parameterised sub-headings; add them to ``ROLE_HEADINGS`` once they settle.

Usage
-----
::

    uv run --no-project python scripts/check_report.py critic report.md --mode candidate_critique
    uv run --no-project python scripts/check_report.py designer artefacts/designer/report_iter2.md

Exit codes: ``0`` conformant; ``1`` defects found (listed on stdout); ``2``
usage error (unknown role, missing or inapplicable ``--mode``, unreadable
file).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# --- Contract heading tables -----------------------------------------------
#
# Source of truth is each role's interface contract under
# `.claude/docs/contracts/`; these tuples restate the Report Format sections
# verbatim and in order. The Critic's tuples are duplicated in
# `subagents/redteam_critic/agent.py` (which renders them into the request as
# a checklist); `tests/test_check_report.py` asserts the two copies match and
# that every heading here appears in its contract.

CRITIC_HEADINGS: dict[str, tuple[str, ...]] = {
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

# Roles without modes key their single heading list under `None`.
ROLE_HEADINGS: dict[str, dict[str | None, tuple[str, ...]]] = {
    "critic": CRITIC_HEADINGS,
    "mapper": {
        None: (
            "Summary",
            "Environment Summary & File Catalogue",
            "Pipeline Model",
            "Instruction Analysis",
            "Scorer Analysis",
            "Tool & Environment Analysis",
            "Manipulable Surface Inventory",
            "Environment Defects",
            "Uncertainty & Open Questions",
        )
    },
    "designer": {
        None: (
            "Summary",
            "Proposed Variants",
            "Discrimination Matrix",
            "Variant Dependencies",
            "Confounds & Risks",
            "Uncertainty & Open Questions",
        )
    },
    "explorer": {
        None: (
            "Summary",
            "Environment Summary & File Catalogue",
            "Pipeline Model",
            "Modification Sites",
            "Variant Dependencies",
            "Global Risk Assessment",
            "Uncertainty & Open Questions",
        )
    },
}

# Headings the contracts mark *(only if applicable)*: allowed in position,
# not required.
OPTIONAL_HEADINGS: frozenset[str] = frozenset({"Variant Dependencies"})

# All contracts specify level-3 headings for report sections.
SECTION_LEVEL = 3

HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Heading:
    """A markdown heading found outside fenced code.

    Parameters
    ----------
    level : int
        Number of leading ``#``.
    text : str
        Heading text with surrounding whitespace removed.
    line : int
        1-based line number.
    """

    level: int
    text: str
    line: int


def extract_headings(lines: list[str]) -> list[Heading]:
    """Find every markdown heading, ignoring those inside fenced code blocks.

    Parameters
    ----------
    lines : list[str]
        The report split into lines.

    Returns
    -------
    list[Heading]
        Headings in document order.
    """
    headings: list[Heading] = []
    in_fence = False
    for lineno, line in enumerate(lines, start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence  # opening and closing fences look alike
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            headings.append(Heading(len(m.group("hashes")), m.group("text"), lineno))
    return headings


def _loose(text: str) -> str:
    """Normalise a heading for near-miss detection (case, punctuation, spacing).

    Parameters
    ----------
    text : str
        Heading text.

    Returns
    -------
    str
        Lower-cased alphanumerics separated by single spaces.
    """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def check_headings(
    headings: list[Heading], expected: tuple[str, ...]
) -> list[str]:
    """Compare the report's section headings with the contract's.

    Parameters
    ----------
    headings : list[Heading]
        All headings in the report.
    expected : tuple[str, ...]
        The contract's headings in order.

    Returns
    -------
    list[str]
        Defects; empty when the headings conform.
    """
    defects: list[str] = []
    expected_set = set(expected)
    loose_expected = {_loose(h): h for h in expected}

    # Only headings at the section level or shallower are structural; a
    # `####` inside a section is the agent's own sub-structure.
    structural = [h for h in headings if h.level <= SECTION_LEVEL]

    # --- Unexpected, non-verbatim, or mis-levelled headings ---------------
    seen: dict[str, int] = {}
    for h in structural:
        if h.text in expected_set:
            if h.level != SECTION_LEVEL:
                defects.append(
                    f"line {h.line}: `{'#' * h.level} {h.text}` is at level {h.level}; "
                    f"the contract requires level {SECTION_LEVEL} (`### {h.text}`)"
                )
            if h.text in seen:
                defects.append(
                    f"line {h.line}: duplicate section `### {h.text}` (first at line "
                    f"{seen[h.text]})"
                )
            seen.setdefault(h.text, h.line)
            continue
        near = loose_expected.get(_loose(h.text))
        if near:
            defects.append(
                f"line {h.line}: non-verbatim heading `{'#' * h.level} {h.text}` — "
                f"the contract text is `### {near}`"
            )
            seen.setdefault(near, h.line)  # counts as an attempt at that section
        else:
            defects.append(
                f"line {h.line}: heading `{'#' * h.level} {h.text}` is not a section of "
                "this mode's contract (substituted, merged, or from another mode)"
            )

    # --- Missing required sections ----------------------------------------
    if not headings:
        # Bold-wrapped titles (`**### Summary**`) are the usual cause; naming
        # it saves the re-ask from restating every missing section blindly.
        defects.append(
            "no markdown headings found at all — section titles must be `### ` "
            "lines, not bold or plain text"
        )
    for name in expected:
        if name not in seen and name not in OPTIONAL_HEADINGS:
            defects.append(f"missing section `### {name}`")

    # --- Order -------------------------------------------------------------
    observed_order = [h.text for h in structural if h.text in expected_set]
    contract_order = [h for h in expected if h in observed_order]
    if observed_order != contract_order:
        defects.append(
            "sections out of order — observed: "
            + " > ".join(observed_order)
            + "; contract: "
            + " > ".join(contract_order)
        )
    return defects


# --- Critic: proposed-candidate signatures ----------------------------------
#
# Proposals arrive in whatever shape the model chose: bold `**X1 — slug**`
# lines, `#### X13 — Title` headings, `**X13: slug**`, YAML-ish `- id: X6`
# lists, or a fenced JSON array. A block starts at any of the first three
# (an id with markup and a separator) or at an `id:` key; the signature is
# then a labelled field inside the block.

CAND_ID = r"(?P<id>[A-Z]{1,2}\d{1,3})"
BLOCK_START_MARKUP = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:[-*]\s+|\d+\.\s+)?(?:\*\*|__|`)\s*" + CAND_ID
    + r"\b\s*(?:`|\*\*|__)?\s*(?:[—–:]|-\s|\s*\()"
)
BLOCK_START_HEADING = re.compile(r"^#{1,6}\s+" + CAND_ID + r"\b\s*(?:[—–:]|-\s)")
# A plain bullet `- X6 — slug`: no markup, so the dash separator is required
# to keep prose lines that merely start with an id from opening a block.
BLOCK_START_BULLET = re.compile(r"^\s*[-*]\s+" + CAND_ID + r"\s+[—–]\s+\S")
# `- id: X6`, `- **id**: X6`, or `- **id:** \`X1\`` (colon inside the bold).
BLOCK_START_KEY = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*{1,2})?id\s*:?\s*(?:\*{1,2})?\s*:?\s*`?"
    + CAND_ID
    + r"`?\s*$",
    re.I,
)
SIGNATURE_LABEL = re.compile(
    r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:\*{1,2}|_{1,2})?\s*"
    r"(?:discriminating[\s_]+)?signature\s*(?:\*{1,2}|_{1,2})?\s*:?\s*(?:\*{1,2})?\s*"
    r"(?P<rest>.*)$",
    re.I,
)
# Any labelled field line (bold/italic label, or a bare `key:` at line start)
# ends the signature's content.
FIELD_LABEL = re.compile(
    r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:(?:\*{1,2}|_{1,2})[A-Za-z][^*_]{0,40}(?:\*{1,2}|_{1,2})\s*:?"
    r"|[A-Za-z_]{1,40}:\s)"
)
EMPTY_MARKER = re.compile(
    r"\b(none|empty|nothing to add|no additional|no new candidates|no candidates)\b", re.I
)


def _section_lines(lines: list[str], headings: list[Heading], name: str) -> list[str]:
    """Return the body lines of the named level-3 section.

    Parameters
    ----------
    lines : list[str]
        The report split into lines.
    headings : list[Heading]
        All headings in the report.
    name : str
        The exact heading text.

    Returns
    -------
    list[str]
        Lines after the heading up to the next heading at level 3 or
        shallower; empty when the section is absent.
    """
    for i, h in enumerate(headings):
        if h.text == name and h.level == SECTION_LEVEL:
            end = len(lines)
            for later in headings[i + 1 :]:
                if later.level <= SECTION_LEVEL:
                    end = later.line - 1
                    break
            return lines[h.line : end]
    return []


def _signature_present(block: list[str]) -> bool:
    """Whether a candidate block carries a non-empty discriminating signature.

    Parameters
    ----------
    block : list[str]
        The block's lines (first line is the block start).

    Returns
    -------
    bool
        True when a signature label is found with content after it.
    """
    for i, line in enumerate(block):
        m = SIGNATURE_LABEL.match(line)
        if not m:
            continue
        content = [m.group("rest")]
        for later in block[i + 1 :]:
            if FIELD_LABEL.match(later):
                break
            content.append(later)
        text = re.sub(r"[\s*_:`-]+", "", " ".join(content))
        return bool(text)
    return False


def _json_candidates(section: list[str]) -> tuple[list[dict], list[str]]:
    """Parse candidates from any fenced JSON blocks in the section.

    Parameters
    ----------
    section : list[str]
        The section's body lines.

    Returns
    -------
    tuple[list[dict], list[str]]
        Candidate objects found, and warnings for fences that did not parse.
    """
    found: list[dict] = []
    warnings: list[str] = []
    buf: list[str] | None = None
    for line in section:
        if FENCE_RE.match(line):
            if buf is None:
                buf = []
            else:
                try:
                    data = json.loads("\n".join(buf))
                except json.JSONDecodeError as exc:
                    warnings.append(f"a fenced block in the section is not valid JSON: {exc}")
                else:
                    items = data if isinstance(data, list) else [data]
                    found.extend(x for x in items if isinstance(x, dict) and "id" in x)
                buf = None
            continue
        if buf is not None:
            buf.append(line)
    return found, warnings


def check_critic_signatures(
    lines: list[str], headings: list[Heading]
) -> tuple[list[str], list[str]]:
    """Verify every proposed candidate carries a discriminating signature.

    Parameters
    ----------
    lines : list[str]
        The report split into lines.
    headings : list[Heading]
        All headings in the report.

    Returns
    -------
    tuple[list[str], list[str]]
        Defects (a proposal without a signature) and warnings (non-`X` ids,
        unparseable section).
    """
    defects: list[str] = []
    warnings: list[str] = []
    section = _section_lines(lines, headings, "Proposed Additional Candidates")
    if not section:
        return defects, warnings  # absence is a heading defect, reported elsewhere

    # --- Fenced JSON proposals ---------------------------------------------
    json_cands, json_warnings = _json_candidates(section)
    warnings.extend(json_warnings)
    ids_seen: list[str] = []
    for c in json_cands:
        cid = str(c.get("id", "?"))
        ids_seen.append(cid)
        if not str(c.get("discriminating_signature", "")).strip():
            defects.append(
                f"proposed candidate {cid} has no discriminating signature "
                "(JSON field empty or absent)"
            )

    # --- Markdown proposals --------------------------------------------------
    blocks: list[tuple[str, list[str]]] = []
    for line in section:
        m = (
            BLOCK_START_HEADING.match(line)
            or BLOCK_START_MARKUP.match(line)
            or BLOCK_START_BULLET.match(line)
            or BLOCK_START_KEY.match(line)
        )
        if m:
            cid = m.group("id")
            # A `- id: X13` key restating the id of the heading block it sits
            # in is the same candidate, not a new one.
            if blocks and blocks[-1][0] == cid:
                blocks[-1][1].append(line)
                continue
            blocks.append((cid, [line]))
        elif blocks:
            blocks[-1][1].append(line)
    for cid, block in blocks:
        ids_seen.append(cid)
        if not _signature_present(block):
            defects.append(
                f"proposed candidate {cid} has no discriminating signature "
                "(no `Discriminating signature` field with content in its block)"
            )

    # --- Sanity on what was (not) found -------------------------------------
    body = "\n".join(section)
    if not ids_seen:
        if re.search(r"\b[A-Z]{1,2}\d{1,3}\b", body) and not EMPTY_MARKER.search(body):
            warnings.append(
                "Proposed Additional Candidates mentions candidate ids but no candidate "
                "block was recognised — check the signatures by hand"
            )
        elif not EMPTY_MARKER.search(body) and body.strip():
            warnings.append(
                "Proposed Additional Candidates has content but no recognisable "
                "candidate and is not marked empty — check by hand"
            )
    for cid in ids_seen:
        if not cid.startswith("X"):
            warnings.append(
                f"proposed candidate {cid} does not use an `X<n>` id marking Critic "
                "origin; rename on merge"
            )
    return defects, warnings


# --- Driver -------------------------------------------------------------------


def check_report(
    role: str, text: str, mode: str | None = None
) -> tuple[list[str], list[str]]:
    """Run every applicable check on a report.

    Parameters
    ----------
    role : str
        One of ``ROLE_HEADINGS``.
    text : str
        The report's full text.
    mode : str | None
        The active mode for roles that have modes (the Critic); ``None``
        otherwise.

    Returns
    -------
    tuple[list[str], list[str]]
        Defects (non-zero exit) and warnings (informational).

    Raises
    ------
    ValueError
        Unknown role, or a mode that is missing, unknown, or given for a
        role without modes.
    """
    if role not in ROLE_HEADINGS:
        raise ValueError(
            f"unknown role {role!r}; known: {', '.join(ROLE_HEADINGS)} "
            "(executor and analyst are not yet covered — see module docstring)"
        )
    modes = ROLE_HEADINGS[role]
    if None in modes:
        if mode is not None:
            raise ValueError(f"role {role!r} has no modes; drop --mode")
    elif mode not in modes:
        raise ValueError(
            f"role {role!r} needs --mode, one of: {', '.join(m for m in modes if m)}"
        )
    expected = modes[mode]

    lines = text.splitlines()
    headings = extract_headings(lines)
    defects = check_headings(headings, expected)
    warnings: list[str] = []

    # Preamble: anything non-blank before the first heading.
    first = headings[0].line if headings else len(lines) + 1
    preamble = [ln for ln in lines[: first - 1] if ln.strip()]
    if preamble:
        warnings.append(
            f"{len(preamble)} non-blank line(s) precede the first heading; the contract "
            "says the report begins with its first section heading (read structurally)"
        )

    if role == "critic" and mode == "candidate_critique":
        sig_defects, sig_warnings = check_critic_signatures(lines, headings)
        defects.extend(sig_defects)
        warnings.extend(sig_warnings)
    return defects, warnings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Parameters
    ----------
    argv : list[str] | None
        Arguments (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        Process exit code (see module docstring).
    """
    parser = argparse.ArgumentParser(
        description="Check a sub-agent report's headings (and Critic signatures) "
        "against its interface contract."
    )
    parser.add_argument("role", help=f"one of: {', '.join(ROLE_HEADINGS)}")
    parser.add_argument("report", type=Path, help="path to the saved report")
    parser.add_argument(
        "--mode",
        default=None,
        help="active mode for the critic: " + ", ".join(CRITIC_HEADINGS),
    )
    args = parser.parse_args(argv)

    try:
        text = args.report.read_text()
    except OSError as exc:
        print(f"error: cannot read {args.report}: {exc}", file=sys.stderr)
        return 2
    try:
        defects, warnings = check_report(args.role, text, args.mode)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    label = f"{args.role}/{args.mode}" if args.mode else args.role
    if defects:
        print(f"{args.report}: {label} — {len(defects)} defect(s)")
        for d in defects:
            print(f"  - {d}")
    else:
        print(f"{args.report}: {label} — OK")
    for w in warnings:
        print(f"  warning: {w}")
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
