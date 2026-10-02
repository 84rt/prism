"""Running spend total for an investigation.

Sums two telemetry sources that the pipeline now writes:

- **Sub-agent cost sidecars** — ``cost_*.json`` files written by
  ``subagents/runner.py`` from the Agent SDK's result message (USD, turns,
  tokens) for every invocation launched through ``scripts/launch_subagent.py``.
- **Eval token usage** — per condition-model pair, recorded in each batch
  run's ``status.json`` by ``scripts/execute_evals.py`` from the eval log
  header's ``model_usage``.

Eval spend is reported in tokens per model, not USD: the runner has no price
table and the target models vary per investigation. The stopping policy that
consumes these figures lives in ``diagnostician_responsibilities.md``.

Usage
-----
    uv run python scripts/spend.py investigations/<name> [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any


def _load(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def collect(root: str) -> dict[str, Any]:
    """Walk an investigation directory and total its telemetry.

    Parameters
    ----------
    root : str
        ``investigations/<name>`` (any directory; walked recursively).

    Returns
    -------
    dict
        ``subagents``: total USD, count, per-role totals and the list of
        sidecars; ``evals``: per-model token totals and per-run summaries.
        Example::

            {"subagents": {"total_cost_usd": 12.34, "invocations": 7,
                           "by_agent": {"Transcript analyst": {"cost_usd": 9.1, "invocations": 3}}},
             "evals": {"runs": 4, "pairs": 9,
                       "tokens_by_model": {"openai/gpt-5": {"input_tokens": 1e6, ...}}}}
    """
    sidecars: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for dirpath, _dirs, files in os.walk(os.path.abspath(root)):
        for fn in files:
            path = os.path.join(dirpath, fn)
            if fn.startswith("cost_") and fn.endswith(".json"):
                data = _load(path)
                if data and data.get("prism_cost") == 1:
                    data["_path"] = path
                    sidecars.append(data)
            elif fn == "status.json":
                data = _load(path)
                if data and data.get("schema") and "pairs" in data:
                    data["_path"] = path
                    runs.append(data)

    by_agent: dict[str, dict[str, Any]] = defaultdict(lambda: {"cost_usd": 0.0, "invocations": 0, "turns": 0})
    total = 0.0
    for s in sidecars:
        cost = float(s.get("total_cost_usd") or 0.0)
        total += cost
        a = by_agent[s.get("agent", "?")]
        a["cost_usd"] += cost
        a["invocations"] += 1
        a["turns"] += int(s.get("num_turns") or 0)

    tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pair_count = 0
    run_summaries = []
    for r in runs:
        run_tokens = 0
        for pid, e in r.get("pairs", {}).items():
            pair_count += 1
            for model, mu in (e.get("model_usage") or {}).items():
                for k in ("input_tokens", "output_tokens", "total_tokens", "input_tokens_cache_read", "input_tokens_cache_write", "reasoning_tokens"):
                    v = mu.get(k)
                    if v:
                        tokens[model][k] += int(v)
                run_tokens += int(mu.get("total_tokens") or 0)
        run_summaries.append({"run_id": r.get("run_id"), "state": r.get("state"), "pairs": len(r.get("pairs", {})), "total_tokens": run_tokens, "path": r["_path"]})

    return {
        "subagents": {
            "total_cost_usd": round(total, 4),
            "invocations": len(sidecars),
            "by_agent": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in sorted(by_agent.items())},
            "sidecars": [{"agent": s.get("agent"), "model": s.get("model"), "cost_usd": s.get("total_cost_usd"), "turns": s.get("num_turns"), "subtype": s.get("subtype"), "path": s["_path"]} for s in sidecars],
        },
        "evals": {
            "runs": len(runs),
            "pairs": pair_count,
            "tokens_by_model": {m: dict(v) for m, v in sorted(tokens.items())},
            "run_summaries": run_summaries,
        },
    }


def render(summary: dict[str, Any]) -> str:
    """Human-readable rendering of `collect`'s output."""
    s, e = summary["subagents"], summary["evals"]
    lines = [f"sub-agent spend: ${s['total_cost_usd']:.2f} over {s['invocations']} invocation(s)"]
    for agent, v in s["by_agent"].items():
        lines.append(f"  {agent:<24} ${v['cost_usd']:>8.2f}  {v['invocations']} invocation(s), {v['turns']} turns")
    lines.append(f"eval usage: {e['runs']} run(s), {e['pairs']} condition-model pair(s)")
    for model, t in e["tokens_by_model"].items():
        lines.append(f"  {model:<40} in={t.get('input_tokens', 0):>10,} out={t.get('output_tokens', 0):>9,} total={t.get('total_tokens', 0):>11,}")
    if not e["tokens_by_model"]:
        lines.append("  (no eval usage recorded)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sum sub-agent cost sidecars and eval token usage.")
    parser.add_argument("investigation", help="investigations/<name> directory")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.investigation):
        print(f"not a directory: {args.investigation}", file=sys.stderr)
        return 1
    summary = collect(args.investigation)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
