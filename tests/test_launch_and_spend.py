"""Offline tests for the detached launcher (D04, D28), the cost sidecar (D12)
and the turn cap threading (D14), plus scripts/spend.py."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import launch_subagent as ls
import spend

ROOT = Path(__file__).resolve().parent.parent


def stub_entry(tmp_path, *, exit_code=0, sleep=0.0, cost=True):
    """A stand-in for a sub-agent CLI: prints a report, writes a cost sidecar."""
    script = tmp_path / "stub_agent.py"
    script.write_text(
        "import os, sys, json, time\n"
        f"time.sleep({sleep})\n"
        "print('### Summary')\n"
        "print('stub report')\n"
        "p = os.environ.get('PRISM_COST_FILE')\n"
        f"if p and {cost}:\n"
        "    json.dump({'prism_cost': 1, 'agent': 'Stub', 'total_cost_usd': 0.25, 'num_turns': 3, 'subtype': 'success'}, open(p, 'w'))\n"
        "print('stderr line', file=sys.stderr)\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def make_request(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({"experiment_name": "x"}))
    return str(p)


# ---------------------------------------------------------------------------
# D04 — persistent requests dir, detached, receipt + status; D28 — naming
# ---------------------------------------------------------------------------


def test_launch_persists_request_names_per_invocation_and_records_exit(tmp_path):
    inv = tmp_path / "investigations" / "demo"
    req = make_request(tmp_path)
    r1 = ls.launch("critic", req, str(inv), "iter0", entry=stub_entry(tmp_path))
    r2 = ls.launch("critic", req, str(inv), "iter0", entry=stub_entry(tmp_path))

    # Requests persisted under investigations/<name>/requests/, absolute paths.
    assert r1["request"] == str(inv / "requests" / "iter0_critic_1.json")
    assert r2["request"] == str(inv / "requests" / "iter0_critic_2.json")
    assert os.path.isabs(r1["report"]) and os.path.isabs(r1["stderr"])
    # Per-invocation output names: the second Critic never overwrites the first.
    assert r1["report"].endswith("artefacts/critic/report_iter0_critic_1.md")
    assert r2["report"].endswith("artefacts/critic/report_iter0_critic_2.md")
    assert r1["cost"].endswith("cost_iter0_critic_1.json")

    # Detached: the supervisor runs in a different session from the test.
    assert os.getsid(r1["supervisor_pid"]) != os.getsid(os.getpid())

    for r in (r1, r2):
        final = ls.wait_for(r["receipt"], timeout=60, interval=0.2)
        assert final["state"] == "finished" and final["exit_code"] == 0, final
        assert Path(r["report"]).read_text().startswith("### Summary")
        assert "stderr line" in Path(r["stderr"]).read_text()
        assert json.load(open(r["cost"]))["total_cost_usd"] == 0.25

    listing = ls.list_receipts(str(inv))
    assert [(x["role"], x["n"]) for x in listing] == [("critic", 1), ("critic", 2)]
    text = ls.describe(final)
    assert "critic iter0 #2: finished exit=0" in text and "cost: $0.25" in text


def test_launch_cli_round_trip_and_wait_exit_codes(tmp_path):
    inv = tmp_path / "investigations" / "demo"
    req = make_request(tmp_path)
    # The stub must still be running when the first `wait --timeout 0.5`
    # subprocess checks it. 2s was too tight once earlier tests leave detached
    # workers loading the host (intermittent failures after the Wave 1 merges);
    # 10s keeps the window wide without weakening the assertion. The second
    # wait (timeout 60) still covers the stub's full run.
    entry = stub_entry(tmp_path, sleep=10.0)
    r = ls.launch("executor", req, str(inv), "00_resample", artefacts_dir=str(inv / "iterations" / "00_resample" / "artefacts"), entry=entry)
    assert r["report"].endswith("iterations/00_resample/artefacts/executor/report_00_resample_executor_1.md")
    script = str(ROOT / "scripts" / "launch_subagent.py")
    rc = subprocess.run([sys.executable, script, "wait", r["receipt"], "--timeout", "0.5", "--interval", "0.2"], capture_output=True, text=True)
    assert rc.returncode == 3 and ("running" in rc.stdout or "launching" in rc.stdout)
    rc = subprocess.run([sys.executable, script, "wait", r["receipt"], "--timeout", "60", "--interval", "0.2"], capture_output=True, text=True)
    assert rc.returncode == 0 and "finished exit=0" in rc.stdout
    rc = subprocess.run([sys.executable, script, "status", r["receipt"], "--json"], capture_output=True, text=True)
    assert json.loads(rc.stdout)["exit_code"] == 0
    rc = subprocess.run([sys.executable, script, "list", str(inv)], capture_output=True, text=True)
    assert "executor 00_resample #1" in rc.stdout


def test_launch_rejects_bad_role_and_missing_request(tmp_path):
    with pytest.raises(ValueError):
        ls.launch("nope", make_request(tmp_path), str(tmp_path / "inv"), "t")
    with pytest.raises(ValueError):
        ls.launch("critic", str(tmp_path / "missing.json"), str(tmp_path / "inv"), "t")


def test_build_command_uses_unresolved_target_root():
    cmd = ls.build_command("analyst", "/abs/req.json", "/target")
    assert cmd[:6] == ["uv", "run", "--with", "claude-agent-sdk", "--with", "python-dotenv"]
    assert cmd[6] == "python" and cmd[7].endswith("subagents/transcript_analyst/main.py")
    assert cmd[8:] == ["/abs/req.json", "--cwd", "/target"]


def test_nonzero_exit_is_recorded(tmp_path):
    inv = tmp_path / "inv"
    r = ls.launch("mapper", make_request(tmp_path), str(inv), "phase1", entry=stub_entry(tmp_path, exit_code=2, cost=False))
    final = ls.wait_for(r["receipt"], timeout=60, interval=0.2)
    assert final["exit_code"] == 2
    assert "exit=2" in ls.describe(final)


# ---------------------------------------------------------------------------
# D12 — cost sidecar from the SDK result; D14 — max_turns threading
# ---------------------------------------------------------------------------


def _fake_result_message():
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="success",
        duration_ms=1234,
        duration_api_ms=1000,
        is_error=False,
        num_turns=7,
        session_id="s",
        stop_reason="end_turn",
        total_cost_usd=0.4321,
        usage={"input_tokens": 100, "output_tokens": 50},
        result="### Summary\nok",
    )


def test_cost_sidecar_written_from_result_message(tmp_path, monkeypatch, capsys):
    from subagents import runner

    path = tmp_path / "artefacts" / "executor" / "cost_iter1_executor_1.json"
    monkeypatch.setenv(runner.COST_FILE_ENV, str(path))
    runner._write_cost_sidecar("Experiment executor", "claude-opus-4-8", None, _fake_result_message())
    data = json.load(open(path))
    assert data["prism_cost"] == 1
    assert data["agent"] == "Experiment executor"
    assert data["total_cost_usd"] == 0.4321 and data["num_turns"] == 7
    assert data["usage"]["input_tokens"] == 100
    assert data["route"] == "direct"
    assert "[cost] agent='Experiment executor' model=claude-opus-4-8 total_cost_usd=0.4321 turns=7" in capsys.readouterr().err

    monkeypatch.delenv(runner.COST_FILE_ENV)
    runner._write_cost_sidecar("X", "m", "openrouter", _fake_result_message())  # only logs


def test_max_turns_threaded_to_options(monkeypatch):
    """`subagent_max_turns` reaches ClaudeAgentOptions; default stays 100."""
    from subagents import runner

    captured = {}

    class FakeOptions:
        def __init__(self, **kw):
            captured.update(kw)
            self.max_turns = kw.get("max_turns")

    async def fake_query(prompt, options):
        yield _fake_result_message()

    monkeypatch.setattr(runner, "ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr(runner, "query", fake_query)

    out = asyncio.run(runner.run_agent("p", "s", ["Bash"], [], "Agent", max_turns=250))
    assert out == "### Summary\nok"
    assert captured["max_turns"] == 250
    asyncio.run(runner.run_agent("p", "s", ["Bash"], [], "Agent"))
    assert captured["max_turns"] == runner.DEFAULT_MAX_TURNS == 100


def test_no_output_error_names_the_subtype(monkeypatch):
    from subagents import runner
    from claude_agent_sdk import ResultMessage

    class FakeOptions:
        def __init__(self, **kw):
            self.max_turns = kw.get("max_turns")

    async def fake_query(prompt, options):
        yield ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=100, session_id="s", stop_reason=None, result=None)

    monkeypatch.setattr(runner, "ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr(runner, "query", fake_query)
    with pytest.raises(RuntimeError, match=r"subtype=error_max_turns.*num_turns=100.*max_turns=100"):
        asyncio.run(runner.run_agent("p", "s", ["Bash"], [], "Analyst"))


def test_executor_agent_reads_max_turns_and_persists_request(tmp_path, monkeypatch):
    """The `**data` hazard: the Executor's prompt builder must read the field."""
    from subagents.experiment_executor import agent

    seen = {}

    async def fake_run_agent(**kw):
        seen.update(kw)
        return "### Summary"

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    exp = tmp_path / "exp"
    exp.mkdir()
    asyncio.run(agent.run_experiment_executor(cwd=None, experiment_name="x", experiment_dir=str(exp), conditions={"baseline": "task.py"}, models=["m"], subagent_max_turns=42))
    assert seen["max_turns"] == 42
    persisted = list((exp / "artefacts" / "executor").glob("request_*.json"))
    assert len(persisted) == 1
    assert json.load(open(persisted[0]))["subagent_max_turns"] == 42
    assert f"`{persisted[0]}`" in seen["prompt"]


# ---------------------------------------------------------------------------
# spend.py
# ---------------------------------------------------------------------------


def test_spend_sums_sidecars_and_eval_usage(tmp_path):
    inv = tmp_path / "investigations" / "demo"
    (inv / "artefacts" / "critic").mkdir(parents=True)
    (inv / "iterations" / "01" / "artefacts" / "analyst").mkdir(parents=True)
    json.dump({"prism_cost": 1, "agent": "Critic", "total_cost_usd": 1.5, "num_turns": 4}, open(inv / "artefacts" / "critic" / "cost_iter0_critic_1.json", "w"))
    json.dump({"prism_cost": 1, "agent": "Analyst", "total_cost_usd": 2.25, "num_turns": 60}, open(inv / "iterations" / "01" / "artefacts" / "analyst" / "cost_iter1_analyst_1.json", "w"))
    json.dump({"prism_cost": 1, "agent": "Analyst", "total_cost_usd": None, "num_turns": 1}, open(inv / "iterations" / "01" / "artefacts" / "analyst" / "cost_iter1_analyst_2.json", "w"))
    run = inv / "iterations" / "01" / "artefacts" / "executor" / "runs" / "r1"
    run.mkdir(parents=True)
    json.dump(
        {
            "schema": 1,
            "run_id": "r1",
            "state": "completed",
            "pairs": {
                "a": {"model_usage": {"openai/gpt-5": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}},
                "b": {"model_usage": {"openai/gpt-5": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}, "anthropic/x": {"total_tokens": 7}}},
            },
        },
        open(run / "status.json", "w"),
    )
    s = spend.collect(str(inv))
    assert s["subagents"]["total_cost_usd"] == 3.75
    assert s["subagents"]["invocations"] == 3
    assert s["subagents"]["by_agent"]["Analyst"] == {"cost_usd": 2.25, "invocations": 2, "turns": 61}
    assert s["evals"]["tokens_by_model"]["openai/gpt-5"] == {"input_tokens": 150, "output_tokens": 15, "total_tokens": 165}
    assert s["evals"]["pairs"] == 2
    text = spend.render(s)
    assert "sub-agent spend: $3.75 over 3 invocation(s)" in text
    assert "openai/gpt-5" in text
