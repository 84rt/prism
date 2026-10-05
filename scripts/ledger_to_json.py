#!/usr/bin/env python3
"""Serialise the live candidate ledger (`CANDIDATES.md`) into sub-agent JSON.

The Variant Designer and the Red-Teaming Critic both take the ledger as a
JSON array of ``{id, slug, statement, discriminating_signature, status}``
(see `designer_interface_contract.md §Candidate explanations` and
`critic_interface_contract.md §Fields for candidate_critique`). Deriving that
array by hand each round invites drift between the markdown ledger, which is
the source of truth, and what the sub-agents are actually shown — and in one
run it shipped a candidate with *empty* statement and signature fields to
both agents, so the Critic ended up critiquing a blank. This script is the
sanctioned serialiser, and its one non-negotiable is that it **fails loudly**
rather than emit a live candidate whose *Statement* or *Discriminating
signature* is missing.

What is emitted
---------------
Only candidates under the ledger's two live sections — `## Live candidates
(ranked)` and `## Live but untested` — are serialised; eliminated and inert
candidates are absent by contract. A candidate under a live section whose
heading trailer nevertheless says it was eliminated, retired, merged or
withdrawn is skipped with a warning: the section is authoritative for what
the sub-agents receive, but the trailer means the ledger needs tidying.

Ledger format parsed (from `diagnostician_responsibilities.md §State
Management`)::

    ## Live candidates (ranked)

    ### C1 — `slug` — rank 1
    - *Statement*: <the explanation>
    - *Origin*: ...
    - *Discriminating signature*: <what moves it, in which direction>
    - *Evidence for*: ...

Field values may wrap onto following lines (any non-blank line that does not
start another field, heading, or section continues the current field).

Usage
-----
::

    uv run --no-project python scripts/ledger_to_json.py CANDIDATES.md
    uv run --no-project python scripts/ledger_to_json.py CANDIDATES.md -o candidates.json

Writes the JSON array to stdout (or to ``-o``) and a one-line-per-candidate
summary to stderr. Exit codes: ``0`` serialised; ``1`` at least one live
candidate is missing a required field — the defects are listed and nothing is
written; ``2`` the file does not look like a ledger (no live section found)
or a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- Ledger grammar ---------------------------------------------------------

# `### C1 — `slug` — rank 1`. The id is a letter prefix plus a number: C<n>
# for Diagnostician-authored, X<n> for Critic-originated, and runs have used
# other prefixes (E<n>, A<n>) for evidence- and analyst-suggested candidates.
# The dash between id and slug is an em-dash in the template, but hyphens and
# en-dashes appear in practice and are accepted.
CANDIDATE_HEADING = re.compile(
    r"^###\s+(?P<id>[A-Z]{1,2}\d{1,3})\s+[—–-]+\s+`(?P<slug>[^`]+)`(?P<trailer>.*)$"
)

# A `- *Label*: value` field line. The label may carry a parenthetical
# qualifier before the colon (e.g. `*Discriminating signature* (authored by
# the Diagnostician): ...`), and the separator is a colon in the template but
# an em-dash has been used. Bold labels (`- **Label**:`) are also accepted as
# field starts so that they terminate the previous field, even though none of
# the forwarded fields are written that way. Anchored at column 0 on purpose:
# an entry's fields are never indented, whereas an *indented* `- *Label*:`
# line is a sub-bullet of the field above it (a signature written as
# `- *Against C1*: ...` / `- *Against X11*: ...`) and must be kept as content.
FIELD_LINE = re.compile(
    r"^-\s+\*{1,2}(?P<label>[^*]+?)\*{1,2}"  # - *Label*
    r"(?:\s*\((?:[^()]|\([^()]*\))*\))?"  # optional (qualifier)
    r"\s*[:—–-]?\s*(?P<value>.*)$"
)

# The two field labels the sub-agent contracts carry. Everything else on an
# entry (origin, evidence, confounds, recipe membership) is ledger bookkeeping
# and is not forwarded.
FORWARDED_LABELS = {
    "statement": "statement",
    "discriminating signature": "discriminating_signature",
}

# Section headings that hold live candidates, matched on a normalised prefix
# so that minor variations in the trailing text do not silently drop a
# section. Any other `## ` heading closes the live region.
LIVE_SECTION_PREFIXES = ("live candidates", "live but untested")

# A heading trailer containing one of these words contradicts a live section
# and marks the entry as not to be sent.
NOT_LIVE_TRAILER = re.compile(r"\b(eliminated|retired|merged|withdrawn)\b", re.I)


@dataclass
class Candidate:
    """One parsed ledger entry.

    Parameters
    ----------
    id : str
        The ledger id (``C1``, ``X3``, ...).
    slug : str
        The backticked slug from the heading.
    trailer : str
        Whatever followed the slug on the heading line (rank, status notes).
    line : int
        1-based line number of the heading, for error messages.
    fields : dict[str, str]
        Forwarded field values keyed by JSON field name.
    """

    id: str
    slug: str
    trailer: str
    line: int
    fields: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, str]:
        """Render the entry in the sub-agent contract shape.

        Returns
        -------
        dict[str, str]
            ``{id, slug, statement, discriminating_signature, status}``.
        """
        return {
            "id": self.id,
            "slug": self.slug,
            "statement": self.fields.get("statement", ""),
            "discriminating_signature": self.fields.get("discriminating_signature", ""),
            "status": "live",
        }


@dataclass
class ParseResult:
    """Everything the parser learned about a ledger.

    Parameters
    ----------
    candidates : list[Candidate]
        Live candidates in ledger order (skipped entries excluded).
    warnings : list[str]
        Non-fatal observations (skipped entries, unparsed headings).
    live_section_seen : bool
        Whether any live section heading was found at all.
    """

    candidates: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    live_section_seen: bool = False


def _normalise_label(label: str) -> str:
    """Lower-case and collapse whitespace so label matching is forgiving.

    Parameters
    ----------
    label : str
        The raw text between the asterisks of a field line.

    Returns
    -------
    str
        The normalised label, e.g. ``"discriminating signature"``.
    """
    return " ".join(label.split()).lower()


def parse_ledger(text: str) -> ParseResult:
    """Parse the live candidates out of a `CANDIDATES.md` document.

    Parameters
    ----------
    text : str
        Full text of the ledger.

    Returns
    -------
    ParseResult
        Live candidates, warnings, and whether a live section was found.
    """
    result = ParseResult()
    in_live_section = False
    current: Candidate | None = None
    current_field: str | None = None  # JSON key of the field being continued

    def close_candidate() -> None:
        nonlocal current, current_field
        if current is not None:
            result.candidates.append(current)
        current, current_field = None, None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()

        # --- Section boundaries -------------------------------------------
        if line.startswith("## "):
            close_candidate()
            heading = _normalise_label(line[3:])
            in_live_section = heading.startswith(LIVE_SECTION_PREFIXES)
            result.live_section_seen |= in_live_section
            continue
        if not in_live_section:
            continue

        # --- Candidate headings -------------------------------------------
        if line.startswith("### "):
            close_candidate()
            m = CANDIDATE_HEADING.match(line)
            if not m:
                result.warnings.append(
                    f"line {lineno}: heading under a live section did not parse as "
                    f"`### <id> — `<slug>` — ...` and was ignored: {line!r}"
                )
                continue
            trailer = m.group("trailer")
            if "###" in trailer:
                # Two entries share one heading line; only the first is parsed.
                result.warnings.append(
                    f"line {lineno}: two candidate headings on one line — only "
                    f"{m.group('id')} was parsed; give each its own `### ` line"
                )
            status_word = NOT_LIVE_TRAILER.search(trailer)
            if status_word:
                result.warnings.append(
                    f"line {lineno}: {m.group('id')} `{m.group('slug')}` sits under a "
                    f"live section but its heading says {status_word.group(1)!r}; "
                    "not emitted — move the entry to the matching section"
                )
                continue  # `current` stays None, so its fields are ignored
            current = Candidate(
                id=m.group("id"), slug=m.group("slug"), trailer=trailer, line=lineno
            )
            continue

        if current is None:
            continue

        # --- Field lines and continuations --------------------------------
        m = FIELD_LINE.match(line)
        if m:
            key = FORWARDED_LABELS.get(_normalise_label(m.group("label")))
            if key is None:
                current_field = None  # a bookkeeping field: ends any continuation
                continue
            current_field = key
            # A repeated label would silently overwrite; keep the first and flag.
            if key in current.fields:
                result.warnings.append(
                    f"line {lineno}: {current.id} has a second *{m.group('label')}* "
                    "field; the first was kept"
                )
                current_field = None
                continue
            current.fields[key] = m.group("value").strip()
            continue

        if not line.strip():
            current_field = None  # a blank line ends a wrapped field
            continue
        if current_field is not None:
            # Wrapped continuation of the field above (nested bullets included).
            current.fields[current_field] = (
                current.fields[current_field] + " " + line.strip()
            ).strip()

    close_candidate()
    return result


def find_defects(candidates: list[Candidate]) -> list[str]:
    """List every reason the parsed ledger must not be serialised.

    Parameters
    ----------
    candidates : list[Candidate]
        The live candidates from :func:`parse_ledger`.

    Returns
    -------
    list[str]
        Human-readable defects; empty when the ledger is serialisable.
    """
    defects: list[str] = []
    seen: dict[str, int] = {}
    for c in candidates:
        if c.id in seen:
            defects.append(
                f"{c.id} (line {c.line}): duplicate id — also at line {seen[c.id]}"
            )
        seen.setdefault(c.id, c.line)
        # The two contract fields are the admissibility bar: a candidate
        # without a signature cannot be tested or retired, and a candidate
        # without a statement is a slug. Either reaching a sub-agent as an
        # empty string is exactly the failure this script exists to prevent.
        for label, key in FORWARDED_LABELS.items():
            if not c.fields.get(key, "").strip():
                defects.append(
                    f"{c.id} `{c.slug}` (line {c.line}): missing *{label.capitalize()}* — "
                    "write the entry in the §State Management field format, "
                    "or move it out of the live sections"
                )
    return defects


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
        description="Serialise the live candidates of CANDIDATES.md to sub-agent JSON."
    )
    parser.add_argument("ledger", type=Path, help="Path to CANDIDATES.md")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write the JSON array here instead of stdout",
    )
    args = parser.parse_args(argv)

    try:
        text = args.ledger.read_text()
    except OSError as exc:
        print(f"error: cannot read {args.ledger}: {exc}", file=sys.stderr)
        return 2

    result = parse_ledger(text)
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)

    if not result.live_section_seen:
        print(
            f"error: no `## Live candidates (ranked)` or `## Live but untested` section "
            f"found in {args.ledger} — is this a CANDIDATES.md per §State Management?",
            file=sys.stderr,
        )
        return 2

    defects = find_defects(result.candidates)
    if defects:
        print(
            f"error: {len(defects)} defect(s) in {args.ledger}; nothing written:",
            file=sys.stderr,
        )
        for d in defects:
            print(f"  - {d}", file=sys.stderr)
        return 1

    payload = [c.to_json() for c in result.candidates]
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    else:
        sys.stdout.write(rendered)

    # A per-candidate size line so a suspiciously short field is visible in
    # the Diagnostician's terminal even when the JSON itself is not read.
    for c in payload:
        print(
            f"{c['id']:>4}  {c['slug']:<44} statement={len(c['statement']):>4}ch  "
            f"signature={len(c['discriminating_signature']):>4}ch",
            file=sys.stderr,
        )
    dest = str(args.output) if args.output is not None else "stdout"
    print(f"{len(payload)} live candidate(s) -> {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
