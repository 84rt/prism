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
    assert s["current"] == {"phase": "2", "box": "explore", "round": "2"}
    assert [h["role"] for h in s["helpers"]] == ["Environment Explorer", "Environment Explorer"]
    assert s["startedAt"] == "2026-11-08T10:00:00+00:00"


def test_orchestrate_explanation_and_steps(orchestrate, tmp_path):
    s = _status(orchestrate, tmp_path)
    assert s["explanation"] == "Framing the sabotage as preventing weapons use raises concealment."
    # Asides dropped and wide symbols swapped for plain ones
    assert s["steps"] == ["Discussion: scope agreed", "Round 1: Iterate - switch environment to agentic_misalignment."]


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
