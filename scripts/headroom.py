"""Summarise a headroom survey: per cell, how often the eval's own scorer fires.

A headroom survey runs each candidate eval, unmodified, on each candidate
model at small n before any experiment is designed on it
(`orchestrator_responsibilities.md §Phase 1, step 9`). Its one question is
whether the target behaviour can move at all: a cell where every model scores
0% (or 100%) cannot register any manipulation, and a factorial built on it
spends hours to learn nothing. One archived overnight run spent three
factorial iterations, about six hours, on cells a 30-minute survey would have
shown sitting at 0/30.

This is the one place the lead reads eval scores before an Analyst report, and
the exemption is narrow: a survey has no contrast, so there is no comparison
for an early look to bias. The script reads only the directory it is given;
never point it at an iteration's logs.

For each cell — the condition directory under the logs dir, its task and its
model — and each scorer (each key, for dict-valued scores), it reports the
count of samples whose score converts to a number above zero, with a Wilson
95% interval and the Analyst's floor/ceiling tags: `[floor]` when the upper
bound is below 0.20, `[ceiling]` when the lower bound is above 0.80. At
n = 20 a 0/20 cell is already tagged `[floor]`; at n = 15 it is not, which is
why the survey's default is 20 per cell.

Usage:
    uv run python scripts/headroom.py investigations/<name>/headroom/logs

Logs whose status is not `success` are skipped and named; samples that
errored are excluded from n and counted.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

from inspect_ai.log import read_eval_log
from inspect_ai.scorer import value_to_float

# Same thresholds as the Analyst's floor/ceiling markers
FLOOR_UPPER = 0.20
CEILING_LOWER = 0.80
Z95 = 1.959964


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Parameters
    ----------
    k : int
        Number of positive samples.
    n : int
        Number of samples.
    z : float
        Normal quantile; the default gives a 95% interval.

    Returns
    -------
    tuple[float, float]
        Lower and upper bounds, e.g. ``wilson(0, 20)`` → ``(0.0, 0.161)``.
    """
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def tag(lo: float, hi: float) -> str:
    """The floor/ceiling marker for an interval, or "" when it has headroom.

    Parameters
    ----------
    lo, hi : float
        The Wilson interval's bounds.

    Returns
    -------
    str
        ``"[floor]"``, ``"[ceiling]"`` or ``""``.
    """
    if hi < FLOOR_UPPER:
        return "[floor]"
    if lo > CEILING_LOWER:
        return "[ceiling]"
    return ""


def score_values(scores: dict) -> dict[str, float]:
    """Flatten one sample's scores to {scorer or scorer.key: number}.

    Parameters
    ----------
    scores : dict
        The sample's ``scores``, scorer name → ``Score``.

    Returns
    -------
    dict[str, float]
        One number per scorer, or per key of a dict-valued score, e.g.
        ``{"grader": 1.0}`` or ``{"judge.harmful": 0.0, "judge.leak": 1.0}``.
        Values Inspect cannot convert (free text, lists) are left out.
    """
    to_float = value_to_float()
    out: dict[str, float] = {}
    for name, score in (scores or {}).items():
        value = score.value
        items = value.items() if isinstance(value, dict) else [(None, value)]
        for key, v in items:
            if isinstance(v, (list, tuple)):
                continue
            try:
                out[name if key is None else f"{name}.{key}"] = float(to_float(v))
            except (TypeError, ValueError):
                continue
    return out


def survey(logs_dir: Path) -> tuple[list[dict], list[str]]:
    """Tally positive samples per cell and scorer.

    Parameters
    ----------
    logs_dir : Path
        The survey's logs directory; each condition's logs sit in their own
        sub-directory, as the Executor writes them.

    Returns
    -------
    tuple[list[dict], list[str]]
        One row per (cell, task, model, scorer) with ``n``, ``positive``,
        ``errored``, ``lo``, ``hi`` and ``tag``; and the skipped logs, each
        with its status.
    """
    tallies: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "positive": 0, "errored": 0})
    errored: dict[tuple, int] = defaultdict(int)
    skipped: list[str] = []

    for path in sorted(logs_dir.rglob("*.eval")):
        log = read_eval_log(str(path))
        rel = path.relative_to(logs_dir)
        # The cell is the condition directory; a log at the top level has none
        cell = rel.parts[0] if len(rel.parts) > 1 else "-"
        if log.status != "success":
            skipped.append(f"{rel} ({log.status})")
            continue
        base = (cell, log.eval.task, log.eval.model)
        for sample in log.samples or []:
            if sample.error is not None:
                errored[base] += 1
                continue
            for scorer, value in score_values(sample.scores).items():
                t = tallies[base + (scorer,)]
                t["n"] += 1
                t["positive"] += value > 0

    rows = []
    for (cell, task, model, scorer), t in sorted(tallies.items()):
        lo, hi = wilson(t["positive"], t["n"])
        rows.append(
            {
                "cell": cell, "task": task, "model": model, "scorer": scorer,
                "n": t["n"], "positive": t["positive"], "errored": errored[(cell, task, model)],
                "lo": lo, "hi": hi, "tag": tag(lo, hi),
            }
        )
    return rows, skipped


def render(rows: list[dict], skipped: list[str]) -> str:
    """Format the tally as a Markdown table for the log's Headroom Survey block.

    Parameters
    ----------
    rows : list[dict]
        From ``survey``.
    skipped : list[str]
        From ``survey``.

    Returns
    -------
    str
        The table, then any skipped logs.
    """
    lines = [
        "| Cell | Task | Model | Scorer | Positive / n | Wilson 95% CI | Tag | Errored |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['cell']} | {r['task']} | {r['model']} | {r['scorer']} | "
            f"{r['positive']}/{r['n']} | {r['lo']:.0%}–{r['hi']:.0%} | {r['tag']} | {r['errored']} |"
        )
    if not rows:
        lines.append("| (no scored samples) | | | | | | | |")
    if skipped:
        lines.append("")
        lines.append("Skipped (not `success`): " + "; ".join(skipped))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the survey table for a logs directory.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        0 on success, 2 when the directory does not exist.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("logs_dir", type=Path, help="the survey's logs directory")
    args = parser.parse_args(argv)
    if not args.logs_dir.is_dir():
        print(f"not a directory: {args.logs_dir}", file=sys.stderr)
        return 2
    print(render(*survey(args.logs_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
