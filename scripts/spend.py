"""Running spend total for an investigation.

Sums three telemetry sources:

- **Sub-agent cost sidecars** — ``cost_*.json`` files written by
  ``subagents/runner.py`` from the Agent SDK's result message (USD, turns,
  tokens) for every invocation launched through ``scripts/launch_subagent.py``.
- **Lead-agent sessions** — the Claude Code session files of the lead agent
  (Orchestrator or Diagnostician) that ran this investigation, priced at API
  rates from the token usage of each model call. A session counts when it was
  started with ``/orchestrate`` or ``/diagnose`` and wrote or edited a file
  inside this investigation's directory, so a resumed lead counts and a session
  that only read the results does not. The figure is what the lead would cost
  on the API, which over-states real spend when the lead ran on a subscription.
- **Eval token usage** — per condition-model pair, recorded in each batch
  run's ``status.json`` by ``scripts/execute_evals.py`` from the eval log
  header's ``model_usage``: every attempt's (``usage_attempts`` — preflight
  probe, retest, and each full-run attempt including retried ones), or the
  final attempt's alone in a status file written before that field existed.

Eval spend is reported in tokens per model, not USD: the runner has no price
table and the target models vary per investigation. Both lead agents consume
these figures: the Diagnostician's budget exit (``diagnostician_responsibilities.md``
§Stopping rule) and the Orchestrator's spend check before each Decision
(``orchestrator_responsibilities.md`` Step 2j). The headline total is sub-agents
plus lead; both parts are shown.

Known limitation — the total under-counts: eval usage is not priced in USD,
and the Transcript Analyst's own scanner and validator model calls (made from
inside its scans, not through the SDK result the sidecar records) are not
captured at all. Callers keep headroom below the cap rather than spending to
it. Dollar pricing for eval usage and capture of the Analyst's scanner spend
are future work.

Usage
-----
    uv run python scripts/spend.py investigations/<name> [--json]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any

# API prices for the models a lead agent runs on, in USD per million tokens:
# (input, output, cache read). Cache writes are priced from input: 1.25x for
# the 5-minute cache, 2x for the 1-hour cache Claude Code uses for a main
# session. A model missing here is reported in tokens with no dollar figure,
# never guessed at. Update with each model release.
LEAD_PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
}
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
# Fast mode doubles the per-token price (Opus 5.5: $8 / $40); assumed to apply
# to cache reads and writes alike.
FAST_MODE_MULTIPLIER = 2.0

# What marks a session as an investigation's lead: the skill that started it,
# and a file it wrote inside the investigation directory
_LEAD_SKILL_MARKERS = ("<command-name>/orchestrate</command-name>", "<command-name>/diagnose</command-name>")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit")


def _load(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def default_projects_dir() -> str:
    """Where Claude Code keeps per-project session files.

    Returns
    -------
    str
        ``$CLAUDE_CONFIG_DIR/projects``, or ``~/.claude/projects`` when unset.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(config, "projects")


def _project_slug(path: str) -> str:
    """Claude Code's folder name for a project path.

    Parameters
    ----------
    path : str
        Absolute project directory.

    Returns
    -------
    str
        The path with every non-alphanumeric character replaced by ``-``,
        e.g. ``/workspaces/mf-demos`` -> ``-workspaces-mf-demos``.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _price_call(model: str, usage: dict[str, Any]) -> float | None:
    """API price of one model call from its usage block.

    Parameters
    ----------
    model : str
        The model the call ran on.
    usage : dict
        The message's ``usage`` (input, output, cache read and write tokens,
        with the write split by cache lifetime when the CLI recorded it).

    Returns
    -------
    float | None
        USD, or None for a model missing from ``LEAD_PRICES_PER_MTOK``.
    """
    prices = LEAD_PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    inp, out, read = prices

    # Cache writes split by lifetime; an unsplit figure is taken as 5-minute
    writes = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation") or {}
    w1h = split.get("ephemeral_1h_input_tokens") or 0
    w5m = split.get("ephemeral_5m_input_tokens") or 0
    if w1h + w5m == 0:
        w5m = writes

    cost = (
        (usage.get("input_tokens") or 0) * inp
        + (usage.get("output_tokens") or 0) * out
        + (usage.get("cache_read_input_tokens") or 0) * read
        + w5m * inp * CACHE_WRITE_5M_MULTIPLIER
        + w1h * inp * CACHE_WRITE_1H_MULTIPLIER
    ) / 1e6
    if usage.get("speed") == "fast":
        cost *= FAST_MODE_MULTIPLIER
    return cost


def _read_lead_session(path: str, investigation: str) -> dict[str, Any] | None:
    """Total one session file's usage if it is this investigation's lead.

    Parameters
    ----------
    path : str
        A Claude Code ``<session>.jsonl`` file.
    investigation : str
        Absolute path of the investigation directory.

    Returns
    -------
    dict | None
        ``{"session": id, "path": path, "calls": n, "cost_usd": x,
        "unpriced_calls": k, "tokens_by_model": {...}}``, where ``cost_usd``
        covers the priced calls only; or None when the session is not a lead
        session that wrote into the investigation.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None

    # ---------- Is this the investigation's lead? ----------
    rel = "investigations/" + os.path.basename(investigation) + "/"
    is_lead_skill = any(m in line for line in lines for m in _LEAD_SKILL_MARKERS)
    if not is_lead_skill:
        return None
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    wrote_here = False
    for e in entries:
        if e.get("type") != "assistant" or e.get("entrypoint") not in (None, "cli"):
            continue
        for block in (e.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in _WRITE_TOOLS:
                target = str((block.get("input") or {}).get("file_path") or "")
                if target.startswith(investigation + "/") or rel in target:
                    wrote_here = True
    if not wrote_here:
        return None

    # ---------- Sum each model call once ----------
    # Claude Code writes one line per content block, each carrying the whole
    # message's usage, so calls are keyed by message id
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for e in entries:
        msg = e.get("message") or {}
        if e.get("type") == "assistant" and msg.get("usage") and msg.get("id"):
            calls[msg["id"]] = (msg.get("model") or "?", msg["usage"])

    tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cost = 0.0
    unpriced_calls = 0
    for model, usage in calls.values():
        t = tokens[model]
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            t[k] += int(usage.get(k) or 0)
        c = _price_call(model, usage)
        if c is None:
            unpriced_calls += 1
        else:
            cost += c
    return {
        "session": os.path.splitext(os.path.basename(path))[0],
        "path": path,
        "calls": len(calls),
        "cost_usd": round(cost, 4),
        "unpriced_calls": unpriced_calls,
        "tokens_by_model": {m: dict(v) for m, v in sorted(tokens.items())},
    }


def collect_lead(root: str, projects_dir: str | None = None) -> dict[str, Any]:
    """Find and price the lead-agent sessions of one investigation.

    Parameters
    ----------
    root : str
        ``<repo>/investigations/<name>``; the sessions are looked for under the
        Claude Code project folder of ``<repo>``.
    projects_dir : str | None
        Claude Code's projects folder; ``default_projects_dir()`` when None.

    Returns
    -------
    dict
        ``{"total_cost_usd": x, "sessions": [...], "unpriced_models": [...]}``;
        calls on an unpriced model add nothing to the total and their model
        is listed instead.
    """
    investigation = os.path.abspath(root)
    repo = os.path.dirname(os.path.dirname(investigation))
    folder = os.path.join(projects_dir or default_projects_dir(), _project_slug(repo))
    sessions = []
    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        s = _read_lead_session(path, investigation)
        if s is not None:
            sessions.append(s)
    total = sum(s["cost_usd"] for s in sessions)
    unpriced = sorted({m for s in sessions for m in s["tokens_by_model"] if m not in LEAD_PRICES_PER_MTOK})
    return {"total_cost_usd": round(total, 4), "sessions": sessions, "unpriced_models": unpriced}


def collect(root: str, projects_dir: str | None = None) -> dict[str, Any]:
    """Walk an investigation directory and total its telemetry.

    Parameters
    ----------
    root : str
        ``investigations/<name>`` (any directory; walked recursively).
    projects_dir : str | None
        Claude Code's projects folder, where the lead sessions are found;
        ``default_projects_dir()`` when None.

    Returns
    -------
    dict
        ``total_cost_usd``: sub-agents plus lead; ``subagents``: total USD,
        count, per-role totals and the list of sidecars; ``lead``: see
        `collect_lead`; ``evals``: per-model token totals and per-run
        summaries. Example::

            {"total_cost_usd": 40.34,
             "subagents": {"total_cost_usd": 12.34, "invocations": 7,
                           "by_agent": {"Transcript analyst": {"cost_usd": 9.1, "invocations": 3}}},
             "lead": {"total_cost_usd": 28.0, "sessions": [...], "unpriced_models": []},
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
        for e in r.get("pairs", {}).values():
            pair_count += 1
            # All attempts where recorded; older status files kept only the
            # final attempt's usage, which is then the best figure there is.
            attempts = e.get("usage_attempts")
            if attempts is None:
                attempts = [e.get("model_usage") or {}]
            for usage in attempts:
                for model, mu in (usage or {}).items():
                    for k in ("input_tokens", "output_tokens", "total_tokens", "input_tokens_cache_read", "input_tokens_cache_write", "reasoning_tokens"):
                        v = mu.get(k)
                        if v:
                            tokens[model][k] += int(v)
                    run_tokens += int(mu.get("total_tokens") or 0)
        run_summaries.append({"run_id": r.get("run_id"), "state": r.get("state"), "pairs": len(r.get("pairs", {})), "total_tokens": run_tokens, "path": r["_path"]})

    lead = collect_lead(root, projects_dir)
    return {
        "total_cost_usd": round(total + lead["total_cost_usd"], 4),
        "subagents": {
            "total_cost_usd": round(total, 4),
            "invocations": len(sidecars),
            "by_agent": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in sorted(by_agent.items())},
            "sidecars": [{"agent": s.get("agent"), "model": s.get("model"), "cost_usd": s.get("total_cost_usd"), "turns": s.get("num_turns"), "subtype": s.get("subtype"), "path": s["_path"]} for s in sidecars],
        },
        "lead": lead,
        "evals": {
            "runs": len(runs),
            "pairs": pair_count,
            "tokens_by_model": {m: dict(v) for m, v in sorted(tokens.items())},
            "run_summaries": run_summaries,
        },
    }


def render(summary: dict[str, Any]) -> str:
    """Human-readable rendering of `collect`'s output."""
    s, e, ld = summary["subagents"], summary["evals"], summary["lead"]
    lines = [f"total spend: ${summary['total_cost_usd']:.2f} (sub-agents ${s['total_cost_usd']:.2f} + lead ${ld['total_cost_usd']:.2f}; eval usage below, in tokens)"]
    lines.append(f"sub-agent spend: ${s['total_cost_usd']:.2f} over {s['invocations']} invocation(s)")
    for agent, v in s["by_agent"].items():
        lines.append(f"  {agent:<24} ${v['cost_usd']:>8.2f}  {v['invocations']} invocation(s), {v['turns']} turns")
    lines.append(f"lead spend (at API prices): ${ld['total_cost_usd']:.2f} over {len(ld['sessions'])} session(s)")
    for sess in ld["sessions"]:
        unpriced = f" ({sess['unpriced_calls']} unpriced)" if sess["unpriced_calls"] else ""
        lines.append(f"  {sess['session']}  ${sess['cost_usd']:.2f}  {sess['calls']} model calls{unpriced}")
    if ld["unpriced_models"]:
        lines.append(f"  no price for: {', '.join(ld['unpriced_models'])} (tokens only; add to LEAD_PRICES_PER_MTOK)")
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
