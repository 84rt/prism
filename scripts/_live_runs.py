"""Is any Prism investigation in a target folder still running?

Install-time plumbing for ``install.sh --pin`` and ``uninstall.sh``: before either replaces
or removes a target's scaffold links or pinned copy, it asks this script whether
doing so would change files under a live run. Run from the scaffold checkout,
never linked into a target::

    python3 scripts/_live_runs.py /path/to/target_repo

An investigation counts as running when either

- one of its helpers is still alive (its receipt's process answers), or
- it has neither ``FINDINGS-REPORT.md`` nor ``RESUME.md`` and its log changed
  within the last ``RECENT_HOURS`` hours: the lead works between helpers
  with none alive, and a run abandoned without either file stops counting
  once it goes quiet.

Prints one line per running investigation and exits 1 if there is any, else
exits 0 silently. Standard library only, like ``run_status.py``.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import launch_subagent  # noqa: E402

# How long a quiet, unfinished log still counts as a run in progress. The
# lead's gaps between log writes are minutes; a helper-free wait longer than
# this is a stall or an abandoned run.
RECENT_HOURS = 2.0


def live_reasons(inv: str, now: float | None = None) -> list[str]:
    """Why one investigation counts as running, or [] when it doesn't.

    Parameters
    ----------
    inv : str
        An ``investigations/<name>`` folder.
    now : float | None
        The current time (seconds since the epoch); defaults to now.

    Returns
    -------
    list[str]
        Short reasons, e.g. ``["executor 02_round2 still running"]``.
    """
    now = time.time() if now is None else now
    reasons = []

    # ---- Any helper whose process still answers ----
    for receipt in launch_subagent.list_receipts(inv):
        if launch_subagent.exit_code_for(receipt) == launch_subagent.EXIT_RUNNING:
            who = " ".join(filter(None, [receipt.get("role") or "helper", receipt.get("tag")]))
            reasons.append(f"{who} still running")

    # ---- An unfinished log the lead wrote to recently ----
    finished = any(os.path.exists(os.path.join(inv, f)) for f in ("FINDINGS-REPORT.md", "RESUME.md"))
    log = os.path.join(inv, "INVESTIGATION-LOG.md")
    if not finished and os.path.exists(log):
        age_h = (now - os.path.getmtime(log)) / 3600
        if age_h < RECENT_HOURS:
            reasons.append(f"no report yet and its log changed {age_h * 60:.0f} minutes ago")
    return reasons


def main(argv: list[str] | None = None) -> int:
    """Report the target's running investigations.

    Parameters
    ----------
    argv : list[str] | None
        ``[target_repo]``; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        1 if any investigation is running, else 0 (also when the target has
        no ``investigations/`` folder).
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("Usage: python3 scripts/_live_runs.py /path/to/target_repo", file=sys.stderr)
        return 2
    root = os.path.join(argv[0], "investigations")
    if not os.path.isdir(root):
        return 0

    live = 0
    for name in sorted(os.listdir(root)):
        inv = os.path.join(root, name)
        if not os.path.isdir(inv):
            continue
        reasons = live_reasons(inv)
        if reasons:
            live += 1
            print(f"  Running: investigations/{name} ({'; '.join(reasons)})")
    return 1 if live else 0


if __name__ == "__main__":
    sys.exit(main())
