"""Tests for `scripts/headroom.py`, the headroom survey's per-cell tally.

Run with the self-contained recipe from the fix briefs' Shared Preamble:

    uv run --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" \
        --with claude-agent-sdk==0.2.165 --with python-dotenv --with pytest pytest tests/
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "headroom.py"


def _load():
    """Import the script as a module by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("headroom", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hr = _load()


def write_log(path: Path, values: list, *, model: str = "mockllm/model", status: str = "success", errored: int = 0) -> None:
    """Write a .eval log whose samples carry the given `grader` score values.

    Parameters
    ----------
    path : Path
        Destination ``.eval`` path.
    values : list
        One score value per sample (``"C"``, ``0``, a dict, ...).
    model : str
        The header's model.
    status : str
        Log status.
    errored : int
        How many trailing samples carry an error.
    """
    from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalSample, EvalSpec, EvalStats, write_eval_log
    from inspect_ai.log._log import EvalError
    from inspect_ai.model import ChatMessageUser
    from inspect_ai.scorer import Score

    now = datetime.now(timezone.utc).isoformat()
    spec = EvalSpec(
        run_id="r", created=now, task="survey_task", task_id="t", task_version=0, task_file="task.py",
        dataset=EvalDataset(samples=1, sample_ids=["s1"]), model=model, config=EvalConfig(epochs=len(values)),
    )
    samples = []
    for i, v in enumerate(values):
        err = EvalError(message="boom", traceback="tb", traceback_ansi="tb") if i >= len(values) - errored else None
        samples.append(
            EvalSample(
                id="s1", epoch=i + 1, input="hi", target="", messages=[ChatMessageUser(content="hi")],
                scores={"grader": Score(value=v)}, error=err,
            )
        )
    log = EvalLog(version=2, status=status, eval=spec, stats=EvalStats(started_at=now, completed_at=now), samples=samples)
    os.makedirs(path.parent, exist_ok=True)
    write_eval_log(log, str(path))


# ---------------------------------------------------------------------------
# Unit: interval and tags
# ---------------------------------------------------------------------------


def test_zero_of_twenty_is_floor_but_zero_of_fifteen_is_not():
    # The survey's default n rests on this: 0/20 is decisive, 0/15 is not
    assert hr.tag(*hr.wilson(0, 20)) == "[floor]"
    assert hr.tag(*hr.wilson(0, 15)) == ""


def test_ceiling_and_headroom_tags():
    assert hr.tag(*hr.wilson(20, 20)) == "[ceiling]"
    assert hr.tag(*hr.wilson(10, 20)) == ""


def test_wilson_matches_known_value():
    lo, hi = hr.wilson(0, 20)
    assert lo == 0.0
    assert hi == pytest.approx(0.161, abs=0.001)


def test_score_values_flattens_dicts_and_skips_text():
    from inspect_ai.scorer import Score

    out = hr.score_values({"judge": Score(value={"harmful": 1, "leak": "I"}), "grader": Score(value="C")})
    assert out == {"judge.harmful": 1.0, "judge.leak": 0.0, "grader": 1.0}


# ---------------------------------------------------------------------------
# End to end: a survey directory
# ---------------------------------------------------------------------------


def test_survey_tallies_cells_and_skips_failed_logs(tmp_path):
    logs = tmp_path / "logs"
    write_log(logs / "c_aaaa" / "a.eval", ["I"] * 20, model="m/one")
    write_log(logs / "c_bbbb" / "b.eval", ["C"] * 8 + ["I"] * 12 + ["C"], model="m/two", errored=1)
    write_log(logs / "c_cccc" / "c.eval", ["C"] * 5, status="error")

    rows, skipped = hr.survey(logs)
    by_cell = {r["cell"]: r for r in rows}

    assert by_cell["c_aaaa"]["positive"] == 0 and by_cell["c_aaaa"]["n"] == 20
    assert by_cell["c_aaaa"]["tag"] == "[floor]"
    # The errored sample is out of n and counted separately
    assert by_cell["c_bbbb"]["positive"] == 8 and by_cell["c_bbbb"]["n"] == 20
    assert by_cell["c_bbbb"]["errored"] == 1
    assert by_cell["c_bbbb"]["tag"] == ""
    assert "c_cccc" not in by_cell
    assert skipped and "error" in skipped[0]


def test_cli_prints_table_and_refuses_missing_dir(tmp_path, capsys):
    write_log(tmp_path / "logs" / "c_aaaa" / "a.eval", [0] * 20)
    assert hr.main([str(tmp_path / "logs")]) == 0
    out = capsys.readouterr().out
    assert "| c_aaaa | survey_task | mockllm/model | grader | 0/20 | 0%–16% | [floor] | 0 |" in out
    assert hr.main([str(tmp_path / "nope")]) == 2
