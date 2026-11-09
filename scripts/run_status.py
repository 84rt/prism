#!/usr/bin/env python3
"""Where a Prism investigation is, read from its files: for a terminal or the Control Panel.

The Control Panel's Prism run tab draws a run's pipeline, current step,
helpers and cost from this script's ``--json`` answer, polling it every few
seconds while a run is open. Keeping the reading inside Prism means a change
to Prism's outputs is fixed in the same commit and caught by Prism's tests.

**Read-only, by contract.** The script never writes anywhere, takes no lock
and starts no process: it reads the investigation folder, the helpers'
receipts (checking their processes with signal 0, which delivers nothing)
and, for cost, the lead's Claude Code session files via `spend.py`. It must
stay well under a second, since the panel runs it every 5 seconds.

What it reports
---------------
- ``pipeline``: the pipeline drawn for this run, from ``.claude/docs/pipelines.json``.
- ``state``: ``finished`` (``FINDINGS-REPORT.md`` exists), ``suspended``
  (``RESUME.md`` exists) or ``running``.
- ``current``: phase, box, ``isSide`` and ``round``, or null when the files
  don't settle it. A running helper's tag and role give the box, provided the
  newest log section agrees on the phase; otherwise the lead's newest log
  heading does. The mapping lives in the data file, not here.
- ``explanation``: the top-ranked live candidate (model forensics) or the
  latest hypothesis (science of evals), trimmed.
- ``helpers``: one per receipt, with Executor sample counts from its run's
  ``status.json``, which updates as each condition finishes. Inspect's live
  sample store would count per sample, but it is an SQLite database in WAL
  mode that can't be read without taking a lock, so it is not used.
- ``steps``: one short line per finished phase or round.
- ``cost``: total dollars (helpers plus the lead, as `spend.py` reports them)
  and eval tokens.

The JSON contract (camelCase keys, null for absent, ``version`` 1) is set by
the panel: see the note at the top of the session-tracker mod's
``tests/fixtures/prism-status-diagnose.ts`` in the dotfiles repo.

Usage
-----
    uv run --no-project python scripts/run_status.py investigations/<name> [--json] [--pipeline diagnose|orchestrate]

The script uses the standard library only, so the panel runs it with the demo
folder's own Python. ``--pipeline`` is a hint for a run too new for its files
to say which pipeline it is.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import launch_subagent
import ledger_to_json
import spend

STATUS_VERSION = 1

# Unresolved path on purpose, as in `launch_subagent.py`: in an installed demo
# folder this script is a symlink, and the data file is linked beside it.
TARGET_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINES_PATH = os.path.join(TARGET_ROOT, ".claude", "docs", "pipelines.json")

# Longest explanation or step text sent, in characters: the panel is narrow
MAX_EXPLANATION = 200
MAX_STEP = 70

# A candidate id in a ledger change line, e.g. `C2` or `X13`
CANDIDATE_ID = re.compile(r"^[A-Z]{1,2}\d{1,3}$")


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------


def read_log(path: str) -> list[dict[str, Any]]:
    """Split ``INVESTIGATION-LOG.md`` into its ``## `` sections.

    Parameters
    ----------
    path : str
        The log file; a missing file reads as an empty log.

    Returns
    -------
    list[dict]
        One ``{"title": str, "subs": [{"title": str, "lines": [str]}]}`` per
        ``## `` heading, in order. Text before a section's first ``### `` sits
        in a sub with an empty title. Example::

            [{"title": "Iteration 1", "subs": [{"title": "", "lines": []},
                                               {"title": "Decision", "lines": ["- Continue."]}]}]
    """
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return []
    sections: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append({"title": line[3:].strip(), "subs": [{"title": "", "lines": []}]})
        elif not sections:
            continue
        elif line.startswith("### "):
            sections[-1]["subs"].append({"title": line[4:].strip(), "lines": []})
        else:
            sections[-1]["subs"][-1]["lines"].append(line)
    return sections


def _match(pattern: str, text: str) -> re.Match[str] | None:
    """Match a data-file pattern at the start of `text`, ignoring case."""
    return re.match(pattern, text, re.I)


def map_sections(spec: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a phase (and round, for a loop) to each log section the data file names.

    Parameters
    ----------
    spec : dict
        One pipeline from the data file.
    sections : list[dict]
        `read_log`'s output.

    Returns
    -------
    list[dict]
        The sections that map onto a phase, in log order, each with added
        ``phase`` (an id) and ``round`` (an int, or None outside a loop).
        Sections for the same phase and round (e.g. ``## Iteration 1 —
        Pre-registration`` then ``## Iteration 1``) merge into one, placed
        at the later one's position.
    """
    mapped: list[dict[str, Any]] = []
    for s in sections:
        for rule in spec["phaseHeadings"]:
            m = _match(rule["pattern"], s["title"])
            if m:
                rnd = m.groupdict().get("round")
                entry = {**s, "phase": rule["phase"], "round": int(rnd) if rnd else None}
                same = next((e for e in mapped if (e["phase"], e["round"]) == (entry["phase"], entry["round"])), None)
                if same is not None:
                    mapped.remove(same)
                    entry["subs"] = same["subs"] + entry["subs"]
                mapped.append(entry)
                break
    return mapped


# Symbols the logs use that some terminals draw double-width (East Asian
# "ambiguous" width), which garbles the panel: swapped for plain characters
PLAIN_SYMBOLS = str.maketrans({
    "—": "-", "–": "-", "→": "->", "←": "<-", "↔": "<->", "↑": "up", "↓": "down", "⇒": "=>",
    "…": "...", "≤": "<=", "≥": ">=", "≈": "~", "×": "x", "±": "+/-",
    "“": '"', "”": '"', "‘": "'", "’": "'",
})


def _plain(text: str) -> str:
    """Strip markdown emphasis and code marks, swap wide symbols, and collapse whitespace."""
    return " ".join(re.sub(r"[`*]", "", text).translate(PLAIN_SYMBOLS).split())


def _trim(text: str, limit: int, sentences: bool = True) -> str:
    """Cut text to fit `limit` characters.

    Parameters
    ----------
    text : str
        Plain text.
    limit : int
        Maximum length.
    sentences : bool
        Prefer ending at a sentence break, if one falls in the second half of
        the limit; otherwise, and when False, cut at a word boundary and end
        in "...".

    Returns
    -------
    str
        The cut text. Example: ``_trim("First one. Two three.", 12)`` -> ``"First one."``.
    """
    if len(text) <= limit:
        return text
    ends = [m.end() for m in re.finditer(r"[.;!?](?=\s)", text) if limit // 2 <= m.end() <= limit] if sentences else []
    if ends:
        return text[: ends[-1]]
    cut = text[: limit - 3].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "..."


# ---------------------------------------------------------------------------
# Pipeline and state
# ---------------------------------------------------------------------------


def load_pipelines(path: str = PIPELINES_PATH) -> dict[str, Any]:
    """Read the pipeline data file.

    Parameters
    ----------
    path : str
        ``.claude/docs/pipelines.json``.

    Returns
    -------
    dict
        The ``pipelines`` mapping, keyed ``diagnose`` and ``orchestrate``.
    """
    with open(path) as f:
        return json.load(f)["pipelines"]


def detect_pipeline(inv: str, sections: list[dict[str, Any]], receipts: list[dict[str, Any]], hint: str | None) -> tuple[str, bool]:
    """Work out which pipeline a run follows.

    Parameters
    ----------
    inv : str
        The investigation folder.
    sections : list[dict]
        `read_log`'s output.
    receipts : list[dict]
        The helpers' receipts.
    hint : str | None
        ``--pipeline``, used only when the files say nothing.

    Returns
    -------
    tuple[str, bool]
        The pipeline name, and whether the files settled it (False means a
        hint or the default was used, so no stage should be claimed).
    """
    roles = {r.get("role") for r in receipts}
    titles = [s["title"] for s in sections] + [sub["title"] for s in sections for sub in s["subs"]]
    # ---- Roles and files only one pipeline has ----
    if roles & {"mapper", "designer", "critic"} or os.path.exists(os.path.join(inv, "CANDIDATES.md")) or os.path.isdir(os.path.join(inv, "iterations")):
        return "diagnose", True
    if "explorer" in roles or any(t.startswith("Hypothesis this iteration") for t in titles):
        return "orchestrate", True
    # ---- Headings only the forensics log has ----
    if any(re.match(r"Phase [0-5]\b", t) for t in titles):
        return "diagnose", True
    return (hint or "diagnose"), False


def run_state(inv: str) -> str:
    """``finished``, ``suspended`` or ``running``, from the folder's marker files.

    A report wins over a resume note: the run did finish.
    """
    if os.path.exists(os.path.join(inv, "FINDINGS-REPORT.md")):
        return "finished"
    if os.path.exists(os.path.join(inv, "RESUME.md")):
        return "suspended"
    return "running"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def helper_state(receipt: dict[str, Any]) -> str:
    """A receipt's state in the panel's terms.

    Parameters
    ----------
    receipt : dict
        A helper's receipt.

    Returns
    -------
    str
        ``launching`` or ``running`` while its process lives; ``finished``
        for a clean exit; ``failed`` for a launch failure, a non-zero exit, or
        a process that died without recording one (e.g. a stopped Executor,
        whose receipt still says "running").
    """
    state = receipt.get("state")
    if state == "failed":
        return "failed"
    if state == "finished":
        return "finished" if receipt.get("exit_code") == 0 else "failed"
    if launch_subagent.exit_code_for(receipt) == launch_subagent.EXIT_DEAD:
        return "failed"
    return "launching" if state == "launching" else "running"


def _load_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def executor_samples(receipt: dict[str, Any]) -> dict[str, int] | None:
    """Samples done and in total for an Executor invocation.

    Parameters
    ----------
    receipt : dict
        The Executor's receipt; its batch run's ``status.json`` sits under
        ``runs/`` beside it.

    Returns
    -------
    dict | None
        ``{"done": n, "total": m}``, or None before the run has a status file
        or when a condition's size can't be known. A condition still running
        counts 0 done until it finishes, since the status file updates per
        condition. Its size, before then, is the request's ``epochs`` times
        its sample ids, when the request names them.
    """
    receipt_path = receipt.get("receipt")
    runs_dir = os.path.join(os.path.dirname(receipt_path), "runs") if receipt_path else None
    if not runs_dir or not os.path.isdir(runs_dir):
        return None

    # ---- The batch run this invocation started: the newest begun inside its lifetime ----
    started, finished = receipt.get("started_at") or "", receipt.get("finished_at")
    best: dict[str, Any] | None = None
    for name in os.listdir(runs_dir):
        status = _load_json(os.path.join(runs_dir, name, "status.json"))
        if not status or not isinstance(status.get("pairs"), dict):
            continue
        at = status.get("started_at") or ""
        if at < started or (finished and at > finished):
            continue
        if best is None or at > (best.get("started_at") or ""):
            best = status
    if best is None:
        return None

    # ---- Each condition's size: recorded once run, else estimated from the request ----
    overrides = (_load_json(receipt.get("request")) or {}).get("overrides") or {}
    ids = overrides.get("sample_id")
    n_ids = 1 if isinstance(ids, (str, int)) else len(ids) if isinstance(ids, list) else None
    estimate = overrides["epochs"] * n_ids if n_ids and isinstance(overrides.get("epochs"), int) else None
    done = total = 0
    for pair in best["pairs"].values():
        size = pair.get("samples_total")
        if size is None:
            size = estimate
        if size is None:
            return None
        total += int(size)
        done += int(pair.get("samples_completed") or 0)
    return {"done": done, "total": total}


def collect_helpers(receipts: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """One panel entry per helper receipt, oldest first.

    Parameters
    ----------
    receipts : list[dict]
        Receipts, oldest first (`launch_subagent.list_receipts`).
    spec : dict
        The run's pipeline from the data file, for display names.

    Returns
    -------
    list[dict]
        ``{role, tag, state, startedAt, finishedAt?, samples?}`` per helper,
        plus private ``_role`` (the launcher's role) and ``_mode`` (a Critic's
        request mode) for `locate`; receipts with no start time are left out.
    """
    out = []
    for r in receipts:
        if not r.get("started_at"):
            continue
        role = r.get("role") or "?"
        h: dict[str, Any] = {
            "role": spec["roles"].get(role, role.title()),
            "tag": str(r.get("tag") or ""),
            "state": helper_state(r),
            "startedAt": r["started_at"],
            "_role": role,
            "_mode": (_load_json(r.get("request")) or {}).get("mode") if role == "critic" else None,
        }
        if r.get("finished_at"):
            h["finishedAt"] = r["finished_at"]
        if role == "executor":
            samples = executor_samples(r)
            if samples is not None:
                h["samples"] = samples
        out.append(h)
    return out


# ---------------------------------------------------------------------------
# Where the run is
# ---------------------------------------------------------------------------


def _round_label(n: int | None, cap: int | None) -> str | None:
    """``"2 of 4"`` with a cap, ``"2"`` without, None outside a loop."""
    if n is None:
        return None
    return f"{n} of {cap}" if cap else str(n)


def iteration_cap(sections: list[dict[str, Any]]) -> int | None:
    """The iteration cap recorded in the Agreed Scope, or None (none, or not found).

    Matches lines such as ``- Iteration cap: **4**``; ``**none**`` gives None.
    """
    for s in sections:
        if s["title"].lower().startswith("agreed scope"):
            text = "\n".join(line for sub in s["subs"] for line in sub["lines"])
            m = re.search(r"iteration cap\W{0,6}(\d+)\b", text, re.I)
            return int(m.group(1)) if m else None
    return None


def locate(spec: dict[str, Any], mapped: list[dict[str, Any]], helpers: list[dict[str, Any]], state: str, cap: int | None) -> dict[str, Any] | None:
    """The phase and box the run is at, or None when the files don't settle it.

    Parameters
    ----------
    spec : dict
        The run's pipeline from the data file.
    mapped : list[dict]
        `map_sections`'s output.
    helpers : list[dict]
        `collect_helpers`'s output.
    state : str
        `run_state`'s answer.
    cap : int | None
        The iteration cap, for the round label.

    Returns
    -------
    dict | None
        ``{"phase", "box", "isSide"?, "round"?}``. ``box`` is ``""`` in a
        phase with no boxes; ``isSide`` means the named box's side box is
        working.
    """
    order = [p["id"] for p in spec["phases"]]
    phases = {p["id"]: p for p in spec["phases"]}
    if state == "finished":
        return {"phase": order[-1], "box": ""}
    if not mapped:
        return None
    last = mapped[-1]

    # ---- A helper at work: its tag, role and mode name the box ----
    running = [h for h in helpers if h["state"] in ("launching", "running")]
    if running:
        h = max(running, key=lambda x: x["startedAt"])
        for rule in spec["helperBoxes"]:
            m = _match(rule["tag"], h["tag"])
            if not m or rule.get("role", h["_role"]) != h["_role"] or rule.get("mode", h["_mode"]) != h["_mode"]:
                continue
            # The log lags a launch by at most one phase; a bigger gap means a
            # stale receipt or an unmapped step, so claim nothing
            gap = order.index(rule["phase"]) - order.index(last["phase"])
            if gap not in (0, 1):
                return None
            rnd = m.groupdict().get("round")
            out: dict[str, Any] = {"phase": rule["phase"], "box": rule["box"]}
            if rule.get("side"):
                out["isSide"] = True
            if phases[rule["phase"]].get("loop") and rnd:
                out["round"] = _round_label(int(rnd), cap)
            return out
        return None

    # ---- The lead at work: its newest log heading that names a box ----
    phase = phases[last["phase"]]
    out = {"phase": phase["id"], "box": ""}
    if phase.get("loop") and last["round"] is not None:
        out["round"] = _round_label(last["round"], cap)
    if not phase["boxes"]:
        return out
    for sub in reversed(last["subs"]):
        for rule in spec["logBoxes"]:
            if rule["phase"] == phase["id"] and sub["title"] and _match(rule["heading"], sub["title"]):
                out["box"] = rule["box"]
                return out
    return None


# ---------------------------------------------------------------------------
# Explanation and steps
# ---------------------------------------------------------------------------


def leading_explanation(name: str, inv: str, mapped: list[dict[str, Any]]) -> str | None:
    """What the run is currently chasing, in a sentence or two.

    Parameters
    ----------
    name : str
        The pipeline.
    inv : str
        The investigation folder.
    mapped : list[dict]
        `map_sections`'s output.

    Returns
    -------
    str | None
        Model forensics: ``"<id> <slug>: <statement>"`` for the rank-1 live
        candidate in ``CANDIDATES.md`` (the first live one when no entry says
        rank 1). Science of evals: the latest hypothesis's statement. None
        when there is none yet.
    """
    if name == "diagnose":
        try:
            with open(os.path.join(inv, "CANDIDATES.md")) as f:
                live = ledger_to_json.parse_ledger(f.read()).candidates
        except OSError:
            return None
        if not live:
            return None
        top = next((c for c in live if re.search(r"\brank\s*1\b", c.trailer, re.I)), live[0])
        statement = _plain(top.fields.get("statement", ""))
        return _trim(f"{top.id} {top.slug}: {statement}" if statement else f"{top.id} {top.slug}", MAX_EXPLANATION)

    for s in reversed(mapped):
        for sub in reversed(s["subs"]):
            if sub["title"].lower().startswith("hypothesis this iteration"):
                lines = [ln for ln in sub["lines"] if ln.strip()]
                stated = next((ln for ln in lines if re.match(r"-\s+\*Statement\*", ln)), lines[0] if lines else "")
                text = _plain(re.sub(r"^-\s+\*Statement\*\s*:?", "", stated))
                return _trim(text, MAX_EXPLANATION) or None
    return None


def _ledger_ids(lines: list[str], label: str) -> list[str]:
    """Candidate ids on a ``- <label>:`` line of a Ledger Delta block, in order."""
    ids: list[str] = []
    for ln in lines:
        if re.match(rf"-\s+\*{{0,2}}{label}\*{{0,2}}\s*:", ln, re.I):
            ids += [t for t in re.findall(r"`([^`]+)`", ln) if CANDIDATE_ID.match(t) and t not in ids]
    return ids


def _ledger_change(section: dict[str, Any]) -> str | None:
    """``"C2 out, X3 added"`` from a section's Ledger Delta blocks; None without one."""
    lines = [ln for sub in section["subs"] if sub["title"].lower().startswith("ledger delta") for ln in sub["lines"]]
    if not lines:
        return None
    out, added = _ledger_ids(lines, "Eliminated"), _ledger_ids(lines, "Added")
    parts = ([", ".join(out) + " out"] if out else []) + ([", ".join(added) + " added"] if added else [])
    return "; ".join(parts) if parts else "no candidate out"


def _decision_headline(section: dict[str, Any]) -> str | None:
    """The opening line of a section's Decision block, without its asides.

    The first line that opens in bold, skipping bold labels such as
    ``**ESCALATION NOTE (...):**``, with parentheticals removed: e.g.
    ``**Iterate (iteration 3)** — AM leaking scenario`` -> ``"Iterate - AM leaking scenario"``.
    None when the block has no such line.
    """
    for sub in section["subs"]:
        if sub["title"].lower().startswith("decision"):
            for ln in sub["lines"]:
                m = re.match(r"\s*\*\*(.+?)\*\*", ln)
                if not m or m.group(1).rstrip().endswith(":"):
                    continue
                text = ln
                while re.search(r"\s*\([^()]*\)", text):
                    text = re.sub(r"\s*\([^()]*\)", "", text)
                return _plain(text).rstrip(".")
    return None


def finished_steps(name: str, spec: dict[str, Any], mapped: list[dict[str, Any]], state: str) -> list[str]:
    """One short line per finished phase or round.

    Parameters
    ----------
    name : str
        The pipeline.
    spec : dict
        The run's pipeline from the data file.
    mapped : list[dict]
        `map_sections`'s output.
    state : str
        `run_state`'s answer: in a finished run every section is done.

    Returns
    -------
    list[str]
        E.g. ``["Scoping done", "Reading: X1, X2 added", "Round 1: C2 out; X3 added"]``.
        A loop round counts as finished once it has a Decision block; any
        other section once a later one starts. The report phase adds no step.
    """
    phases = {p["id"]: p for p in spec["phases"]}
    report_phase = spec["phases"][-1]["id"]
    steps: list[str] = []
    for i, s in enumerate(mapped):
        phase = phases[s["phase"]]
        has_decision = any(sub["title"].lower().startswith("decision") for sub in s["subs"])
        done = state == "finished" or i < len(mapped) - 1 or (phase.get("loop") and has_decision)
        if not done or s["phase"] == report_phase:
            continue

        # ---- A round: what changed, or what was decided ----
        if phase.get("loop") and s["round"] is not None:
            detail = _ledger_change(s) if name == "diagnose" else _decision_headline(s)
            steps.append(_trim(f"Round {s['round']}: {detail}" if detail else f"Round {s['round']} done", MAX_STEP))
        # ---- Another phase: once per phase, with any ledger change ----
        elif not any(st.startswith(phase["title"]) for st in steps):
            detail = _ledger_change(s) if name == "diagnose" else None
            if phase["id"] == spec["phases"][0]["id"]:
                steps.append(f"{phase['title']}: scope agreed")
            else:
                steps.append(_trim(f"{phase['title']}: {detail}" if detail else f"{phase['title']} done", MAX_STEP))
    return steps


# ---------------------------------------------------------------------------
# The whole answer
# ---------------------------------------------------------------------------


def _iso_mtime(path: str) -> str | None:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


def build_status(inv: str, hint: str | None = None, pipelines: dict[str, Any] | None = None, projects_dir: str | None = None) -> dict[str, Any]:
    """The status answer for one investigation.

    Parameters
    ----------
    inv : str
        ``investigations/<name>``.
    hint : str | None
        The pipeline to assume when the files don't say.
    pipelines : dict | None
        The data file's pipelines; read from ``PIPELINES_PATH`` when None.
    projects_dir : str | None
        Claude Code's projects folder, for the lead's cost (`spend.py`).

    Returns
    -------
    dict
        The panel's ``version`` 1 status, e.g.::

            {"version": 1, "investigation": "sandbagging_r2",
             "pipeline": {"name": "diagnose", "title": "Model forensics", "phases": [...]},
             "state": "running", "current": {"phase": "3", "box": "run", "round": "2"},
             "explanation": "C1 deployment_goal_inference: ...",
             "helpers": [...], "steps": ["Round 1: C2 out; X3 added"],
             "cost": {"usd": 64.42, "evalTokens": 23129877}, "report": null,
             "startedAt": "2026-11-08T08:02:50+00:00"}
    """
    inv = os.path.abspath(inv)
    pipelines = pipelines or load_pipelines()
    sections = read_log(os.path.join(inv, "INVESTIGATION-LOG.md"))
    receipts = launch_subagent.list_receipts(inv)
    name, sure = detect_pipeline(inv, sections, receipts, hint)
    spec = pipelines[name]
    mapped = map_sections(spec, sections)
    state = run_state(inv)
    helpers = collect_helpers(receipts, spec)

    # ---- Cost: helpers plus lead in dollars; eval usage in tokens ----
    summary = spend.collect(inv, projects_dir)
    eval_tokens = sum(int(t.get("total_tokens") or 0) for t in summary["evals"]["tokens_by_model"].values())
    cost: dict[str, Any] = {"usd": round(summary["total_cost_usd"], 2)}
    if eval_tokens:
        cost["evalTokens"] = eval_tokens

    status: dict[str, Any] = {
        "version": STATUS_VERSION,
        "investigation": os.path.basename(inv),
        "pipeline": {"name": name, "title": spec["title"], "phases": spec["phases"]},
        "state": state,
        "current": locate(spec, mapped, helpers, state, iteration_cap(sections)) if sure else None,
        "explanation": leading_explanation(name, inv, mapped),
        "helpers": [{k: v for k, v in h.items() if not k.startswith("_")} for h in helpers],
        "steps": finished_steps(name, spec, mapped, state),
        "cost": cost,
        "report": "FINDINGS-REPORT.md" if state == "finished" else None,
    }
    if helpers:
        status["startedAt"] = helpers[0]["startedAt"]
    if state == "finished":
        status["finishedAt"] = _iso_mtime(os.path.join(inv, "FINDINGS-REPORT.md"))
    return status


def render(status: dict[str, Any]) -> str:
    """A short readable summary of `build_status`'s answer, for a terminal."""
    phases = {p["id"]: p for p in status["pipeline"]["phases"]}
    lines = [f"{status['investigation']}: {status['pipeline']['title']}, {status['state']}"]

    # ---- Stage ----
    cur = status["current"]
    if cur is None:
        lines.append("stage: unknown")
    else:
        phase = phases[cur["phase"]]
        box = next((b for b in phase["boxes"] if b["id"] == cur["box"]), None)
        where = f"phase {phase['id']} {phase['title']}"
        if box:
            where += f", {box['side']['title'] if cur.get('isSide') and box.get('side') else box['title']}"
        if cur.get("round"):
            where += f", round {cur['round']}"
        lines.append(f"stage: {where}")
    if status["explanation"]:
        lines.append(f"leading: {status['explanation']}")

    # ---- Helpers at work, steps, cost, report ----
    for h in status["helpers"]:
        if h["state"] in ("launching", "running"):
            samples = f", {h['samples']['done']}/{h['samples']['total']} samples" if h.get("samples") else ""
            lines.append(f"working: {h['role']} {h['tag']} since {h['startedAt']}{samples}")
    lines.append(f"helpers: {len(status['helpers'])} launched")
    lines += [f"  {s}" for s in status["steps"]]
    tokens = status["cost"].get("evalTokens")
    lines.append(f"cost: ${status['cost']['usd']:.2f}" + (f" plus {tokens:,} eval tokens" if tokens else ""))
    if status["report"]:
        lines.append(f"report: {status['report']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only status of a Prism investigation.")
    parser.add_argument("investigation", help="investigations/<name> directory")
    parser.add_argument("--json", action="store_true", help="one JSON object, for the Control Panel")
    parser.add_argument("--pipeline", choices=("diagnose", "orchestrate"), help="pipeline to assume when the files don't say")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.investigation):
        print(f"not a directory: {args.investigation}", file=sys.stderr)
        return 1
    status = build_status(args.investigation, args.pipeline)
    print(json.dumps(status) if args.json else render(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
