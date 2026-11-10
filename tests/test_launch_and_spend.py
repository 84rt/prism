"""Offline tests for the detached launcher (D04, D28), the cost sidecar (D12)
and the turn cap threading (D14), plus scripts/spend.py."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import launch_subagent as ls
import spend

ROOT = Path(__file__).resolve().parent.parent


def stub_entry(tmp_path, *, exit_code=0, gate=None, cost=True):
    """A stand-in for a sub-agent CLI: prints a report, writes a cost sidecar.

    Parameters
    ----------
    tmp_path : Path
        Where the stub script is written.
    exit_code : int
        The stub's exit status.
    gate : Path | None
        When given, the stub blocks until this file exists — a sentinel the
        test creates when it wants the "sub-agent" to finish (no fixed sleep).
    cost : bool
        Whether to write the cost sidecar.

    Returns
    -------
    list[str]
        argv for `launch(..., entry=...)`.
    """
    script = tmp_path / "stub_agent.py"
    script.write_text(
        "import os, sys, json, time\n"
        f"gate = {str(gate) if gate else ''!r}\n"
        "while gate and not os.path.exists(gate):\n"
        "    time.sleep(0.05)\n"
        "print('### Summary')\n"
        "print('stub report')\n"
        "p = os.environ.get('PRISM_COST_FILE')\n"
        f"if p and {cost}:\n"
        "    with open(p, 'w') as f:\n"
        "        json.dump({'prism_cost': 1, 'agent': 'Stub', 'total_cost_usd': 0.25, 'num_turns': 3, 'subtype': 'success'}, f)\n"
        "print('stderr line', file=sys.stderr)\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def launch_request(tmp_path):
    """A minimal request file for the launcher (its content is never interpreted here)."""
    p = tmp_path / "req.json"
    p.write_text(json.dumps({"experiment_name": "x"}))
    return str(p)


def write_json(path: Path, data) -> None:
    """Write JSON through a closed handle, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# D04 — persistent requests dir, detached, receipt + status; D28 — naming
# ---------------------------------------------------------------------------


def test_launch_persists_request_names_per_invocation_and_records_exit(tmp_path):
    inv = tmp_path / "investigations" / "demo"
    req = launch_request(tmp_path)
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
        assert json.loads(Path(r["cost"]).read_text())["total_cost_usd"] == 0.25

    listing = ls.list_receipts(str(inv))
    assert [(x["role"], x["n"]) for x in listing] == [("critic", 1), ("critic", 2)]
    text = ls.describe(final)
    assert "critic iter0 #2: finished exit=0" in text and "cost: $0.25" in text


def test_launch_cli_round_trip_and_wait_exit_codes(tmp_path):
    inv = tmp_path / "investigations" / "demo"
    req = launch_request(tmp_path)
    # The stub holds on a gate file until the "still running" wait has been
    # observed, so the exit-3 assertion cannot race a loaded host (it used to
    # rely on a 10 s sleep outlasting the first wait).
    gate = tmp_path / "gate"
    entry = stub_entry(tmp_path, gate=gate)
    r = ls.launch("executor", req, str(inv), "00_round0", artefacts_dir=str(inv / "iterations" / "00_round0" / "artefacts"), entry=entry)
    assert r["report"].endswith("iterations/00_round0/artefacts/executor/report_00_round0_executor_1.md")
    script = str(ROOT / "scripts" / "launch_subagent.py")
    rc = subprocess.run([sys.executable, script, "wait", r["receipt"], "--timeout", "0.5", "--interval", "0.2"], capture_output=True, text=True)
    assert rc.returncode == 3 and ("running" in rc.stdout or "launching" in rc.stdout)
    gate.touch()
    rc = subprocess.run([sys.executable, script, "wait", r["receipt"], "--timeout", "60", "--interval", "0.2"], capture_output=True, text=True)
    assert rc.returncode == 0 and "finished exit=0" in rc.stdout
    rc = subprocess.run([sys.executable, script, "status", r["receipt"], "--json"], capture_output=True, text=True)
    assert json.loads(rc.stdout)["exit_code"] == 0
    rc = subprocess.run([sys.executable, script, "list", str(inv)], capture_output=True, text=True)
    assert "executor 00_round0 #1" in rc.stdout


def test_launch_rejects_bad_role_and_missing_request(tmp_path):
    with pytest.raises(ValueError):
        ls.launch("nope", launch_request(tmp_path), str(tmp_path / "inv"), "t")
    with pytest.raises(ValueError):
        ls.launch("critic", str(tmp_path / "missing.json"), str(tmp_path / "inv"), "t")


def test_build_command_uses_unresolved_target_root():
    cmd = ls.build_command("analyst", "/abs/req.json", "/target")
    assert cmd[:2] == ["uv", "run"]
    assert "python-dotenv" in cmd and any(a.startswith("claude-agent-sdk") for a in cmd)
    i = cmd.index("python")
    assert cmd[i + 1].endswith("subagents/transcript_analyst/main.py")
    assert cmd[i + 2 :] == ["/abs/req.json", "--cwd", "/target"]


def test_nonzero_exit_is_recorded(tmp_path):
    inv = tmp_path / "inv"
    r = ls.launch("mapper", launch_request(tmp_path), str(inv), "phase1", entry=stub_entry(tmp_path, exit_code=2, cost=False))
    final = ls.wait_for(r["receipt"], timeout=60, interval=0.2)
    assert final["exit_code"] == 2
    assert "exit=2" in ls.describe(final)


# ---------------------------------------------------------------------------
# `list --unfinished`: the mechanical half of RESUME.md
# ---------------------------------------------------------------------------
#
# Receipts are written directly (no processes), so each case is exact and
# fast: which invocations are listed, in what order, alive or relaunchable.


def _receipt(inv: Path, tag: str, n: int, *, request: dict, state: str, exit_code=None, started: str, pid=None) -> dict:
    """Write one launcher-shaped receipt plus its persisted request.

    Parameters
    ----------
    inv : Path
        Investigation directory.
    tag : str
        Iteration/phase tag.
    n : int
        Invocation index.
    request : dict
        The persisted request's content (compared for "same work").
    state : str
        ``finished`` / ``failed`` / ``running`` / ``launching``.
    exit_code : int | None
        Recorded exit code.
    started : str
        ISO timestamp; `unfinished` orders and supersedes by it.
    pid : int | None
        Recorded sub-agent pid.

    Returns
    -------
    dict
        The receipt as written.
    """
    stem = f"{tag}_analyst_{n}"
    art = inv / "artefacts" / "analyst"
    req_path = inv / "requests" / f"{stem}.json"
    write_json(req_path, request)
    r = {
        "role": "analyst", "tag": tag, "n": n, "state": state, "exit_code": exit_code,
        "started_at": started, "finished_at": None, "pid": pid, "supervisor_pid": None,
        "request": str(req_path), "report": str(art / f"report_{stem}.md"),
        "stderr": str(art / f"stderr_{stem}.log"), "cost": str(art / f"cost_{stem}.json"),
        "receipt": str(art / f"receipt_{stem}.json"), "cwd": None,
    }
    write_json(art / f"receipt_{stem}.json", r)
    return r


def test_unfinished_respects_superseding_order(tmp_path):
    """Only a *later* clean run of the same work supersedes a failure.

    A success that started before the failure is an earlier attempt, not its
    recovery; dropping the order check would hide a failure that still needs a
    relaunch.
    """
    inv = tmp_path / "inv"
    work = {"topic": "t"}
    _receipt(inv, "iter2", 1, request=work, state="finished", exit_code=0, started="2026-10-01T10:00:00+00:00")
    failed = _receipt(inv, "iter2", 2, request=work, state="finished", exit_code=2, started="2026-10-01T11:00:00+00:00")
    assert [e["receipt"]["n"] for e in ls.unfinished(str(inv))] == [failed["n"]]

    # A later clean run of the same work (differing only in infrastructure keys) recovers it.
    _receipt(inv, "iter2", 3, request={**work, "subagent_route": "openrouter"}, state="finished", exit_code=0, started="2026-10-01T12:00:00+00:00")
    assert ls.unfinished(str(inv)) == []


def test_unfinished_alive_invocation_is_not_relaunched(tmp_path, monkeypatch, capsys):
    """A still-running invocation is listed as alive with no relaunch command."""
    inv = tmp_path / "inv"
    running = _receipt(inv, "iter2", 1, request={"topic": "t"}, state="running", started="2026-10-01T10:00:00+00:00", pid=os.getpid())
    dead = _receipt(inv, "iter2", 2, request={"topic": "u"}, state="finished", exit_code=2, started="2026-10-01T11:00:00+00:00")
    # Liveness is the launcher's own check; force it so the test does not
    # depend on how pid-reuse is guarded.
    monkeypatch.setattr(ls, "_pid_alive", lambda pid, *a, **k: pid == running["pid"])

    entries = ls.unfinished(str(inv))
    assert [(e["receipt"]["n"], e["alive"], e["relaunch"] is None) for e in entries] == [(1, True, True), (2, False, False)]

    # --- The CLI prints the same, one block per invocation, in order ---
    assert ls.main(["list", str(inv), "--unfinished"]) == 0
    out = capsys.readouterr().out
    assert "-> still running: let it finish; do not relaunch over it" in out
    relaunch = "  -> relaunch (from the target root): " + shlex.join(ls.relaunch_command(dead))
    assert relaunch in out
    assert out.index("analyst iter2 #1") < out.index("still running") < out.index("analyst iter2 #2") < out.index(relaunch)


def test_unfinished_empty_prints_a_line(tmp_path, capsys):
    inv = tmp_path / "inv"
    _receipt(inv, "iter2", 1, request={"topic": "t"}, state="finished", exit_code=0, started="2026-10-01T10:00:00+00:00")
    assert ls.main(["list", str(inv), "--unfinished"]) == 0
    assert capsys.readouterr().out.strip() == "no unfinished invocations"


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
    data = json.loads(path.read_text())
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
    assert json.loads(persisted[0].read_text())["subagent_max_turns"] == 42
    assert f"`{persisted[0]}`" in seen["prompt"]


def test_spend_guard_is_active():
    """The conftest guard: provider keys are unset and an unstubbed `query` raises."""
    import claude_agent_sdk

    from subagents import runner

    assert not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"))
    for query in (claude_agent_sdk.query, runner.query):
        with pytest.raises(RuntimeError, match="spend guard"):
            query(prompt="p", options=None)


# ---------------------------------------------------------------------------
# spend.py
# ---------------------------------------------------------------------------


@pytest.fixture
def no_price_list(monkeypatch):
    """Keep spend tests off the network: OpenRouter's price list finds nothing."""
    monkeypatch.setattr(spend, "_fetch_json", lambda url: None)


def test_spend_sums_sidecars_and_eval_usage(tmp_path, no_price_list):
    inv = tmp_path / "investigations" / "demo"
    analyst = inv / "iterations" / "01" / "artefacts" / "analyst"
    write_json(inv / "artefacts" / "critic" / "cost_iter0_critic_1.json", {"prism_cost": 1, "agent": "Critic", "total_cost_usd": 1.5, "num_turns": 4})
    write_json(analyst / "cost_iter1_analyst_1.json", {"prism_cost": 1, "agent": "Analyst", "total_cost_usd": 2.25, "num_turns": 60})
    write_json(analyst / "cost_iter1_analyst_2.json", {"prism_cost": 1, "agent": "Analyst", "total_cost_usd": None, "num_turns": 1})
    # A JSON file that is not a sidecar (no `prism_cost` marker) is not summed.
    write_json(analyst / "cost_notes.json", {"agent": "Analyst", "total_cost_usd": 99.0})
    run = inv / "iterations" / "01" / "artefacts" / "executor" / "runs" / "r1"
    write_json(
        run / "status.json",
        {
            "schema": 1,
            "run_id": "r1",
            "state": "completed",
            "pairs": {
                "a": {"model_usage": {"openai/gpt-5": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}},
                "b": {"model_usage": {"openai/gpt-5": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}, "anthropic/x": {"total_tokens": 7}}},
            },
        },
    )
    # A `status.json` that is not a runner status file (no `schema`) is not an eval run.
    write_json(inv / "elsewhere" / "status.json", {"pairs": {"z": {"model_usage": {"openai/gpt-5": {"total_tokens": 10_000}}}}})
    s = spend.collect(str(inv), projects_dir=str(tmp_path / "no-projects"))
    assert s["subagents"]["total_cost_usd"] == 3.75
    assert s["subagents"]["invocations"] == 3
    assert s["subagents"]["by_agent"]["Analyst"] == {"cost_usd": 2.25, "invocations": 2, "turns": 61}
    assert s["evals"]["tokens_by_model"]["openai/gpt-5"] == {"input_tokens": 150, "output_tokens": 15, "total_tokens": 165}
    assert s["evals"]["pairs"] == 2
    assert [r["total_tokens"] for r in s["evals"]["run_summaries"]] == [172]  # 110 + 55 + 7
    text = spend.render(s)
    assert "sub-agent spend: $3.75 over 3 invocation(s)" in text
    assert "openai/gpt-5" in text


def _session_line(**fields) -> str:
    return json.dumps(fields) + "\n"


def _assistant(msg_id, model, usage, content=()):
    return _session_line(type="assistant", entrypoint="cli", message={"id": msg_id, "model": model, "usage": usage, "content": list(content)})


def test_spend_prices_the_lead_session_that_wrote_into_the_investigation(tmp_path, no_price_list):
    """Only the lead that started with the skill and wrote here is counted, each call once."""
    repo = tmp_path / "repo"
    inv = repo / "investigations" / "demo"
    inv.mkdir(parents=True)
    projects = tmp_path / "projects"
    folder = projects / spend._project_slug(str(repo))
    folder.mkdir(parents=True)
    write = {"type": "tool_use", "name": "Write", "input": {"file_path": str(inv / "INVESTIGATION-LOG.md")}}
    usage = {
        "input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 10_000_000,
        "cache_creation_input_tokens": 500_000, "cache_creation": {"ephemeral_1h_input_tokens": 500_000, "ephemeral_5m_input_tokens": 0},
    }
    (folder / "lead.jsonl").write_text(
        _session_line(type="user", message={"content": "<command-name>/diagnose</command-name>"})
        # The same message on two lines (one per content block) is one call
        + _assistant("m1", "claude-opus-5-5", usage, [write])
        + _assistant("m1", "claude-opus-5-5", usage)
        + _assistant("m2", "claude-mystery-9", {"input_tokens": 5, "output_tokens": 5})
    )
    # Started with the skill but only read this investigation: not its lead
    (folder / "reader.jsonl").write_text(
        _session_line(type="user", message={"content": "<command-name>/diagnose</command-name>"})
        + _assistant("r1", "claude-opus-5-5", usage, [{"type": "tool_use", "name": "Read", "input": {"file_path": str(inv / "FINDINGS-REPORT.md")}}])
    )
    # Wrote here but was never a lead session (e.g. a tidy-up session)
    (folder / "other.jsonl").write_text(_assistant("o1", "claude-opus-5-5", usage, [write]))

    s = spend.collect(str(inv), projects_dir=str(projects))
    lead = s["lead"]
    assert [x["session"] for x in lead["sessions"]] == ["lead"]
    # Opus 5.5: 1M in x $4 + 0.1M out x $20 + 10M reads x $0.20 + 0.5M 1-hour writes x $8
    assert lead["total_cost_usd"] == 4.0 + 2.0 + 2.0 + 4.0
    assert lead["sessions"][0]["calls"] == 2
    assert lead["sessions"][0]["unpriced_calls"] == 1
    assert lead["unpriced_models"] == ["claude-mystery-9"]
    assert s["total_cost_usd"] == 12.0
    text = spend.render(s)
    assert "total spend: $12.00 (sub-agents $0.00 + lead $12.00" in text
    assert "no price for: claude-mystery-9" in text


def test_lead_pricing_handles_unsplit_writes_and_fast_mode():
    """An unsplit cache write is priced as 5-minute; fast mode doubles the call."""
    usage = {"cache_creation_input_tokens": 1_000_000}
    assert spend._price_call("claude-opus-5-5", usage) == 4.0 * 1.25
    assert spend._price_call("claude-opus-5-5", {**usage, "speed": "fast"}) == 4.0 * 1.25 * 2
    assert spend._price_call("claude-unknown", usage) is None
