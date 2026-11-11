"""The read-only run status command the Control Panel's Prism run tab draws from.

Each test builds a small investigation folder in `tmp_path` (both pipelines;
running, suspended, finished; with and without receipts) and checks the
answer of `scripts/run_status.py`. One test checks the answer against the
panel's contract (`prism-status.ts` `parseStatus` in the dotfiles repo,
mirrored here), and one that the command leaves the folder untouched.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import launch_subagent as ls
import run_status as rs

from conftest import REPO_ROOT


# ---------------------------------------------------------------------------
# Building sample investigations
# ---------------------------------------------------------------------------

FORENSICS_LOG = """# Investigation Log: sample
<!-- Append-only discipline. -->

## Agreed Scope
- Explanandum (contrast form): The model scores **14%** rather than 90% on the arithmetic test.
- Budget:
  - Iteration cap: **4** (default).

## Phase 1: Initial Scoping
### Mapper Briefing Summary
- ok
### Resample Results
- 100/100
### Baseline
- rate 14%

## Phase 2: Read the CoT
### Reading
- read
### Ledger Delta (iter 0, after Critic)
- Added: `X1` (origin: Critic iter 0) — assembly failure. `X2` (origin: Critic iter 0) — future users.

## Iteration 1
### Pre-registered Predictions
### Executor Results
### Ledger Delta
- Eliminated: `C2` (`doc_as_requirement`, was rank 2) — by R1.4.
- Added: `X3` (origin: Critic iter 1) — `inferred_assessor_preference`.
### Decision
- **Continue** to iteration 2.

## Iteration 2
### Pre-registered Predictions
- R2.1 …
"""

LEDGER = """# Candidate Ledger

## Live candidates (ranked)

### C1 — `deployment_goal` — rank 1
- *Statement*: The model takes being deployed as its goal and fails on purpose to get there. It reads the threshold as the route.
- *Discriminating signature*: assured deployment → rate falls.

### C3 — `accuracy_norm_gap` — rank 2
- *Statement*: No instruction to be accurate.
- *Discriminating signature*: an accuracy instruction → rate falls.

## Eliminated candidates
### C2 — `doc_as_requirement` — eliminated iteration 1
"""

ORCHESTRATE_LOG = """# Investigation Log: sample

## Agreed Scope
- Research question: Does framing a sabotage task as preventing harm change how often models conceal it?
- Iteration cap: none.

## Iteration 1
### Hypothesis this iteration
- *Statement*: Framing the sabotage as preventing weapons use raises concealment.
- *IV*: justification.
### Decision
**Iterate — switch environment to `agentic_misalignment` (Option 2).** Per the floor gate.

## Iteration 2
### Scope this iteration
- more
### Explorer Report Summary
- explored
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _receipt(inv: Path, where: str, role: str, tag: str, n: int, state: str, *, alive: bool = False,
             started: str = "2026-11-08T10:00:00+00:00", finished: str | None = None,
             exit_code: int | None = None, request: dict | None = None) -> Path:
    """Write a launcher receipt (and its request) as `launch_subagent.py` does.

    Parameters
    ----------
    inv : Path
        The investigation folder.
    where : str
        The artefacts folder under `inv`, e.g. ``"iterations/02_round2/artefacts"``.
    role, tag, n, state : str, str, int, str
        The receipt's identity and state.
    alive : bool
        Record this test's own process as the helper's, so it counts as running.
    started, finished, exit_code
        Times and exit code as recorded.
    request : dict | None
        The request body written beside it (a Critic's ``mode``, an Executor's ``overrides``).

    Returns
    -------
    Path
        The receipt file.
    """
    role_dir = inv / where / role
    req = _write(inv / "requests" / f"{tag}_{role}_{n}.json", json.dumps(request or {}))
    path = role_dir / f"receipt_{tag}_{role}_{n}.json"
    pid = os.getpid() if alive else 999_999_999
    body = {
        "role": role, "tag": tag, "n": n, "state": state, "request": str(req), "receipt": str(path),
        "report": str(role_dir / f"report_{tag}_{role}_{n}.md"), "stderr": str(role_dir / "stderr.log"),
        "cost": str(role_dir / f"cost_{tag}_{role}_{n}.json"),
        "started_at": started, "finished_at": finished, "exit_code": exit_code,
        "pid": pid, "pid_start": ls._pid_start_time(pid) if alive else None,
    }
    return _write(path, json.dumps(body))


def _executor_status(receipt: Path, pairs: dict, started: str = "2026-11-08T10:01:00+00:00", state: str = "running") -> None:
    """Write the batch run's `status.json` under the Executor's `runs/` folder."""
    _write(receipt.parent / "runs" / "iter_02_x" / "status.json",
           json.dumps({"schema": 1, "run_id": "iter_02_x", "started_at": started, "state": state, "pairs": pairs}))


@pytest.fixture
def forensics(tmp_path: Path) -> Path:
    """A model forensics run in round 2, its Executor running half-way."""
    inv = tmp_path / "investigations" / "sandbagging_r9"
    _write(inv / "INVESTIGATION-LOG.md", FORENSICS_LOG)
    _write(inv / "CANDIDATES.md", LEDGER)
    _receipt(inv, "artefacts", "mapper", "iter_00", 1, "finished", exit_code=0, started="2026-11-08T08:00:00+00:00", finished="2026-11-08T08:05:00+00:00")
    _receipt(inv, "iterations/01_round1/artefacts", "analyst", "iter_01", 1, "finished", exit_code=2, started="2026-11-08T09:00:00+00:00", finished="2026-11-08T09:00:01+00:00")
    ex = _receipt(inv, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "running", alive=True,
                  request={"overrides": {"sample_id": "episode", "epochs": 100}})
    _executor_status(ex, {
        "c_a": {"state": "success", "samples_completed": 100, "samples_total": 100},
        "c_b": {"state": "running", "samples_completed": None, "samples_total": None},
    })
    return inv


@pytest.fixture
def orchestrate(tmp_path: Path) -> Path:
    """A science-of-evals run in round 2, the lead between helpers."""
    inv = tmp_path / "investigations" / "gsm8k_validity"
    _write(inv / "INVESTIGATION-LOG.md", ORCHESTRATE_LOG)
    _receipt(inv, "iter_01/artefacts", "explorer", "iter_01", 1, "finished", exit_code=0, finished="2026-11-08T10:07:00+00:00")
    _receipt(inv, "iter_02/artefacts", "explorer", "iter_02", 1, "finished", exit_code=0, started="2026-11-08T12:00:00+00:00", finished="2026-11-08T12:05:00+00:00")
    return inv


def _status(inv: Path, tmp_path: Path, hint: str | None = None) -> dict:
    """The command's answer, with the lead's cost read from an empty projects folder."""
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    return rs.build_status(str(inv), hint, projects_dir=str(projects))


def _lead_session(inv: Path, tmp_path: Path, started: str, writes: list[tuple[str, str]], skill: str = "diagnose",
                  usage: dict | None = None) -> None:
    """Write the lead's Claude Code session file: its start, then each log write.

    Parameters
    ----------
    inv : Path
        The investigation folder, under ``<repo>/investigations/``.
    tmp_path : Path
        Holds the ``projects`` folder `_status` reads.
    started : str
        The time of the skill's command line, the run's start.
    writes : list[tuple[str, str]]
        ``(time, text)`` per Edit of the log, e.g. ``("…T09:30:00Z", "## Iteration 2\\n")``.
    skill : str
        ``diagnose`` or ``orchestrate``.
    usage : dict | None
        Each write's API usage; one input token when None.
    """
    import spend

    folder = tmp_path / "projects" / spend._project_slug(str(inv.parent.parent))
    lines = [{"type": "user", "timestamp": started, "message": {"content": f"<command-name>/{skill}</command-name>"}}]
    for n, (at, text) in enumerate(writes):
        edit = {"type": "tool_use", "name": "Edit", "input": {"file_path": str(inv / "INVESTIGATION-LOG.md"), "new_string": text}}
        lines.append({"type": "assistant", "entrypoint": "cli", "timestamp": at,
                      "message": {"id": f"m{n}", "model": "claude-opus-5-5", "usage": usage or {"input_tokens": 1}, "content": [edit]}})
    _write(folder / "lead.jsonl", "".join(json.dumps(x) + "\n" for x in lines))


# ---------------------------------------------------------------------------
# Model forensics
# ---------------------------------------------------------------------------


def test_forensics_running_executor_names_the_box_and_round(forensics, tmp_path):
    s = _status(forensics, tmp_path)
    assert s["pipeline"]["name"] == "diagnose"
    assert s["state"] == "running"
    assert s["current"] == {"phase": "3", "box": "run", "round": "2 of 4"}


def test_forensics_executor_samples_count_finished_conditions(forensics, tmp_path):
    ex = [h for h in _status(forensics, tmp_path)["helpers"] if h["role"] == "Executor"]
    # The running condition counts 0 until it finishes; its size comes from the request
    assert ex[0]["samples"] == {"done": 100, "total": 200}


def _executor_counts(inv: Path, tmp_path: Path, pairs: dict, overrides: dict, state: str = "running") -> dict | None:
    """The Executor's episode count for a batch run with these pairs and request overrides."""
    ex = _receipt(inv, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "running", alive=True,
                  request={"overrides": overrides})
    _executor_status(ex, pairs, state=state)
    return next(h for h in _status(inv, tmp_path)["helpers"] if h["role"] == "Executor").get("samples")


def test_several_pinned_episodes_multiply_the_estimate(forensics, tmp_path):
    pairs = {"c_a": {"state": "running", "samples_completed": 50}}
    assert _executor_counts(forensics, tmp_path, pairs, {"sample_id": "e1,e2, e3", "epochs": 100}) == {"done": 50, "total": 300}


def test_a_running_conditions_planned_size_beats_the_estimate(forensics, tmp_path):
    # No sample ids or epochs in the request, but the log header named the plan
    pairs = {"c_a": {"state": "running", "samples_completed": 4, "samples_planned": 40}}
    assert _executor_counts(forensics, tmp_path, pairs, {}) == {"done": 4, "total": 40}


def test_conditions_that_will_never_run_count_for_nothing(forensics, tmp_path):
    overrides = {"sample_id": "episode", "epochs": 100}
    excluded = {"c_a": {"state": "success", "samples_completed": 100, "samples_total": 100},
                "c_b": {"state": "excluded-at-preflight", "samples_completed": None, "samples_total": None}}
    assert _executor_counts(forensics, tmp_path, excluded, overrides) == {"done": 100, "total": 100}
    # A stopped run's untouched conditions are not still to come
    stopped = {"c_a": {"state": "success", "samples_completed": 100, "samples_total": 100},
               "c_b": {"state": "interrupted", "samples_completed": None, "samples_total": None}}
    assert _executor_counts(forensics, tmp_path, stopped, {}, state="interrupted") == {"done": 100, "total": 100}


def test_helper_states_from_receipts(forensics, tmp_path):
    states = {h["tag"]: h["state"] for h in _status(forensics, tmp_path)["helpers"]}
    # A non-zero exit is a failure; a live process is running
    assert states == {"iter_00": "finished", "iter_01": "failed", "iter_02": "running"}


def test_a_dead_running_receipt_is_failed_and_the_log_names_the_box(forensics, tmp_path):
    # A stopped Executor leaves its receipt saying "running"
    _receipt(forensics, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "running", alive=False)
    s = _status(forensics, tmp_path)
    assert [h["state"] for h in s["helpers"] if h["role"] == "Executor"] == ["failed"]
    # With no helper at work, the lead's newest log heading names the box
    assert s["current"] == {"phase": "3", "box": "pre-register", "round": "2 of 4"}


def test_a_running_critic_lights_its_side_box(forensics, tmp_path):
    _receipt(forensics, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "finished", exit_code=0, finished="2026-11-08T11:00:00+00:00")
    _receipt(forensics, "iterations/02_round2/artefacts", "critic", "iter_02", 1, "running", alive=True,
             started="2026-11-08T11:30:00+00:00", request={"mode": "diff_review"})
    assert _status(forensics, tmp_path)["current"] == {"phase": "3", "box": "design", "isSide": True, "round": "2 of 4"}


def test_a_helper_far_ahead_of_the_log_claims_no_stage(forensics, tmp_path):
    # A phase-4 helper while the log is still in phase 3 is fine (the log lags);
    # the log back in phase 1 with an iteration-2 helper is not
    _write(forensics / "INVESTIGATION-LOG.md", FORENSICS_LOG.split("## Phase 2")[0])
    assert _status(forensics, tmp_path)["current"] is None


def test_forensics_explanation_is_the_rank_one_candidate(forensics, tmp_path):
    text = _status(forensics, tmp_path)["explanation"]
    assert text.startswith("C1 deployment_goal: The model takes being deployed as its goal")
    assert len(text) <= rs.MAX_EXPLANATION


def test_forensics_steps_name_ledger_changes(forensics, tmp_path):
    assert _status(forensics, tmp_path)["steps"] == [
        "Input: scope agreed",
        "Scoping done",
        "Read the reasoning: X1, X2 added",
        "Round 1: C2 out; X3 added",
    ]


def test_suspended_run(forensics, tmp_path):
    _write(forensics / "RESUME.md", "# RESUME\n")
    assert _status(forensics, tmp_path)["state"] == "suspended"


def test_finished_run_points_at_the_report(forensics, tmp_path):
    _write(forensics / "RESUME.md", "# RESUME\n")
    _write(forensics / "FINDINGS-REPORT.md", "# Findings\n")
    s = _status(forensics, tmp_path)
    # A report wins over a resume note; the last phase is lit
    assert s["state"] == "finished"
    assert s["report"] == "FINDINGS-REPORT.md"
    assert s["current"] == {"phase": "5", "box": ""}
    assert s["finishedAt"]
    assert "Round 2: no candidate out" not in s["steps"]  # round 2 had no Ledger Delta
    assert s["steps"][-1] == "Round 2 done"


def test_no_receipts_yet(tmp_path):
    inv = tmp_path / "investigations" / "fresh"
    _write(inv / "INVESTIGATION-LOG.md", "# Investigation Log\n\n## Agreed Scope\n- x\n\n## Phase 1: Initial Scoping\n")
    s = _status(inv, tmp_path)
    assert s["pipeline"]["name"] == "diagnose" and s["helpers"] == [] and "startedAt" not in s
    # Phase 1 has boxes and nothing names one yet: unknown beats a guess
    assert s["current"] is None


def test_an_empty_folder_takes_the_hint_and_claims_no_stage(tmp_path):
    inv = tmp_path / "investigations" / "brand_new"
    inv.mkdir(parents=True)
    s = _status(inv, tmp_path, hint="orchestrate")
    assert s["pipeline"]["name"] == "orchestrate"
    assert s["current"] is None and s["steps"] == [] and s["explanation"] is None
    assert s["cost"] == {"usd": 0}


# ---------------------------------------------------------------------------
# Science of evals
# ---------------------------------------------------------------------------


def test_orchestrate_lead_between_helpers(orchestrate, tmp_path):
    s = _status(orchestrate, tmp_path)
    assert s["pipeline"]["name"] == "orchestrate"
    # Round 2's Explorer has finished and no Executor has started: between the two
    assert s["current"] == {"phase": "2", "box": "explore", "round": "2", "between": {"from": "explore", "to": "execute"}}
    assert [h["role"] for h in s["helpers"]] == ["Environment Explorer", "Environment Explorer"]
    assert s["startedAt"] == "2026-11-08T10:00:00+00:00"
    assert "stage: phase 2 Iterate, between Explore and Execute, round 2" in rs.render(s)


def test_orchestrate_explanation_and_steps(orchestrate, tmp_path):
    s = _status(orchestrate, tmp_path)
    assert s["explanation"] == "Framing the sabotage as preventing weapons use raises concealment."
    # Asides dropped and wide symbols swapped for plain ones; round 2's
    # finished Explorer counts before the round is written up
    assert s["steps"] == ["Discussion: scope agreed", "Round 1: Iterate - switch environment to agentic_misalignment.",
                          "Round 2, Explore done"]


# ---------------------------------------------------------------------------
# Fuller material for the run tab
# ---------------------------------------------------------------------------


def test_question_and_full_explanation_for_both_pipelines(forensics, orchestrate, tmp_path):
    s = _status(forensics, tmp_path)
    assert s["question"] == "The model scores 14% rather than 90% on the arithmetic test."
    assert s["explanationFull"] == ("C1 deployment_goal: The model takes being deployed as its goal and fails "
                                    "on purpose to get there. It reads the threshold as the route.")
    assert s["explanationFull"].startswith(s["explanation"].removesuffix("..."))
    o = _status(orchestrate, tmp_path)
    assert o["question"].startswith("Does framing a sabotage task as preventing harm")
    assert o["explanationFull"] == o["explanation"]


def test_step_details_follow_the_steps_in_full(forensics, orchestrate, tmp_path):
    for inv in (forensics, orchestrate):
        s = _status(inv, tmp_path)
        assert [d["title"] for d in s["stepDetails"]] == [re.split(r":| done$", st)[0] for st in s["steps"]]
    assert _status(forensics, tmp_path)["stepDetails"] == [
        {"title": "Input", "detail": "scope agreed"},
        {"title": "Scoping", "detail": "done"},
        {"title": "Read the reasoning", "detail": "X1, X2 added"},
        {"title": "Round 1", "detail": "C2 out; X3 added"},
    ]


def test_helpers_carry_an_excerpt_of_their_brief_when_it_has_one(forensics, tmp_path):
    _receipt(forensics, "iterations/02_round2/artefacts", "analyst", "iter_02", 1, "finished", exit_code=0,
             started="2026-11-08T11:00:00+00:00", finished="2026-11-08T11:30:00+00:00",
             request={"topic": "Grade **all** 200 transcripts for deliberate underperformance. " * 20})
    helpers = _status(forensics, tmp_path)["helpers"]
    task = next(h for h in helpers if h["role"] == "Analyst" and h["tag"] == "iter_02")["task"]
    assert task.startswith("Grade all 200 transcripts") and len(task) <= rs.MAX_TASK
    # The Executor's request has no prose to excerpt
    assert "task" not in next(h for h in helpers if h["role"] == "Executor")


def test_no_receipts_and_no_session_gives_no_timings(tmp_path):
    inv = tmp_path / "investigations" / "fresh"
    _write(inv / "INVESTIGATION-LOG.md", "# Investigation Log\n\n## Agreed Scope\n- x\n\n## Phase 1: Initial Scoping\n")
    assert "timings" not in _status(inv, tmp_path)


def _spans(v):
    """`v` without its cost fields, to compare times alone."""
    if isinstance(v, dict):
        return {k: _spans(x) for k, x in v.items() if k not in ("usd", "leadApprox")}
    return [_spans(x) for x in v] if isinstance(v, list) else v


def test_timings_of_a_running_loop_in_its_second_round(forensics, tmp_path):
    _lead_session(forensics, tmp_path, "2026-11-08T07:45:00Z", [
        ("2026-11-08T07:50:00Z", "## Agreed Scope\n- ...\n"),
        ("2026-11-08T07:55:00Z", "## Phase 1: Initial Scoping\n"),
        ("2026-11-08T08:30:00Z", "## Phase 2: Read the CoT\n"),
        ("2026-11-08T08:40:00Z", "## Iteration 1\n"),
        ("2026-11-08T09:30:00Z", "## Iteration 2\n"),
    ])
    # Mapper 08:00-08:05 (phase 1); Analyst 09:00-09:00:01 (round 1); Executor from 10:00, running (round 2)
    s = _status(forensics, tmp_path)
    _check_contract(s)
    phases = _spans(s["timings"]["phases"])
    t = "2026-11-08T{}+00:00".format
    assert phases[:3] == [
        {"id": "0", "startedAt": t("07:45:00"), "finishedAt": t("07:55:00")},
        {"id": "1", "startedAt": t("07:55:00"), "finishedAt": t("08:30:00"),
         "boxes": [{"id": "triage", "startedAt": t("08:00:00"), "finishedAt": t("08:05:00")}]},
        # The lead reads alone: the whole phase
        {"id": "2", "startedAt": t("08:30:00"), "finishedAt": t("08:40:00"),
         "boxes": [{"id": "read-cot", "startedAt": t("08:30:00"), "finishedAt": t("08:40:00")}]},
    ]
    loop = phases[3]
    # The running phase has no finish, and nothing after it is listed
    assert len(phases) == 4 and loop == {"id": "3", "startedAt": t("08:40:00"), "rounds": loop["rounds"]}
    assert loop["rounds"] == [
        {"round": "1", "startedAt": t("08:40:00"), "finishedAt": t("09:30:00"),
         # Pre-registering has no known neighbour before it, so it is left out;
         # updating runs from the Analyst's finish to the round's end
         "boxes": [{"id": "measure", "startedAt": t("09:00:00"), "finishedAt": t("09:00:01")},
                   {"id": "update", "startedAt": t("09:00:01"), "finishedAt": t("09:30:00")}]},
        {"round": "2", "startedAt": t("09:30:00"),
         "boxes": [{"id": "run", "startedAt": t("10:00:00")}]},
    ]


def test_a_log_skeleton_written_at_once_times_only_its_first_heading(forensics, tmp_path):
    _lead_session(forensics, tmp_path, "2026-11-08T07:45:00Z", [
        ("2026-11-08T07:50:00Z", "## Agreed Scope\n\n## Phase 1: Initial Scoping\n\n## Iteration 1\n"),
    ])
    phases = _spans(_status(forensics, tmp_path)["timings"]["phases"])
    # Phase 1 starts with its Mapper, not with the skeleton
    assert phases[1]["startedAt"] == "2026-11-08T08:00:00+00:00"
    assert phases[0] == {"id": "0", "startedAt": "2026-11-08T07:45:00+00:00", "finishedAt": "2026-11-08T08:00:00+00:00"}


def test_orchestrate_timings_put_round_boxes_under_rounds(orchestrate, tmp_path):
    _lead_session(orchestrate, tmp_path, "2026-11-08T09:00:00Z", [
        ("2026-11-08T09:20:00Z", "## Iteration 1\n"),
        ("2026-11-08T11:50:00Z", "## Iteration 2\n"),
    ], skill="orchestrate")
    phases = _spans(_status(orchestrate, tmp_path)["timings"]["phases"])
    t = "2026-11-08T{}+00:00".format
    assert phases[0] == {"id": "1", "startedAt": t("09:00:00"), "finishedAt": t("09:20:00")}
    r1, r2 = phases[1]["rounds"]
    # Explorer 10:00-10:07: the hypothesis box runs from the round's start to it
    assert r1["boxes"][:2] == [{"id": "hypothesis", "startedAt": t("09:20:00"), "finishedAt": t("10:00:00")},
                               {"id": "explore", "startedAt": t("10:00:00"), "finishedAt": t("10:07:00")}]
    assert r2["startedAt"] == t("11:50:00") and "finishedAt" not in r2


# ---------------------------------------------------------------------------
# The run tab's third round: places, side helpers, between boxes, box steps,
# costs and one box's material
# ---------------------------------------------------------------------------


def test_helpers_say_which_box_they_belong_to(forensics, tmp_path):
    _receipt(forensics, "iterations/02_round2/artefacts", "critic", "iter_02", 1, "finished", exit_code=0,
             started="2026-11-08T09:40:00+00:00", finished="2026-11-08T09:50:00+00:00", request={"mode": "diff_review"})
    by_tag = {(h["role"], h["tag"]): h for h in _status(forensics, tmp_path)["helpers"]}
    assert {k: by_tag[("Mapper", "iter_00")][k] for k in ("phase", "box")} == {"phase": "1", "box": "triage"}
    assert "round" not in by_tag[("Mapper", "iter_00")]
    assert {k: by_tag[("Executor", "iter_02")].get(k) for k in ("phase", "round", "box", "side")} == \
        {"phase": "3", "round": "2", "box": "run", "side": None}
    # A pipeline side box keeps its own id and is not "side" work
    critic = by_tag[("Critic", "iter_02")]
    assert critic["box"] == "diff-review" and "side" not in critic


def test_a_concurrent_side_helper_never_moves_the_lit_box(orchestrate, tmp_path):
    _lead_session(orchestrate, tmp_path, "2026-11-08T09:00:00Z", [
        ("2026-11-08T09:20:00Z", "## Iteration 1\n"),
        ("2026-11-08T11:50:00Z", "## Iteration 2\n"),
    ], skill="orchestrate")
    _receipt(orchestrate, "iter_02/artefacts", "executor", "iter_02", 1, "running", alive=True, started="2026-11-08T12:10:00+00:00")
    # The newest helper at work is an Analyst on a construal pilot, off the pipeline
    _receipt(orchestrate, "construal_pilot/artefacts", "analyst", "pilot", 1, "running", alive=True,
             started="2026-11-08T12:15:00+00:00", request={"topic": "How the model reads its workspace files."})
    s = _status(orchestrate, tmp_path)
    _check_contract(s)
    assert s["current"] == {"phase": "2", "box": "execute", "round": "2"}
    pilot = next(h for h in s["helpers"] if h["tag"] == "pilot")
    assert pilot == {**pilot, "side": True, "purpose": "construal pilot", "phase": "2", "round": "2"}
    assert "box" not in pilot
    assert "working: Transcript Analyst pilot (side: construal pilot)" in rs.render(s)


def test_a_side_helper_alone_leaves_the_lead_between_boxes(orchestrate, tmp_path):
    _receipt(orchestrate, "construal_pilot/artefacts", "analyst", "pilot", 1, "running", alive=True, started="2026-11-08T12:15:00+00:00")
    assert _status(orchestrate, tmp_path)["current"]["between"] == {"from": "explore", "to": "execute"}


def test_between_two_helper_boxes_in_a_forensics_round(forensics, tmp_path):
    _receipt(forensics, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "finished", exit_code=0, finished="2026-11-08T11:00:00+00:00")
    assert _status(forensics, tmp_path)["current"] == {
        "phase": "3", "box": "run", "round": "2 of 4", "between": {"from": "run", "to": "measure"}}
    # Once the Analyst starts there is nothing between; and before a box of
    # the lead's own (here, updating) the lead is in that box, not between
    _receipt(forensics, "iterations/02_round2/artefacts", "analyst", "iter_02", 1, "finished", exit_code=0,
             started="2026-11-08T11:10:00+00:00", finished="2026-11-08T11:20:00+00:00")
    assert "between" not in _status(forensics, tmp_path)["current"]


def test_a_box_counts_as_a_step_before_the_lead_writes_it_up(forensics, tmp_path):
    ex = _receipt(forensics, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "finished", exit_code=0, finished="2026-11-08T11:00:00+00:00")
    _write(ex.parent / "report_iter_02_executor_1.md",
           "### Summary\n\n11 condition-model pairs were submitted: all **completed**. Ran at width 2.\n\n### Matrix\n")
    s = _status(forensics, tmp_path)
    assert s["stepDetails"][-1] == {"title": "Round 2, Run", "detail": "11 condition-model pairs were submitted: all completed."}
    assert s["steps"][-1].startswith("Round 2, Run: 11 condition-model pairs")
    # The lead's write-up of the round replaces it
    _write(forensics / "INVESTIGATION-LOG.md", FORENSICS_LOG + "### Executor Results\n- ok\n### Decision\n- **Continue**.\n")
    titles = [d["title"] for d in _status(forensics, tmp_path)["stepDetails"]]
    assert titles[-1] == "Round 2" and "Round 2, Run" not in titles


def test_a_failed_box_counts_as_a_failed_step(forensics, tmp_path):
    _receipt(forensics, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "finished", exit_code=2, finished="2026-11-08T11:00:00+00:00")
    assert _status(forensics, tmp_path)["stepDetails"][-1] == {"title": "Round 2, Run", "detail": "failed"}


def test_an_older_run_without_receipts_has_none_of_the_new_fields(tmp_path):
    inv = tmp_path / "investigations" / "old"
    _write(inv / "INVESTIGATION-LOG.md", FORENSICS_LOG)
    s = _status(inv, tmp_path)
    assert s["helpers"] == [] and "between" not in (s["current"] or {})
    assert [d["title"] for d in s["stepDetails"]] == ["Input", "Scoping", "Read the reasoning", "Round 1"]
    assert "timings" not in s


def test_costs_sit_where_they_were_spent(forensics, tmp_path):
    import spend

    usage = {"input_tokens": 1_000_000}
    per_call = spend._price_call("claude-opus-5-5", usage)
    # One lead call per log write: 07:55 (phase 1), 08:40 (round 1), 09:30 and 10:30 (round 2)
    _lead_session(forensics, tmp_path, "2026-11-08T07:45:00Z", [
        ("2026-11-08T07:55:00Z", "## Phase 1: Initial Scoping\n"),
        ("2026-11-08T08:40:00Z", "## Iteration 1\n"),
        ("2026-11-08T09:30:00Z", "## Iteration 2\n"),
        ("2026-11-08T10:30:00Z", "- note\n"),
    ], usage=usage)
    # The Mapper's own session cost $2
    mapper = forensics / "artefacts" / "mapper"
    _write(mapper / "cost_iter_00_mapper_1.json", json.dumps({"prism_cost": 1, "agent": "Mapper", "total_cost_usd": 2.0}))
    s = _status(forensics, tmp_path)
    phases = {t["id"]: t for t in s["timings"]["phases"]}
    triage = next(b for b in phases["1"]["boxes"] if b["id"] == "triage")
    assert triage["usd"] == 2.0 and "leadApprox" not in triage
    assert phases["1"]["usd"] == round(2.0 + per_call, 2) and phases["1"]["leadApprox"]
    r2 = phases["3"]["rounds"][1]
    # The 10:30 call falls inside the running Executor's box; 09:30 before any box
    run = next(b for b in r2["boxes"] if b["id"] == "run")
    assert run["usd"] == round(per_call, 2) and run["leadApprox"]
    assert r2["usd"] == round(2 * per_call, 2)
    # The phases add up to the total, near enough
    assert abs(sum(t.get("usd", 0) for t in phases.values()) - s["cost"]["usd"]) < 0.05


def test_box_material_for_a_helper_box(forensics, tmp_path):
    ex = _receipt(forensics, "iterations/02_round2/artefacts", "executor", "iter_02", 1, "finished", exit_code=0,
                  finished="2026-11-08T11:00:00+00:00", request={"overrides": {"epochs": 3}})
    _write(ex.parent / "report_iter_02_executor_1.md", "### Summary\n\n" + "All done. " * 400)
    _write(forensics / "INVESTIGATION-LOG.md", FORENSICS_LOG + "### Executor Results\n- 11 pairs completed\n### Analyst Results\n- other box\n")
    m = rs.box_material(str(forensics), "3:2:run")
    assert m["version"] == 1 and m["box"] == {"phase": "3", "round": "2", "box": "run"}
    (h,) = m["helpers"]
    assert h["role"] == "Executor" and h["tag"] == "iter_02"
    assert json.loads(h["task"]) == {"overrides": {"epochs": 3}}
    assert h["result"].startswith("### Summary") and len(h["result"]) <= rs.MAX_MATERIAL
    # Only the log blocks that map onto this box
    assert m["log"] == "### Executor Results\n- 11 pairs completed"


def test_box_material_for_a_lead_box_and_a_phase_without_boxes(forensics, orchestrate, tmp_path):
    m = rs.box_material(str(forensics), "3:1:pre-register")
    assert m["helpers"] == [] and m["log"].startswith("### Pre-registered Predictions")
    d = rs.box_material(str(orchestrate), "1:discussion")
    assert "box" in d and d["box"] == {"phase": "1", "box": "discussion"}
    assert "Research question" in d["log"]


@pytest.mark.parametrize("which", ["3:run", "1:2:triage", "3:2:nope", "9:x", "run", "2:1:discussion"])
def test_an_unknown_box_is_an_error(forensics, which, capsys):
    with pytest.raises(ValueError):
        rs.box_material(str(forensics), which)
    assert rs.main([str(forensics), "--box", which, "--json"]) == 2
    assert capsys.readouterr().err.strip()


def test_plain_text_summary_shows_phase_times(forensics, tmp_path):
    _lead_session(forensics, tmp_path, "2026-11-08T07:45:00Z", [("2026-11-08T07:55:00Z", "## Phase 1: Initial Scoping\n")])
    assert "phase 0 Input: from 07:45 to 07:55 UTC" in rs.render(_status(forensics, tmp_path))


# ---------------------------------------------------------------------------
# Contract, data file and read-only behaviour
# ---------------------------------------------------------------------------


def _check_contract(s: dict) -> None:
    """Fail unless `s` is an answer the panel's `parseStatus` accepts in full.

    Mirrors `prism-status.ts`: required keys and types, known helper states
    (an unknown one is silently dropped there, so it's an error here),
    camelCase keys, and a `current.box` that is a main box of its phase.
    """
    assert s["version"] == 1 and isinstance(s["investigation"], str)
    p = s["pipeline"]
    assert p["name"] in ("diagnose", "orchestrate") and isinstance(p["title"], str)
    for ph in p["phases"]:
        assert isinstance(ph["id"], str) and isinstance(ph["title"], str) and isinstance(ph["boxes"], list)
        for b in ph["boxes"]:
            for box in (b, b.get("side")):
                if box is not None:
                    assert all(isinstance(box[k], str) for k in ("id", "title", "role"))
    assert isinstance(s["state"], str)
    assert isinstance(s["cost"]["usd"], (int, float))
    cur = s["current"]
    if cur is not None:
        phase = next(ph for ph in p["phases"] if ph["id"] == cur["phase"])
        assert isinstance(cur["box"], str)
        if phase["boxes"]:
            assert cur["box"] in [b["id"] for b in phase["boxes"]]
    for h in s["helpers"]:
        assert h["state"] in ("launching", "running", "finished", "failed")
        assert all(isinstance(h[k], str) for k in ("role", "tag", "startedAt"))
    assert all(isinstance(x, str) for x in s["steps"])
    assert s["explanation"] is None or isinstance(s["explanation"], str)
    assert s["report"] is None or isinstance(s["report"], str)

    # ---- The optional fuller fields ----
    for key in ("question", "explanationFull"):
        assert key not in s or isinstance(s[key], str)
    assert len(s["stepDetails"]) == len(s["steps"])
    assert all(isinstance(d["title"], str) and isinstance(d["detail"], str) for d in s["stepDetails"])
    assert all(isinstance(h.get("task", ""), str) for h in s["helpers"])

    def check_span(x: dict) -> None:
        for k in ("startedAt", "finishedAt"):
            assert k not in x or isinstance(x[k], str)

    ids = {ph["id"]: ph for ph in p["phases"]}
    for t in (s.get("timings") or {"phases": []})["phases"]:
        check_span(t)
        box_ids = {b["id"] for b in ids[t["id"]]["boxes"]} | {b["side"]["id"] for b in ids[t["id"]]["boxes"] if b.get("side")}
        for r in t.get("rounds", []):
            assert isinstance(r["round"], str)
            check_span(r)
        for b in t.get("boxes", []) + [b for r in t.get("rounds", []) for b in r.get("boxes", [])]:
            assert b["id"] in box_ids
            check_span(b)
        for x in [t] + t.get("boxes", []) + [y for r in t.get("rounds", []) for y in [r] + r.get("boxes", [])]:
            assert "usd" not in x or isinstance(x["usd"], (int, float))
            assert x.get("leadApprox", True) is True

    # ---- The third round's fields ----
    if cur is not None and "between" in cur:
        phase = next(ph for ph in p["phases"] if ph["id"] == cur["phase"])
        assert cur["between"]["from"] == cur["box"]
        assert cur["between"]["to"] in [b["id"] for b in phase["boxes"]]
    for h in s["helpers"]:
        if h.get("side"):
            assert h["side"] is True and isinstance(h["purpose"], str) and len(h["purpose"]) < 80 and "box" not in h
        elif "box" in h:
            assert h["box"] in {b["id"] for b in ids[h["phase"]]["boxes"]} | {b["side"]["id"] for b in ids[h["phase"]]["boxes"] if b.get("side")}
        assert all(isinstance(h.get(k, ""), str) for k in ("phase", "round", "box"))

    def keys(v):
        if isinstance(v, dict):
            for k, x in v.items():
                yield k
                yield from keys(x)
        elif isinstance(v, list):
            for x in v:
                yield from keys(x)

    assert not [k for k in keys(s) if "_" in k], "keys must be camelCase"


@pytest.mark.parametrize("fixture", ["forensics", "orchestrate"])
def test_answer_matches_the_panel_contract(fixture, request, tmp_path, capsys):
    inv = request.getfixturevalue(fixture)
    _check_contract(_status(inv, tmp_path))
    # The --json output is one JSON object of the same shape
    assert rs.main([str(inv), "--json"]) == 0
    _check_contract(json.loads(capsys.readouterr().out))


def test_data_file_keeps_the_panel_ids():
    # The panel's built-in copy (FALLBACK_PIPELINES) uses these ids: changing
    # one would split older panels from newer Prisms
    pipes = rs.load_pipelines(str(REPO_ROOT / ".claude" / "docs" / "pipelines.json"))
    ids = {n: [(ph["id"], [b["id"] for b in ph["boxes"]]) for ph in p["phases"]] for n, p in pipes.items()}
    assert ids == {
        "diagnose": [("0", []), ("1", ["triage", "resample", "operationalise"]), ("2", ["read-cot"]),
                     ("3", ["design", "pre-register", "run", "measure", "update"]), ("4", ["predict"]), ("5", [])],
        "orchestrate": [("1", []), ("2", ["hypothesis", "explore", "execute", "analyse", "interpret"]), ("3", [])],
    }


def test_data_file_mappings_name_real_boxes():
    for name, p in rs.load_pipelines().items():
        boxes = {ph["id"]: {b["id"] for b in ph["boxes"]} for ph in p["phases"]}
        for rule in p["helperBoxes"] + p["logBoxes"]:
            assert rule["box"] in boxes[rule["phase"]], (name, rule)
        for rule in p["phaseHeadings"]:
            assert rule["phase"] in boxes, (name, rule)
            re.compile(rule["pattern"])


def test_the_command_writes_nothing(forensics, tmp_path):
    def snapshot(root: Path) -> dict:
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")}

    (tmp_path / "projects").mkdir()
    before = snapshot(tmp_path)
    _status(forensics, tmp_path)
    rs.render(_status(forensics, tmp_path))
    assert snapshot(tmp_path) == before


def test_plain_text_summary(forensics, tmp_path):
    text = rs.render(_status(forensics, tmp_path))
    assert "stage: phase 3 Iterate, Run, round 2 of 4" in text
    assert "working: Executor iter_02" in text and "100/200 samples" in text
