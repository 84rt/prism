"""Which Prism this folder runs: one line for the log's Agreed Scope and the report.

A finding is only reproducible if a reader knows which instructions produced
it, and an installed folder reaches Prism one of two ways:

- **frozen** (``install.sh --pin``): its links point into a copy of one Prism
  commit inside the folder, whose ``PINNED_COMMIT`` file names the commit and
  records whether the folder's own patch was applied to it (the model
  forensics demo, for one, runs with published answers removed);
- **live**: its links point at a Prism checkout, which may have moved on, or
  carry uncommitted edits, by the time anyone reads the report.

Usage::

    uv run --no-project python scripts/prism_version.py

Prints, for example::

    Prism 503a9d0 (2026-11-10, "let a pinned demo folder keep its own edits ..."), frozen copy, with this folder's own edits (.prism-local.patch)
    Prism 7bfb151 (2026-11-10, "had the eval runner count ..."), live checkout, not frozen: later Prism changes reach this folder mid-run

Standard library only; exits 0 even when the version can't be read (it says
so), since a missing provenance line is a caveat, not a reason to stop.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Resolved through this file's link, so it is the Prism the folder actually runs
SCAFFOLD_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PIN_MARKER = "PINNED_COMMIT"
LOCAL_EDITS_PREFIX = "local-edits "


def _describe(commit: str, date: str, subject: str) -> str:
    """``Prism <short id> (<day>, "<subject>")``, the subject trimmed for one line."""
    short = subject if len(subject) <= 60 else subject[:57].rstrip() + "..."
    return f'Prism {commit[:7]} ({date[:10]}, "{short}")'


def frozen_version(root: str) -> str | None:
    """The line for a frozen copy, or None when ``root`` is not one.

    Parameters
    ----------
    root : str
        The Prism tree this folder's links resolve to.

    Returns
    -------
    str | None
        From the marker's lines: commit, commit date, subject, then an
        optional ``local-edits <patch file>`` line the installer adds when it
        applied the folder's own patch.
    """
    try:
        with open(os.path.join(root, PIN_MARKER)) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if len(lines) < 3:
        return "Prism version unreadable: the frozen copy's marker file is incomplete"
    line = _describe(*lines[:3]) + ", frozen copy"
    edits = [ln[len(LOCAL_EDITS_PREFIX):] for ln in lines[3:] if ln.startswith(LOCAL_EDITS_PREFIX)]
    if edits:
        line += f", with this folder's own edits ({', '.join(edits)})"
    return line


def live_version(root: str) -> str:
    """The line for a live checkout, read with git.

    Parameters
    ----------
    root : str
        The checkout this folder's links resolve to.

    Returns
    -------
    str
        The checkout's current commit, flagged as not frozen, and as carrying
        uncommitted edits to tracked files when it does.
    """
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout

    try:
        commit, date, subject = git("log", "-1", "--format=%H%n%cI%n%s").splitlines()[:3]
        dirty = bool(git("status", "--porcelain", "--untracked-files=no").strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return f"Prism version unknown: {root} is neither a frozen copy nor a readable git checkout"
    line = _describe(commit, date, subject) + ", live checkout, not frozen: later Prism changes reach this folder mid-run"
    if dirty:
        line += "; uncommitted edits present"
    return line


def main() -> int:
    """Print this folder's Prism version line.

    Returns
    -------
    int
        Always 0.
    """
    print(frozen_version(SCAFFOLD_ROOT) or live_version(SCAFFOLD_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
