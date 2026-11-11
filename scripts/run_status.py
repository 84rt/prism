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
  ``status.json``. The eval runner keeps a running condition's count current
  from its log, which Inspect saves every ten finished samples or fewer.
  Inspect's live sample store would count per sample, but it is an SQLite
  database in WAL mode that can't be read without taking a lock, so it is
  not used.
- ``steps``: one short line per finished phase or round.
- ``cost``: total dollars as `spend.py` totals them (helpers, the lead, evals
  and the Analyst's own calls), and the eval tokens already inside that total.

Fuller material for the run tab, all optional (older panels ignore it):

- ``question``: the Agreed Scope's explanandum or research question, in full.
- ``explanationFull``: the text ``explanation`` is cut from.
- ``stepDetails``: ``{title, detail}`` per ``steps`` line, uncut.
- ``timings``: estimated start and finish of each phase reached, with each
  round and box (see `timings` for the sources and rules). Unknown times are
  left out rather than guessed.
- ``task`` on a helper: a short excerpt of its brief, where its request has one.

The run tab's third round, also optional:

- On a helper, where it sits: ``phase``, ``round`` and ``box`` for one on a
  pipeline box (see `pipeline_place`); ``side`` and ``purpose`` for a side
  helper, one no pipeline box claims (such as a construal-check pilot),
  which never moves ``current``.
- ``current.between``: ``{from, to}`` while the lead works after one
  helper's box and before the next helper's has started (`between_boxes`).
- ``stepDetails`` and ``steps`` gain a step per box whose helpers have all
  finished, until its round or phase is written up.
- ``usd`` on each phase, round and box in ``timings``, ``leadApprox`` where
  it includes a share of the lead's own cost (`add_costs`).
- ``--box <phase>[:<round>]:<box>`` answers with one box's material
  instead: its helpers' briefs and reports and the lead's log for it
  (`box_material`), for the panel to summarise when the box is clicked.

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

# A helper's brief, excerpted: its request's first prose field among these
TASK_FIELDS = ("topic", "hypothesis", "behaviour_description", "evidence_summary")
MAX_TASK = 300

# One box's material (``--box``), cut: each helper's brief and report, and the lead's log
MAX_MATERIAL = 1500
MAX_BOX_LOG = 4000

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
    """Match a data-file pattern at the start of `text`, ignoring case.

    Parameters
    ----------
    pattern : str
        A regular expression from the pipeline data file.
    text : str
        A log heading.

    Returns
    -------
    re.Match | None
        The match, whose named groups (``round``) the callers read.
    """
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
    """Strip markdown emphasis and code marks, swap wide symbols, and collapse whitespace.

    Parameters
    ----------
    text : str
        A line from the log.

    Returns
    -------
    str
        e.g. ``"**C2** → refuted"`` becomes ``"C2 -> refuted"``.
    """
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
    """Whether a run is going, paused or done, from the folder's marker files.

    A report wins over a resume note: the run did finish.

    Parameters
    ----------
    inv : str
        The investigation folder.

    Returns
    -------
    str
        ``finished``, ``suspended`` or ``running``.
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
    """Read a JSON object from a file that may be missing, half-written or not an object.

    Parameters
    ----------
    path : str | None
        The file; None reads nothing.

    Returns
    -------
    dict | None
        The object, or None for any of those cases.
    """
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
        counts the samples its log held at Inspect's last save of it (the eval
        runner updates that every 20 seconds or so). Its size, until it
        finishes, is the plan its log's header names, or before that the
        request's ``epochs`` times its sample ids (a list, or a comma-separated
        string), when the request names them. Conditions excluded at preflight,
        and conditions a finished run never got to, count for nothing.
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
    if isinstance(ids, str):
        n_ids = len([i for i in ids.split(",") if i.strip()]) or None
    else:
        n_ids = 1 if isinstance(ids, int) else len(ids) if isinstance(ids, list) else None
    estimate = overrides["epochs"] * n_ids if n_ids and isinstance(overrides.get("epochs"), int) else None
    finished_run = best.get("state") in ("completed", "interrupted", "failed")
    done = total = 0
    for pair in best["pairs"].values():
        size = pair.get("samples_total")
        if size is None:
            size = pair.get("samples_planned")
        if size is None and (pair.get("state") == "excluded-at-preflight" or finished_run):
            continue  # will never run any more samples
        if size is None:
            size = estimate
        if size is None:
            return None
        total += int(size)
        done += int(pair.get("samples_completed") or 0)
    return {"done": done, "total": total}


def helper_task(request: dict[str, Any]) -> str | None:
    """A short excerpt of the brief a helper was given, when its request says it simply.

    Parameters
    ----------
    request : dict
        The helper's request.

    Returns
    -------
    str | None
        The first of its prose fields (`TASK_FIELDS`: an Analyst's ``topic``,
        an Explorer's ``hypothesis``, a Mapper's ``behaviour_description``, an
        attacking Critic's ``evidence_summary``), cut to ``MAX_TASK``
        characters; None for a request with none (an Executor's, a Designer's,
        a diff-review Critic's).
    """
    for key in TASK_FIELDS:
        text = request.get(key)
        if isinstance(text, str) and text.strip():
            return _trim(_plain(text), MAX_TASK)
    return None


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
        request mode) for `locate`, and ``_receipt`` (the receipt itself);
        receipts with no start time are left out.
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
            "_receipt": r,
        }
        if r.get("finished_at"):
            h["finishedAt"] = r["finished_at"]
        task = helper_task(_load_json(r.get("request")) or {})
        if task:
            h["task"] = task
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
    """A round's label for the panel.

    Parameters
    ----------
    n : int | None
        The round number; None outside a loop.
    cap : int | None
        The iteration cap, if the scope set one.

    Returns
    -------
    str | None
        ``"2 of 4"`` with a cap, ``"2"`` without, None outside a loop.
    """
    if n is None:
        return None
    return f"{n} of {cap}" if cap else str(n)


def iteration_cap(sections: list[dict[str, Any]]) -> int | None:
    """The iteration cap recorded in the Agreed Scope.

    Matches lines such as ``- Iteration cap: **4**``; ``**none**`` gives None.

    Parameters
    ----------
    sections : list[dict]
        `read_log`'s output.

    Returns
    -------
    int | None
        The cap; None when the scope set none or it was not found.
    """
    for s in sections:
        if s["title"].lower().startswith("agreed scope"):
            text = "\n".join(line for sub in s["subs"] for line in sub["lines"])
            m = re.search(r"iteration cap\W{0,6}(\d+)\b", text, re.I)
            return int(m.group(1)) if m else None
    return None


def helper_box(spec: dict[str, Any], helper: dict[str, Any]) -> dict[str, Any] | None:
    """The phase, box and round a helper works in, from its tag, role and mode.

    Parameters
    ----------
    spec : dict
        The run's pipeline from the data file.
    helper : dict
        One `collect_helpers` entry.

    Returns
    -------
    dict | None
        ``{"phase", "box", "side", "round"}``, e.g. ``{"phase": "3", "box":
        "design", "side": True, "round": 2}`` for an ``iter_02`` diff-review
        Critic; ``round`` is None outside a loop. None when no rule maps it.
    """
    phases = {p["id"]: p for p in spec["phases"]}
    for rule in spec["helperBoxes"]:
        m = _match(rule["tag"], helper["tag"])
        if not m or rule.get("role", helper["_role"]) != helper["_role"] or rule.get("mode", helper["_mode"]) != helper["_mode"]:
            continue
        rnd = m.groupdict().get("round")
        loop = phases[rule["phase"]].get("loop")
        return {"phase": rule["phase"], "box": rule["box"], "side": bool(rule.get("side")),
                "round": int(rnd) if loop and rnd else None}
    return None


def _lead_roles(spec: dict[str, Any]) -> set[str]:
    """The box roles that are the lead's own, e.g. ``{"Diagnostician"}``.

    Parameters
    ----------
    spec : dict
        The run's pipeline from the data file.

    Returns
    -------
    set[str]
        Every box role that no launcher role is drawn as.
    """
    return {b["role"] for p in spec["phases"] for b in p["boxes"]} - set(spec["roles"].values())


def between_boxes(spec: dict[str, Any], last: dict[str, Any], helpers: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    """Whether the lead is working between one helper's box and the next helper's.

    That is so when the helper that finished most recently left a box whose
    next box is a helper's that nobody has started, in the round (or the
    phase after) the log is at. When the next box is the lead's own, the
    lead is in it, not between.

    Parameters
    ----------
    spec : dict
        The run's pipeline from the data file.
    last : dict
        The newest mapped log section.
    helpers : list[dict]
        `collect_helpers`'s output, none of them at work on a pipeline box.

    Returns
    -------
    tuple[dict, str] | None
        The finished box's `helper_box` place (its ``box`` the main box's
        id) and the next box's id, e.g. ``({"phase": "2", "box": "explore",
        "side": False, "round": 2}, "execute")``; None otherwise.
    """
    order = [p["id"] for p in spec["phases"]]
    phases = {p["id"]: p for p in spec["phases"]}
    done = [(h, place) for h in helpers if h["state"] == "finished" and h.get("finishedAt") and (place := helper_box(spec, h))]
    if not done:
        return None
    _, place = max(done, key=lambda x: x[0]["finishedAt"])

    # The log at the same round, or a phase behind (it lags a launch)
    lag = order.index(place["phase"]) - order.index(last["phase"])
    if not (lag == 1 or (lag == 0 and place["round"] == last["round"])):
        return None
    boxes = phases[place["phase"]]["boxes"]
    i = next(i for i, b in enumerate(boxes) if b["id"] == place["box"])
    if i + 1 >= len(boxes) or boxes[i + 1]["role"] in _lead_roles(spec):
        return None
    to = boxes[i + 1]["id"]
    started = any((p := helper_box(spec, h)) and (p["phase"], p["round"], p["box"]) == (place["phase"], place["round"], to) for h in helpers)
    return None if started else (place, to)


def side_purpose(inv: str, helper: dict[str, Any]) -> str:
    """A short name for what a side helper (one no pipeline box claims) is doing.

    Parameters
    ----------
    inv : str
        The investigation folder.
    helper : dict
        One `collect_helpers` entry.

    Returns
    -------
    str
        The folder its work sits in, when that names it (``construal_pilot``
        gives ``"construal pilot"``), else its brief's excerpt or its tag;
        under 80 characters.
    """
    receipt = helper["_receipt"].get("receipt") or ""
    top = os.path.relpath(receipt, inv).split(os.sep)[0] if receipt.startswith(inv + os.sep) else ""
    if top and top not in ("artefacts", "iterations", "requests") and not re.match(r"iter", top) and not top.endswith(".json"):
        text = top.replace("_", " ")
    else:
        text = helper.get("task") or helper["tag"]
    return _trim(text, 79, sentences=False)


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
        ``{"phase", "box", "isSide"?, "round"?, "between"?}``. ``box`` is
        ``""`` in a phase with no boxes; ``isSide`` means the named box's side
        box is working; ``between`` (``{"from", "to"}``, box ids) means the
        lead is working after ``from`` finished and before ``to`` started,
        with ``box`` left on ``from``. A helper no pipeline box claims (a
        side helper, see `side_purpose`) never moves it.
    """
    order = [p["id"] for p in spec["phases"]]
    phases = {p["id"]: p for p in spec["phases"]}
    if state == "finished":
        return {"phase": order[-1], "box": ""}
    if not mapped:
        return None
    last = mapped[-1]

    # ---- A helper at work on a pipeline box: its tag, role and mode name the box ----
    running = [(h, place) for h in helpers if h["state"] in ("launching", "running") and (place := helper_box(spec, h))]
    if running:
        _, place = max(running, key=lambda x: x[0]["startedAt"])
        # The log lags a launch by at most one phase; a bigger gap means a
        # stale receipt or an unmapped step, so claim nothing
        if order.index(place["phase"]) - order.index(last["phase"]) not in (0, 1):
            return None
        out: dict[str, Any] = {"phase": place["phase"], "box": place["box"]}
        if place["side"]:
            out["isSide"] = True
        if place["round"] is not None:
            out["round"] = _round_label(place["round"], cap)
        return out

    # ---- The lead at work after one helper's box, before the next helper's ----
    gap = between_boxes(spec, last, helpers)
    if gap is not None:
        place, to = gap
        out = {"phase": place["phase"], "box": place["box"]}
        if place["round"] is not None:
            out["round"] = _round_label(place["round"], cap)
        out["between"] = {"from": place["box"], "to": to}
        return out

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
    """What the run is currently chasing, cut to fit the panel's older layout.

    Parameters
    ----------
    name, inv, mapped
        As for `full_explanation`.

    Returns
    -------
    str | None
        `full_explanation`'s text, cut to ``MAX_EXPLANATION`` characters.
    """
    text = full_explanation(name, inv, mapped)
    return _trim(text, MAX_EXPLANATION) if text else None


def full_explanation(name: str, inv: str, mapped: list[dict[str, Any]]) -> str | None:
    """What the run is currently chasing, in full.

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
        return f"{top.id} {top.slug}: {statement}" if statement else f"{top.id} {top.slug}"

    for s in reversed(mapped):
        for sub in reversed(s["subs"]):
            if sub["title"].lower().startswith("hypothesis this iteration"):
                lines = [ln for ln in sub["lines"] if ln.strip()]
                stated = next((ln for ln in lines if re.match(r"-\s+\*Statement\*", ln)), lines[0] if lines else "")
                text = _plain(re.sub(r"^-\s+\*Statement\*\s*:?", "", stated))
                return text or None
    return None


def agreed_question(sections: list[dict[str, Any]]) -> str | None:
    """The run's agreed question, or the behaviour it sets out to explain, in full.

    Parameters
    ----------
    sections : list[dict]
        `read_log`'s output.

    Returns
    -------
    str | None
        The Agreed Scope's ``Explanandum`` (model forensics) or ``Research
        question`` (science of evals) line, without its label, e.g.
        ``"The model scores 14% rather than 90% on the arithmetic test."``;
        None before the scope is written or when it names neither.
    """
    for s in sections:
        if s["title"].lower().startswith("agreed scope"):
            for line in (ln for sub in s["subs"] for ln in sub["lines"]):
                # e.g. "- Explanandum (contrast form): ..." or "- **Research question**: ..."
                m = re.match(r"\s*-\s+\**(?:explanandum|research question)\**\s*(?:\([^)]*\))?\s*:\**\s*(.+)", line, re.I)
                if m:
                    return _plain(m.group(1)) or None
    return None


def _ledger_ids(lines: list[str], label: str) -> list[str]:
    """Candidate ids on a ``- <label>:`` line of a Ledger Delta block, in order.

    Parameters
    ----------
    lines : list[str]
        The block's lines.
    label : str
        ``Eliminated`` or ``Added``.

    Returns
    -------
    list[str]
        e.g. ``["C2", "X3"]`` for ``- **Eliminated:** `C2`, `X3```, each once.
    """
    ids: list[str] = []
    for ln in lines:
        if re.match(rf"-\s+\*{{0,2}}{label}\*{{0,2}}\s*:", ln, re.I):
            ids += [t for t in re.findall(r"`([^`]+)`", ln) if CANDIDATE_ID.match(t) and t not in ids]
    return ids


def _ledger_change(section: dict[str, Any]) -> str | None:
    """What a round changed in the candidate ledger, from its Ledger Delta blocks.

    Parameters
    ----------
    section : dict
        One mapped log section.

    Returns
    -------
    str | None
        e.g. ``"C2 out; X3 added"``, or ``"no candidate out"``; None when the
        section has no Ledger Delta block.
    """
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

    Parameters
    ----------
    section : dict
        One mapped log section.

    Returns
    -------
    str | None
        The headline; None when the block has no such line.
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


def finished_steps(name: str, spec: dict[str, Any], mapped: list[dict[str, Any]], state: str,
                   helpers: list[dict[str, Any]] | None = None) -> list[str]:
    """One short line per finished phase or round, then per finished box not yet written up.

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
    helpers : list[dict] | None
        `collect_helpers`'s output, for the boxes their helpers have finished.

    Returns
    -------
    list[str]
        E.g. ``["Scoping done", "Reading: X1, X2 added", "Round 1: C2 out; X3 added",
        "Round 2, Design: Three variants of the system prompt."]``. A loop
        round counts as finished once it has a Decision block; any other
        section once a later one starts. The report phase adds no step. A
        box counts once all its helpers have finished, until its round or
        phase is written up, whose step then stands in for it.
    """
    return [_trim(f"{title}: {detail}" if detail else f"{title} done", MAX_STEP)
            for title, detail in _step_parts(name, spec, mapped, state, helpers or [])]


def step_details(name: str, spec: dict[str, Any], mapped: list[dict[str, Any]], state: str,
                 helpers: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """The full text of each `finished_steps` line, uncut.

    Parameters
    ----------
    name, spec, mapped, state, helpers
        As for `finished_steps`.

    Returns
    -------
    list[dict]
        ``{"title", "detail"}`` per step, in the same order, e.g.
        ``{"title": "Round 1", "detail": "C2 out; X3 added"}``; a step with
        nothing more to say has ``"done"`` as its detail.
    """
    return [{"title": title, "detail": detail or "done"}
            for title, detail in _step_parts(name, spec, mapped, state, helpers or [])]


def _step_parts(name: str, spec: dict[str, Any], mapped: list[dict[str, Any]], state: str,
                helpers: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    """Each finished phase's or round's name and what it came to, then each finished box's.

    Parameters
    ----------
    name, spec, mapped, state, helpers
        As for `finished_steps`.

    Returns
    -------
    list[tuple[str, str | None]]
        ``(title, detail)`` per step, e.g. ``("Round 1", "C2 out; X3 added")``
        or ``("Scoping", None)`` when there is nothing more to say.
    """
    phases = {p["id"]: p for p in spec["phases"]}
    report_phase = spec["phases"][-1]["id"]
    parts: list[tuple[str, str | None]] = []
    # The phases and rounds those steps cover, as (phase id, round)
    covered: set[tuple[str, int | None]] = set()
    for i, s in enumerate(mapped):
        phase = phases[s["phase"]]
        has_decision = any(sub["title"].lower().startswith("decision") for sub in s["subs"])
        done = state == "finished" or i < len(mapped) - 1 or (phase.get("loop") and has_decision)
        if not done or s["phase"] == report_phase:
            continue

        # ---- A round: what changed, or what was decided ----
        if phase.get("loop") and s["round"] is not None:
            detail = _ledger_change(s) if name == "diagnose" else _decision_headline(s)
            parts.append((f"Round {s['round']}", detail))
            covered.add((s["phase"], s["round"]))
        # ---- Another phase: once per phase, with any ledger change ----
        elif not any(title == phase["title"] for title, _ in parts):
            if phase["id"] == spec["phases"][0]["id"]:
                parts.append((phase["title"], "scope agreed"))
            else:
                parts.append((phase["title"], _ledger_change(s) if name == "diagnose" else None))
            covered.add((s["phase"], None))
    return parts + _box_step_parts(spec, helpers, covered)


def _box_step_parts(spec: dict[str, Any], helpers: list[dict[str, Any]],
                    covered: set[tuple[str, int | None]]) -> list[tuple[str, str | None]]:
    """A step for each box whose helpers have all finished, in a phase or round not yet written up.

    Parameters
    ----------
    spec : dict
        The run's pipeline from the data file.
    helpers : list[dict]
        `collect_helpers`'s output.
    covered : set[tuple[str, int | None]]
        The (phase id, round) pairs the log's steps already cover.

    Returns
    -------
    list[tuple[str, str | None]]
        ``(title, detail)`` per box, in pipeline order (a side box after its
        main box), e.g. ``("Round 2, Explore", "All six cells are reachable.")``.
        The detail is the newest helper's own one-line result
        (`report_line`), ``"failed"`` when that helper failed, else None.
    """
    by_box: dict[tuple[str, int | None, str, bool], list[dict[str, Any]]] = {}
    for h in helpers:
        place = helper_box(spec, h)
        if place and (place["phase"], place["round"]) not in covered:
            by_box.setdefault((place["phase"], place["round"], place["box"], place["side"]), []).append(h)

    parts: list[tuple[str, str | None]] = []
    for p in spec["phases"]:
        for rnd in sorted({k[1] for k in by_box if k[0] == p["id"]}, key=lambda r: r or 0):
            for b in p["boxes"]:
                for side in (False, True):
                    group = by_box.get((p["id"], rnd, b["id"], side))
                    if not group or any(h["state"] not in ("finished", "failed") for h in group):
                        continue
                    title = b["side"]["title"] if side and b.get("side") else b["title"]
                    newest = max(group, key=lambda h: h.get("finishedAt") or h["startedAt"])
                    detail = "failed" if newest["state"] == "failed" else report_line(newest["_receipt"].get("report"))
                    parts.append((f"Round {rnd}, {title}" if rnd is not None else f"{p['title']}, {title}", detail))
    return parts


def report_line(path: str | None) -> str | None:
    """A helper's own one-line result: the first sentence of its report's summary.

    Parameters
    ----------
    path : str | None
        The helper's report (its receipt's ``report``).

    Returns
    -------
    str | None
        The first sentence of the first paragraph under ``### Summary`` (or
        of the report's first paragraph when it has no such heading), plain
        and cut to ``MAX_EXPLANATION``; None when there is no report yet.
    """
    try:
        with open(path or "") as f:
            text = f.read(20_000)
    except OSError:
        return None
    m = re.search(r"^#+\s*Summary\s*$", text, re.M | re.I)
    body = text[m.end():] if m else text
    para = next((p for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith("#")), "")
    plain = _plain(para)
    first = re.match(r"(.+?[.!?])(?=\s|$)", plain)
    return _trim(first.group(1) if first else plain, MAX_EXPLANATION) or None


# ---------------------------------------------------------------------------
# Timings: estimates from the times the run's own records carry
# ---------------------------------------------------------------------------


def _time(text: Any) -> datetime | None:
    """An ISO time from a receipt or session line, made timezone-aware.

    Parameters
    ----------
    text : Any
        e.g. ``"2026-11-08T21:03:00+00:00"`` or ``"2026-11-08T21:03:00.123Z"``.

    Returns
    -------
    datetime | None
        In UTC; None for anything that is not such a time.
    """
    if not isinstance(text, str):
        return None
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    """A time as the panel's ISO string, e.g. ``"2026-11-08T21:03:00+00:00"``; None stays None.

    Parameters
    ----------
    t : datetime | None
        The time.

    Returns
    -------
    str | None
        The string.
    """
    return t.isoformat(timespec="seconds") if t else None


def _span(start: datetime | None, finish: datetime | None) -> dict[str, str]:
    """``startedAt`` and ``finishedAt`` for whichever of the two is known.

    Parameters
    ----------
    start, finish : datetime | None
        The span's ends.

    Returns
    -------
    dict[str, str]
        e.g. ``{"startedAt": "2026-11-08T21:03:00+00:00"}`` while it runs.
    """
    out = {}
    if start:
        out["startedAt"] = _iso(start)
    if finish:
        out["finishedAt"] = _iso(finish)
    return out


def _heading_place(spec: dict[str, Any], title: str) -> tuple[str, int | None] | None:
    """The phase and round a ``## `` log heading opens, as `map_sections` reads it.

    Parameters
    ----------
    spec : dict
        The run's pipeline from the data file.
    title : str
        The heading, e.g. ``"Iteration 2"``.

    Returns
    -------
    tuple[str, int | None] | None
        ``(phase id, round)``, e.g. ``("3", 2)``; None for a heading no rule names.
    """
    phases = {p["id"]: p for p in spec["phases"]}
    for rule in spec["phaseHeadings"]:
        m = _match(rule["pattern"], title)
        if m:
            rnd = m.groupdict().get("round")
            return rule["phase"], int(rnd) if rnd and phases[rule["phase"]].get("loop") else None
    return None


def _box_times(phase: dict[str, Any], placed: list[tuple[dict[str, Any], dict[str, Any]]], lead_roles: set[str],
               start: datetime | None, finish: datetime | None) -> list[dict[str, str]]:
    """Each box's time within one phase, or one round of a loop phase.

    A box run by a helper takes its helpers' times: the first start and the
    last finish, with no finish while one is still at work. A box the lead
    does alone runs from the end of the box before it to the start of the box
    after it (the span's own start or finish at either end), and is left out
    unless both are known.

    Parameters
    ----------
    phase : dict
        The phase from the data file.
    placed : list[tuple[dict, dict]]
        ``(helper, helper_box(...))`` for the helpers working in this span.
    lead_roles : set[str]
        Box roles that are the lead's own, e.g. ``{"Diagnostician"}``.
    start, finish : datetime | None
        The span's own start and finish, where known.

    Returns
    -------
    list[dict]
        ``{"id", "startedAt"?, "finishedAt"?}`` per box with any time, side
        boxes under their own ids, in drawing order.
    """
    # ---- Helpers' times per box, main or side ----
    by_box: dict[tuple[str, bool], list[datetime | None]] = {}
    for h, place in placed:
        s, f = _time(h["startedAt"]), _time(h.get("finishedAt"))
        if s is None:
            continue
        key = (place["box"], place["side"])
        first, last = by_box.get(key, [s, f])
        # Any helper still at work keeps the box open
        by_box[key] = [min(first, s), None if f is None or last is None else max(last, f)]

    def edge(i: int, end: bool) -> datetime | None:
        """Where box `i` (with its side box) finishes, or starts; the span's own end beyond the last box."""
        if i < 0:
            return start
        if i >= len(phase["boxes"]):
            return finish
        b = phase["boxes"][i]
        times = [by_box[k][1 if end else 0] for k in ((b["id"], False), (b["id"], True)) if k in by_box]
        if not times or (end and None in times):
            return None
        return max(times) if end else min(times)

    out = []
    for i, b in enumerate(phase["boxes"]):
        s = f = None
        if b["role"] in lead_roles:
            left, right = edge(i - 1, end=True), edge(i + 1, end=False)
            if left and right and left <= right:
                s, f = left, right
        if s is None and (b["id"], False) in by_box:
            s, f = by_box[(b["id"], False)]
        if s:
            out.append({"id": b["id"], **_span(s, f)})
        if b.get("side") and (b["id"], True) in by_box:
            out.append({"id": b["side"]["id"], **_span(*by_box[(b["id"], True)])})
    return out


def timings(spec: dict[str, Any], mapped: list[dict[str, Any]], helpers: list[dict[str, Any]],
            sessions: list[dict[str, Any]], current: dict[str, Any] | None, state: str) -> dict[str, Any] | None:
    """Estimated start and finish times for each phase reached, and its rounds and boxes.

    The log's headings carry no times, so the sources are the helpers'
    receipts and the lead's own session: when it started, and when it first
    wrote each ``## `` heading. A phase or round starts at its first sign
    (its heading or its first helper, and the run's start for the first
    phase) and finishes where the next one starts; the one running has no
    finish. When one write brings in several phase or round headings at once
    (a skeleton written ahead), only the first counts. A time that would run
    backwards is dropped. Unknown times are left out.

    Parameters
    ----------
    spec : dict
        The run's pipeline from the data file.
    mapped : list[dict]
        `map_sections`'s output.
    helpers : list[dict]
        `collect_helpers`'s output.
    sessions : list[dict]
        The lead's sessions as `spend.collect_lead` reads them, with
        ``first_at`` and ``log_headings``.
    current : dict | None
        `locate`'s answer.
    state : str
        `run_state`'s answer.

    Returns
    -------
    dict | None
        ``{"phases": [{"id", "startedAt"?, "finishedAt"?, "boxes"?: [...],
        "rounds"?: [{"round": "2", "startedAt"?, "finishedAt"?, "boxes"?}]}]}``;
        None when no time is known at all.
    """
    order = [p["id"] for p in spec["phases"]]
    lead_roles = {b["role"] for p in spec["phases"] for b in p["boxes"]} - set(spec["roles"].values())

    # ---- Signs of each phase and round: headings first written, helpers launched ----
    signs: dict[tuple[str, int | None], list[datetime]] = {}
    written: dict[str, datetime] = {}
    for sess in sessions:
        for title, at in (sess.get("log_headings") or {}).items():
            t = _time(at)
            if t and (title not in written or t < written[title]):
                written[title] = t
    by_write: dict[datetime, list[tuple[int, int, str, int | None]]] = {}
    for title, t in written.items():
        place = _heading_place(spec, title)
        if place:
            by_write.setdefault(t, []).append((order.index(place[0]), place[1] or 0, *place))
    for t, places in by_write.items():
        *_, phase_id, rnd = min(places)
        signs.setdefault((phase_id, rnd), []).append(t)
    run_start = min((t for t in (_time(s.get("first_at")) for s in sessions) if t), default=None)
    if run_start:
        signs.setdefault((order[0], None), []).append(run_start)
    placed = [(h, place) for h in helpers if (place := helper_box(spec, h))]
    for h, place in placed:
        t = _time(h["startedAt"])
        if t:
            signs.setdefault((place["phase"], place["round"]), []).append(t)
    if not signs:
        return None

    # ---- The phases reached: up to the one running ----
    if state == "finished":
        last = len(order) - 1
    elif current:
        last = order.index(current["phase"])
    else:
        reached = [order.index(s["phase"]) for s in mapped] + [order.index(p) for p, _ in signs]
        last = max(reached)
    phase_starts: dict[str, datetime | None] = {}
    floor = None
    for pid in order[: last + 1]:
        t = min((x for (p, _), ts in signs.items() if p == pid for x in ts), default=None)
        if t and floor and t < floor:
            t = None
        phase_starts[pid] = t
        floor = t or floor

    # ---- Each phase: its span, then its rounds or boxes ----
    out = []
    for i, pid in enumerate(order[: last + 1]):
        phase = spec["phases"][i]
        start = phase_starts[pid]
        running = i == last and state != "finished"
        finish = None if running or i == last else phase_starts[order[i + 1]]
        entry: dict[str, Any] = {"id": pid, **_span(start, finish)}
        here = [(h, place) for h, place in placed if place["phase"] == pid]
        if phase.get("loop"):
            numbers = sorted({r for (p, r) in signs if p == pid and r is not None}
                             | {s["round"] for s in mapped if s["phase"] == pid and s["round"] is not None})
            starts, floor = [], start
            for n in numbers:
                t = min(signs.get((pid, n), []), default=None)
                if t and floor and t < floor:
                    t = None
                starts.append(t)
                floor = t or floor
            rounds = []
            for j, n in enumerate(numbers):
                r_finish = starts[j + 1] if j + 1 < len(numbers) else finish
                r: dict[str, Any] = {"round": str(n), **_span(starts[j], r_finish)}
                boxes = _box_times(phase, [x for x in here if x[1]["round"] == n], lead_roles, starts[j], r_finish)
                if boxes:
                    r["boxes"] = boxes
                rounds.append(r)
            if rounds:
                entry["rounds"] = rounds
        else:
            boxes = _box_times(phase, here, lead_roles, start, finish)
            if boxes:
                entry["boxes"] = boxes
        out.append(entry)
    return {"phases": out}


def _within(entries: list[dict[str, Any]], t: datetime) -> dict[str, Any] | None:
    """The latest-starting timed entry whose span holds time `t`.

    Parameters
    ----------
    entries : list[dict]
        `timings` phases, rounds or boxes.
    t : datetime
        The time.

    Returns
    -------
    dict | None
        The entry; one with no finish is still open, so holds every later time.
    """
    held = [e for e in entries if (s := _time(e.get("startedAt"))) and s <= t
            and (not e.get("finishedAt") or t < _time(e["finishedAt"]))]
    return max(held, key=lambda e: e["startedAt"]) if held else None


def helper_cost(helper: dict[str, Any], summary: dict[str, Any]) -> float | None:
    """What one helper cost: its own session, plus the evals and model calls it ran.

    Parameters
    ----------
    helper : dict
        One `collect_helpers` entry.
    summary : dict
        `spend.collect`'s answer for the investigation.

    Returns
    -------
    float | None
        Dollars from its cost sidecar, its batch runs' evals (an Executor's,
        started within its lifetime) and its logged model calls (an
        Analyst's); None when none of these is known.
    """
    r = helper["_receipt"]
    parts = [s["cost_usd"] for s in summary["subagents"]["sidecars"]
             if r.get("cost") and s["path"] == r["cost"] and isinstance(s.get("cost_usd"), (int, float))]
    folder = os.path.dirname(r.get("receipt") or "")
    if folder:
        calls = os.path.join(folder, f"model_calls_{r.get('tag')}_{r.get('role')}_{r.get('n')}.jsonl")
        if calls in summary["analyst_calls"].get("by_file", {}):
            parts.append(summary["analyst_calls"]["by_file"][calls])
        started, finished = r.get("started_at") or "", r.get("finished_at")
        for run in summary["evals"]["run_summaries"]:
            at = run.get("started_at") or ""
            if run["path"].startswith(os.path.join(folder, "runs") + os.sep) and at >= started and not (finished and at > finished):
                parts.append(run["cost_usd"])
    return float(sum(parts)) if parts else None


def add_costs(times: dict[str, Any], spec: dict[str, Any], helpers: list[dict[str, Any]],
              costs: list[float | None], lead_calls: list[tuple[datetime, float]]) -> None:
    """Give each phase, round and box in `times` the dollars spent in it, as ``usd``.

    A helper's cost goes to its box, round and phase (a side helper's to the
    round and phase it started in). Each of the lead's own calls goes to the
    phase, round and main box running when it was made; an entry holding any
    of the lead's cost is marked ``leadApprox``, since a box's span is only
    estimated. Entries with nothing known get no ``usd``.

    Parameters
    ----------
    times : dict
        `timings`'s answer, changed in place.
    spec : dict
        The run's pipeline from the data file.
    helpers : list[dict]
        `collect_helpers`'s output.
    costs : list[float | None]
        `helper_cost` per helper, in the same order.
    lead_calls : list[tuple[datetime, float]]
        The lead's priced calls: when made, and dollars.
    """
    totals: dict[int, float] = {}
    approx: set[int] = set()
    phases = {p["id"]: p for p in spec["phases"]}

    def add(entry: dict[str, Any] | None, usd: float, lead: bool) -> None:
        """Count `usd` towards one entry, by identity."""
        if entry is None:
            return
        totals[id(entry)] = totals.get(id(entry), 0.0) + usd
        if lead:
            approx.add(id(entry))

    # ---- Helpers: their box, round and phase ----
    by_phase = {t["id"]: t for t in times["phases"]}
    for h, usd in zip(helpers, costs):
        if usd is None:
            continue
        place = helper_box(spec, h)
        start = _time(h["startedAt"])
        phase = by_phase.get(place["phase"]) if place else (_within(times["phases"], start) if start else None)
        if phase is None:
            continue
        rnd = None
        if "rounds" in phase:
            rnd = (next((r for r in phase["rounds"] if place and r["round"] == str(place["round"])), None) if place
                   else _within(phase["rounds"], start) if start else None)
        add(phase, usd, False)
        add(rnd, usd, False)
        if place:
            main = next(b for b in phases[place["phase"]]["boxes"] if b["id"] == place["box"])
            box_id = main["side"]["id"] if place["side"] and main.get("side") else main["id"]
            add(next((b for b in (rnd or phase).get("boxes", []) if b["id"] == box_id), None), usd, False)

    # ---- The lead: by when each call was made ----
    first = times["phases"][0] if times["phases"] else None
    for t, usd in lead_calls:
        # Calls before the first known start belong to the run's opening
        phase = _within(times["phases"], t) or (first if first and (not first.get("startedAt") or t < _time(first["startedAt"])) else None)
        if phase is None:
            continue
        rnd = _within(phase.get("rounds", []), t)
        mains = {b["id"] for b in phases[phase["id"]]["boxes"]}
        box = _within([b for b in (rnd or phase).get("boxes", []) if b["id"] in mains], t)
        for entry in (phase, rnd, box):
            add(entry, usd, True)

    # ---- Written onto the entries ----
    for phase in times["phases"]:
        for entry in [phase] + phase.get("boxes", []) + [x for r in phase.get("rounds", []) for x in [r] + r.get("boxes", [])]:
            if id(entry) in totals:
                entry["usd"] = round(totals[id(entry)], 2)
                if id(entry) in approx:
                    entry["leadApprox"] = True


def pipeline_place(inv: str, spec: dict[str, Any], helper: dict[str, Any], times: dict[str, Any] | None) -> dict[str, Any]:
    """Where on the pipeline a helper's work sits, as the panel's helper fields.

    Parameters
    ----------
    inv : str
        The investigation folder.
    spec : dict
        The run's pipeline from the data file.
    helper : dict
        One `collect_helpers` entry.
    times : dict | None
        `timings`'s answer, to place a side helper by when it started.

    Returns
    -------
    dict
        For a helper on a pipeline box, ``{"phase", "round"?, "box"}``, where
        a pipeline side box (attack, diff-review, pick-factor) gives its own
        id, e.g. ``{"phase": "3", "round": "2", "box": "diff-review"}``. For a
        side helper, ``{"side": True, "purpose", "phase"?, "round"?}``, from
        the phase and round running when it started; e.g. ``{"side": True,
        "purpose": "construal pilot", "phase": "2", "round": "1"}``.
    """
    place = helper_box(spec, helper)
    if place:
        main = next(b for b in next(p for p in spec["phases"] if p["id"] == place["phase"])["boxes"] if b["id"] == place["box"])
        out: dict[str, Any] = {"phase": place["phase"]}
        if place["round"] is not None:
            out["round"] = str(place["round"])
        out["box"] = main["side"]["id"] if place["side"] and main.get("side") else main["id"]
        return out
    out = {"side": True, "purpose": side_purpose(inv, helper)}
    start = _time(helper["startedAt"])
    phase = _within(times["phases"], start) if times and start else None
    if phase:
        out["phase"] = phase["id"]
        rnd = _within(phase.get("rounds", []), start)
        if rnd:
            out["round"] = rnd["round"]
    return out


# ---------------------------------------------------------------------------
# The whole answer
# ---------------------------------------------------------------------------


def _iso_mtime(path: str) -> str | None:
    """When a file was last written, as an ISO time in UTC.

    Parameters
    ----------
    path : str
        The file.

    Returns
    -------
    str | None
        e.g. ``"2026-11-08T14:20:03+00:00"``; None if the file is missing.
    """
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
             "startedAt": "2026-11-08T08:02:50+00:00",
             "question": "...", "explanationFull": "C1 deployment_goal_inference: ...",
             "stepDetails": [{"title": "Round 1", "detail": "C2 out; X3 added"}],
             "timings": {"phases": [{"id": "0", "startedAt": "...", "finishedAt": "..."}, ...]}}
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

    # ---- Cost: everything in dollars, as spend.py totals it; eval tokens too ----
    # Never online: the poll runs every 5 seconds, so prices for non-Claude
    # models come only from the lists an earlier `spend.py` run cached
    summary = spend.collect(inv, projects_dir, prices=spend.PriceBook(offline=True))
    eval_tokens = sum(int(t.get("total_tokens") or 0) for t in summary["evals"]["tokens_by_model"].values())
    cost: dict[str, Any] = {"usd": round(summary["total_cost_usd"], 2)}
    if eval_tokens:
        cost["evalTokens"] = eval_tokens

    current = locate(spec, mapped, helpers, state, iteration_cap(sections)) if sure else None
    explanation = full_explanation(name, inv, mapped)
    status: dict[str, Any] = {
        "version": STATUS_VERSION,
        "investigation": os.path.basename(inv),
        "pipeline": {"name": name, "title": spec["title"], "phases": spec["phases"]},
        "state": state,
        "current": current,
        "explanation": _trim(explanation, MAX_EXPLANATION) if explanation else None,
        "helpers": [{k: v for k, v in h.items() if not k.startswith("_")} for h in helpers],
        "steps": finished_steps(name, spec, mapped, state, helpers),
        "cost": cost,
        "report": "FINDINGS-REPORT.md" if state == "finished" else None,
    }
    if helpers:
        status["startedAt"] = helpers[0]["startedAt"]
    if state == "finished":
        status["finishedAt"] = _iso_mtime(os.path.join(inv, "FINDINGS-REPORT.md"))

    # ---- Fuller material for the run tab (all optional; older panels ignore it) ----
    question = agreed_question(sections)
    if question:
        status["question"] = question
    if explanation:
        status["explanationFull"] = explanation
    status["stepDetails"] = step_details(name, spec, mapped, state, helpers)
    # The stage is only claimed when the files settle the pipeline, and so
    # are times, costs and places
    if not sure:
        return status
    times = timings(spec, mapped, helpers, summary["lead"]["sessions"], current, state)
    if times:
        lead_calls = [(t, usd) for sess in summary["lead"]["sessions"]
                      for at, usd in sess.get("call_costs", []) if (t := _time(at))]
        add_costs(times, spec, helpers, [helper_cost(h, summary) for h in helpers], lead_calls)
        status["timings"] = times
    for h, out in zip(helpers, status["helpers"]):
        out.update(pipeline_place(inv, spec, h, times))
    return status


def _slug(title: str) -> str:
    """A phase title as the box name a phase without boxes answers to, e.g. ``"discussion"``.

    Parameters
    ----------
    title : str
        e.g. ``"Read the reasoning"``.

    Returns
    -------
    str
        Lower case, words joined by hyphens: ``"read-the-reasoning"``.
    """
    return "-".join(re.findall(r"[a-z0-9]+", title.lower()))


def _brief(request_path: str | None) -> str | None:
    """A helper's brief in full: its prose field, else its request without the launcher's keys.

    Parameters
    ----------
    request_path : str | None
        The helper's request file.

    Returns
    -------
    str | None
        e.g. an Analyst's ``topic``; an Executor's request as compact JSON;
        None when the request can't be read.
    """
    request = _load_json(request_path)
    if request is None:
        return None
    for key in TASK_FIELDS:
        if isinstance(request.get(key), str) and request[key].strip():
            return request[key].strip()
    return json.dumps(launch_subagent._task_of(request_path) or request)


def box_material(inv: str, which: str, hint: str | None = None, pipelines: dict[str, Any] | None = None) -> dict[str, Any]:
    """The material behind one box, for the panel to summarise when it is clicked.

    Parameters
    ----------
    inv : str
        ``investigations/<name>``.
    which : str
        ``<phase>[:<round>]:<box>``, e.g. ``"3:2:run"``, ``"1:triage"``, or
        for a phase with no boxes its title in lower case, e.g.
        ``"1:discussion"``. A pipeline side box answers to its own id
        (``"3:2:diff-review"``).
    hint : str | None
        The pipeline to assume when the files don't say.
    pipelines : dict | None
        The data file's pipelines; read from ``PIPELINES_PATH`` when None.

    Returns
    -------
    dict
        ``{"version": 1, "box": {"phase", "round"?, "box"}, "helpers":
        [{"role", "tag", "task"?, "result"?}], "log": str}``: each helper's
        brief and final report cut to ``MAX_MATERIAL`` characters, and the
        lead's log for the box (the ``### `` blocks the data file maps onto
        it, or a box-less phase's whole sections) cut to ``MAX_BOX_LOG``.

    Raises
    ------
    ValueError
        For a box the pipeline does not have, with the reason.
    """
    inv = os.path.abspath(inv)
    pipelines = pipelines or load_pipelines()
    sections = read_log(os.path.join(inv, "INVESTIGATION-LOG.md"))
    receipts = launch_subagent.list_receipts(inv)
    name, _ = detect_pipeline(inv, sections, receipts, hint)
    spec = pipelines[name]

    # ---- Which box: phase, round, box ----
    parts = which.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"expected <phase>[:<round>]:<box>, got {which!r}")
    phase = next((p for p in spec["phases"] if p["id"] == parts[0]), None)
    if phase is None:
        raise ValueError(f"no phase {parts[0]!r} in the {name} pipeline")
    rnd = parts[1] if len(parts) == 3 else None
    if bool(phase.get("loop")) != (rnd is not None) or (rnd is not None and not rnd.isdigit()):
        raise ValueError(f"phase {phase['id']} {'needs a round number' if phase.get('loop') else 'has no rounds'}: {which!r}")
    box = parts[-1]
    main = next((b for b in phase["boxes"] if box in (b["id"], (b.get("side") or {}).get("id"))), None)
    if (phase["boxes"] and main is None) or (not phase["boxes"] and box != _slug(phase["title"])):
        names = [b["id"] for b in phase["boxes"]] + [b["side"]["id"] for b in phase["boxes"] if b.get("side")]
        raise ValueError(f"no box {box!r} in phase {phase['id']}; it has: {', '.join(names) or _slug(phase['title'])}")
    round_no = int(rnd) if rnd is not None else None

    # ---- The helpers who worked there ----
    out_helpers = []
    for h in collect_helpers(receipts, spec):
        place = helper_box(spec, h)
        if not place or (place["phase"], place["round"]) != (phase["id"], round_no):
            continue
        b = next(x for x in phase["boxes"] if x["id"] == place["box"])
        if (b["side"]["id"] if place["side"] and b.get("side") else b["id"]) != box:
            continue
        entry: dict[str, Any] = {"role": h["role"], "tag": h["tag"]}
        brief = _brief(h["_receipt"].get("request"))
        if brief:
            entry["task"] = _trim(brief, MAX_MATERIAL, sentences=False)
        try:
            with open(h["_receipt"].get("report") or "") as f:
                report = f.read(MAX_MATERIAL * 4).strip()
        except OSError:
            report = ""
        if report:
            entry["result"] = _trim(report, MAX_MATERIAL, sentences=False)
        out_helpers.append(entry)

    # ---- The lead's log for it ----
    blocks = []
    for s in map_sections(spec, sections):
        if (s["phase"], s["round"]) != (phase["id"], round_no):
            continue
        for sub in s["subs"]:
            if phase["boxes"]:
                rule = next((r for r in spec["logBoxes"] if r["phase"] == phase["id"] and sub["title"] and _match(r["heading"], sub["title"])), None)
                if rule is None or rule["box"] != main["id"]:
                    continue
            text = "\n".join(sub["lines"]).strip()
            blocks.append((f"### {sub['title']}\n" if sub["title"] else "") + text)
    log = "\n\n".join(b for b in blocks if b.strip())

    where: dict[str, Any] = {"phase": phase["id"]}
    if rnd is not None:
        where["round"] = rnd
    where["box"] = box
    return {"version": STATUS_VERSION, "box": where, "helpers": out_helpers,
            "log": _trim(log, MAX_BOX_LOG, sentences=False)}


def render(status: dict[str, Any]) -> str:
    """A short readable summary of `build_status`'s answer, for a terminal.

    Parameters
    ----------
    status : dict
        `build_status`'s answer.

    Returns
    -------
    str
        A few lines: stage, leading explanation, helpers at work, finished
        steps, cost and report.
    """
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
        if box and cur.get("between"):
            to = next(b for b in phase["boxes"] if b["id"] == cur["between"]["to"])
            where += f", between {box['title']} and {to['title']}"
        elif box:
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
            side = f" (side: {h['purpose']})" if h.get("side") else ""
            lines.append(f"working: {h['role']} {h['tag']}{side} since {h['startedAt']}{samples}")
    lines.append(f"helpers: {len(status['helpers'])} launched")
    lines += [f"  {s}" for s in status["steps"]]
    tokens = status["cost"].get("evalTokens")
    lines.append(f"cost: ${status['cost']['usd']:.2f}" + (f" (including {tokens:,} eval tokens)" if tokens else ""))
    for t in (status.get("timings") or {}).get("phases", []):
        if t.get("startedAt"):
            # Times of day in UTC, e.g. "phase 3 Iterate: from 21:03 to 23:40"
            ends = f"from {t['startedAt'][11:16]}" + (f" to {t['finishedAt'][11:16]}" if t.get("finishedAt") else "")
            usd = f", ${t['usd']:.2f}" if "usd" in t else ""
            lines.append(f"  phase {t['id']} {phases[t['id']]['title']}: {ends} UTC{usd}")
    if status["report"]:
        lines.append(f"report: {status['report']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print one investigation's status, as JSON for the panel or as text.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments; ``sys.argv[1:]`` when None.

    Returns
    -------
    int
        0; 1 when the investigation folder does not exist; 2 for a ``--box``
        the pipeline does not have.
    """
    parser = argparse.ArgumentParser(description="Read-only status of a Prism investigation.")
    parser.add_argument("investigation", help="investigations/<name> directory")
    parser.add_argument("--json", action="store_true", help="one JSON object, for the Control Panel")
    parser.add_argument("--pipeline", choices=("diagnose", "orchestrate"), help="pipeline to assume when the files don't say")
    parser.add_argument("--box", metavar="PHASE[:ROUND]:BOX", help="the material behind one box instead, e.g. 3:2:run")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.investigation):
        print(f"not a directory: {args.investigation}", file=sys.stderr)
        return 1
    if args.box:
        try:
            material = box_material(args.investigation, args.box, args.pipeline)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        # The material is for the panel's summariser, so it is JSON either way
        print(json.dumps(material) if args.json else json.dumps(material, indent=2))
        return 0
    status = build_status(args.investigation, args.pipeline)
    print(json.dumps(status) if args.json else render(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
