"""Lint an append-only investigation log for duplication and ordering defects.

`INVESTIGATION-LOG.md` is append-only by rule, so nothing ever gets tidied:
a block pasted twice stays pasted twice, and a block written late lands under
whatever heading happens to be last. Over a long run these accumulate until the
audit artefact stops being navigable. This script finds the three defect shapes
the six-run analysis recorded, so the Diagnostician can catch them at the moment
of writing rather than leave them for the reader:

1. **duplicate-heading** — the same heading twice under the same parent. Scoped
   to the parent deliberately: `### Pre-registered Predictions` recurs once per
   `## Iteration N` by design, and only becomes a defect when it recurs *within*
   one iteration.
2. **duplicate-block** — a section whose body is verbatim identical to an
   earlier section's, wherever it sits. Catches the paste-twice even when the
   heading was reworded.
3. **stranded-block** — a `###`/`####` heading that names `iteration N` but sits
   under `## Iteration M` (M != N) or under a Phase 4/5 heading. The log is
   append-only, so the fix is a forward pointer at the natural location, not a
   move — but the writer should know it happened.

Headings inside fenced code blocks are ignored (the log may quote templates).

Usage:
    uv run --no-project python scripts/lint_log.py <log.md> [...] [--min-lines N]

Output: one `path:line: [kind] message` line per finding. Exit 0 when clean,
1 when there are findings, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_ITERATION_REF = re.compile(r"\biteration\s+(\d+)\b", re.IGNORECASE)
_ITERATION_PARENT = re.compile(r"^iteration\s+(\d+)\b", re.IGNORECASE)
_LATE_PHASE_PARENT = re.compile(r"^phase\s+[45]\b", re.IGNORECASE)

# Bodies shorter than this are not reported as duplicate blocks: a two-line
# `### Decision` that reads "continue" twice is coincidence, not a paste.
DEFAULT_MIN_LINES = 3

# A body made only of labelled fields with short answers — a filled-in form,
# e.g. the Orchestrator's `### Surprises` reading "yes / Nothing / none / no" —
# legitimately repeats verbatim across quiet iterations, so it is never
# reported as a pasted duplicate however many lines it has. Prose, tables and
# long field values still are.
_SHORT_FORM_LINE = re.compile(r"^\s*-\s+\*{1,2}[^*]+\*{1,2}\s*:\s*.{0,24}$")


def _is_short_form(body: list[str]) -> bool:
    """True when every line is a `- *Label*: <short answer>` field.

    Parameters
    ----------
    body : list[str]
        A section's own non-empty lines.

    Returns
    -------
    bool
        Whether the body is a filled-in form whose repetition is coincidental.

    Examples
    --------
    >>> _is_short_form(["- *Aligned with the agreed scope?*: yes", "- *What surprised me*: Nothing"])
    True
    """
    return bool(body) and all(_SHORT_FORM_LINE.match(line) for line in body)


@dataclass
class Section:
    """One heading and the lines under it, up to the next heading of the same or higher level."""

    level: int
    text: str
    line: int
    ancestors: tuple[tuple[int, str], ...]  # (level, text) of each open heading, outermost first
    body: list[str] = field(default_factory=list)


@dataclass
class Finding:
    """One lint result."""

    line: int
    kind: str
    message: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_sections(lines: list[str]) -> list[Section]:
    """Split a markdown document into heading-rooted sections.

    Parameters
    ----------
    lines : list[str]
        The document's lines, without trailing newlines.

    Returns
    -------
    list[Section]
        One section per heading, in document order, each carrying the
        ``(level, text)`` of its ancestor headings (nearest-last) and the non-empty lines that
        follow it up to the next heading of equal or higher level. Lines
        inside ``` / ~~~ fences are never treated as headings, and text before
        the first heading is discarded.
    """
    sections: list[Section] = []
    stack: list[Section] = []  # open headings, outermost first
    in_fence = False

    for number, raw in enumerate(lines, start=1):
        if _FENCE.match(raw):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(raw)

        if match is None:
            stripped = raw.rstrip()
            if stripped:
                # A line belongs to every open section that encloses it, but
                # for duplicate-block comparison we only want the *nearest*
                # heading's own body; deeper sections are compared separately.
                if stack:
                    stack[-1].body.append(stripped)
            continue

        level = len(match.group(1))
        while stack and stack[-1].level >= level:
            stack.pop()
        section = Section(
            level=level,
            text=match.group(2),
            line=number,
            ancestors=tuple((s.level, s.text) for s in stack),
        )
        sections.append(section)
        stack.append(section)

    return sections


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_duplicate_headings(sections: list[Section]) -> list[Finding]:
    """Flag headings repeated under the same parent heading.

    Parameters
    ----------
    sections : list[Section]
        Parsed sections in document order.

    Returns
    -------
    list[Finding]
        One `duplicate-heading` finding per repeat, pointing at the first
        occurrence.
    """
    first_seen: dict[tuple[tuple[str, ...], str], int] = {}
    findings = []
    for s in sections:
        key = (s.ancestors, s.text.strip().lower())
        if key in first_seen:
            findings.append(
                Finding(s.line, "duplicate-heading", f"'{s.text}' repeats a heading at line {first_seen[key]} under the same parent")
            )
        else:
            first_seen[key] = s.line
    return findings


def check_duplicate_blocks(sections: list[Section], min_lines: int) -> list[Finding]:
    """Flag sections whose body is verbatim identical to an earlier one.

    Parameters
    ----------
    sections : list[Section]
        Parsed sections in document order.
    min_lines : int
        Bodies with fewer non-empty lines than this are ignored.

    Returns
    -------
    list[Finding]
        One `duplicate-block` finding per repeat, pointing at the first
        occurrence. Only the section's *own* lines are compared (not its
        children's), so a parent whose children are all duplicated shows up
        via the children, not spuriously via itself.
    """
    first_seen: dict[tuple[str, ...], Section] = {}
    findings = []
    for s in sections:
        if len(s.body) < min_lines or _is_short_form(s.body):
            continue
        key = tuple(s.body)
        if key in first_seen:
            earlier = first_seen[key]
            findings.append(
                Finding(
                    s.line,
                    "duplicate-block",
                    f"body of '{s.text}' ({len(s.body)} lines) is identical to '{earlier.text}' at line {earlier.line}",
                )
            )
        else:
            first_seen[key] = s
    return findings


def check_stranded_blocks(sections: list[Section]) -> list[Finding]:
    """Flag sub-headings that name an iteration other than their parent's.

    Parameters
    ----------
    sections : list[Section]
        Parsed sections in document order.

    Returns
    -------
    list[Finding]
        One `stranded-block` finding per heading whose ``iteration N`` does
        not match its enclosing ``## Iteration M``, or which sits under a
        Phase 4 / Phase 5 heading. Iteration references under Phase 1–3
        headings are left alone: iteration 0 conventionally lives there.
    """
    findings = []
    for s in sections:
        if s.level < 3 or not s.ancestors:
            continue
        ref = _ITERATION_REF.search(s.text)
        if ref is None:
            continue
        # The nearest level-2 ancestor is the block this heading was appended
        # under; that is the only context a reader navigating by `##` has.
        parent = next((text for level, text in reversed(s.ancestors) if level == 2), None)
        if parent is None:
            continue
        n = int(ref.group(1))
        it = _ITERATION_PARENT.match(parent.strip())
        misplaced = (it is not None and int(it.group(1)) != n) or _LATE_PHASE_PARENT.match(parent.strip()) is not None
        if misplaced:
            findings.append(Finding(s.line, "stranded-block", f"'{s.text}' names iteration {n} but sits under '{parent}'"))
    return findings


def lint(text: str, min_lines: int = DEFAULT_MIN_LINES) -> list[Finding]:
    """Run every check over one document.

    Parameters
    ----------
    text : str
        The markdown source.
    min_lines : int
        Passed to `check_duplicate_blocks`.

    Returns
    -------
    list[Finding]
        All findings, sorted by line.
    """
    sections = parse_sections(text.splitlines())
    findings = check_duplicate_headings(sections) + check_duplicate_blocks(sections, min_lines) + check_stranded_blocks(sections)
    return sorted(findings, key=lambda f: (f.line, f.kind))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Lint each file given and print findings.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments; defaults to `sys.argv[1:]`.

    Returns
    -------
    int
        0 when every file is clean, 1 when any finding was reported, 2 when a
        file could not be read.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="markdown files to lint")
    parser.add_argument("--min-lines", type=int, default=DEFAULT_MIN_LINES, help=f"minimum body lines for duplicate-block (default {DEFAULT_MIN_LINES})")
    args = parser.parse_args(argv)

    any_findings = False
    for raw in args.paths:
        path = Path(raw)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 2
        findings = lint(text, args.min_lines)
        for f in findings:
            print(f"{path}:{f.line}: [{f.kind}] {f.message}")
        print(f"{path}: {len(findings)} finding(s)")
        any_findings = any_findings or bool(findings)
    return 1 if any_findings else 0


if __name__ == "__main__":
    sys.exit(main())
