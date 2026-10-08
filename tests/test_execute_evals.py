"""Offline tests for scripts/execute_evals.py — the eval batch runner.

Covers the deficiencies WP1 owns: the engine-level scorer leak (metrics never
reach stdout, the status file or the draft report), D02 (default ceiling),
D36 (zero-sample flag), D01/D24 (detached launch + status + draft report), and
the request→command rendering the Executor agent used to do by hand.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import execute_evals as ee
from conftest import LEAK_TOKENS, write_fixture_log

SCRIPT = str(Path(ee.__file__))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_request(tmp_path, *, conditions=None, models=None, overrides=None, routing=None):
    exp = tmp_path / "exp"
    exp.mkdir(parents=True, exist_ok=True)
    (exp / "task.py").write_text("# fixture task\n")
    req = {
        "experiment_name": "mf_fixture",
        "experiment_dir": str(exp),
        "conditions": conditions or {"baseline": {"task": "task.py", "args": {}}, "treatment": {"task": "task.py", "args": {"system": "alt.txt"}}},
        "models": models or ["openai/gpt-5"],
    }
    if overrides is not None:
        req["overrides"] = overrides
    if routing is not None:
        req["model_routing"] = routing
    path = tmp_path / "request.json"
    path.write_text(json.dumps(req))
    return str(path), req


def substitute_fake_eval(run_dir: str, fake_script: str) -> None:
    """Point every rendered command at the fake eval, keeping the flags."""
    plan_path = os.path.join(run_dir, "plan.json")
    plan = json.load(open(plan_path))
    for p in plan["pairs"]:
        for key in ("command", "preflight_command", "retest_command"):
            parts = shlex.split(p[key])
            i = parts.index("eval")  # "... inspect eval task.py ..."
            p[key] = " ".join(shlex.quote(x) for x in [sys.executable, fake_script] + parts[i + 1 :])
    json.dump(plan, open(plan_path, "w"))


def assert_no_leak(text: str, where: str) -> None:
    for tok in LEAK_TOKENS:
        assert tok not in text, f"scorer token {tok!r} leaked into {where}"


# ---------------------------------------------------------------------------
# Engine-level scorer leak
# ---------------------------------------------------------------------------


def test_verify_eval_log_has_no_metrics(fixture_log):
    info = ee.verify_eval_log(os.path.dirname(fixture_log))
    assert info["status"] == "success"
    assert info["samples_completed"] == 2
    assert "metrics" not in info
    assert_no_leak(json.dumps(info), "verify_eval_log output")
    # Token usage (execution shape, not a scorer output) is retained.
    assert info["model_usage"]["mockllm/model"]["total_tokens"] == 60


def test_sample_shape_reads_shape_only(tmp_path):
    p = write_fixture_log(str(tmp_path / "a.eval"), epochs=3, with_error=True, no_assistant=1)
    shape = ee.sample_shape(p)
    assert shape == {
        "samples": 3,
        "sample_errors": 1,
        "sample_retries": 0,
        "termination": {"no_assistant_msg": 1, "limit_hit": 0, "error_terminated": 1},
    }


def test_scrub_scores_drops_progress_and_panel_lines():
    raw = (
        "Running task\n"
        "Steps:  10/10 100% Samples:   2/2 accuracy: 0.73\n"
        "╭─ task ─╮\n│ accuracy  0.7317 │\n╰──╯\n"
        "ModuleNotFoundError: No module named x\n"
        "mean_score: 0.5 stderr: 0.1\n"
    )
    out = ee.scrub_scores(raw)
    assert "ModuleNotFoundError" in out
    assert "0.73" not in out and "0.7317" not in out
    assert "mean_score: <redacted>" in out and "stderr: <redacted>" in out
    # Grouped/bracketed metric names are not word characters.
    out2 = ee.scrub_scores("gpt-4o     0.750  level_1   0.250\naccuracy[max]  0.500\nkeep: this line\n")
    assert "0.750" not in out2 and "0.500" not in out2 and "keep: this line" in out2


def test_scrub_scores_on_real_inspect_plain_output():
    """Captured stdout of a real `inspect eval --display plain` run (mock model)."""
    raw = (Path(__file__).parent / "fixture_inspect_plain_stdout.txt").read_text()
    assert "accuracy  0.000" in raw  # the fixture really carries metrics
    out = ee.scrub_scores(raw)
    assert "accuracy" not in out and "stderr" not in out and "0.000" not in out
    # Non-score lines survive (the tail must still carry diagnostic context).
    assert "total time:" in out
    assert "Log:" in out


def test_removed_legacy_spec_form_is_rejected(tmp_path):
    """The old single-positional `commands` spec is gone; the CLI must refuse it, not run it."""
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"commands": [], "execution": {"max_parallel": 1}}))
    rc = subprocess.run([sys.executable, SCRIPT, str(spec_path)], capture_output=True, text=True)
    assert rc.returncode != 0
    assert "invalid choice" in rc.stderr


# ---------------------------------------------------------------------------
# Request → commands
# ---------------------------------------------------------------------------


def test_build_pairs_renders_policy_flags_routing_and_defaults(tmp_path):
    routing = {
        "openrouter/deepseek/deepseek-r1": {
            "route": "openrouter",
            "provider_slug": "novita",
            "model_args": {"provider": {"order": ["novita"], "allow_fallbacks": False}},
        }
    }
    _, req = make_request(
        tmp_path,
        models=["openrouter/deepseek/deepseek-r1"],
        overrides={"sample_id": "blackmail_none-none_replacement", "epochs": 30},
        routing=routing,
    )
    pairs = ee.build_pairs(req, ee.DEFAULT_MAX_CONNECTIONS)
    assert [p.id for p in pairs] == ["baseline__openrouter-deepseek-deepseek-r1", "treatment__openrouter-deepseek-deepseek-r1"]
    cmd = pairs[1].command
    assert cmd.startswith("uv run --with 'openai>=2.45.0' inspect eval task.py --model openrouter/deepseek/deepseek-r1 --display plain")
    assert f"--log-dir {req['experiment_dir']}/logs/treatment" in cmd
    assert "--tags exp:mf_fixture,cond:treatment" in cmd
    assert "--metadata condition=treatment --metadata model=openrouter/deepseek/deepseek-r1 --metadata route=openrouter:novita" in cmd
    assert "--no-fail-on-error --retry-on-error 3" in cmd
    assert "-T system=alt.txt" in cmd
    assert "--sample-id blackmail_none-none_replacement --epochs 30 --max-connections 4" in cmd
    # JSON (not Python) rendering of the -M object, lowercase false.
    assert "-M 'provider={\"order\": [\"novita\"], \"allow_fallbacks\": false}'" in cmd
    # Pinned sample id: the probe is the pinned invocation without --epochs, no --limit.
    assert "--limit" not in pairs[1].preflight_command
    assert "--epochs" not in pairs[1].preflight_command
    assert "--sample-id" in pairs[1].preflight_command
    assert "/logs/_preflight/" in pairs[1].preflight_command


def test_build_pairs_limit_probe_and_multi_model_log_dirs(tmp_path):
    _, req = make_request(tmp_path, models=["openai/gpt-5", "anthropic/claude-sonnet-4-5"], overrides={"sample_limit": 20})
    pairs = ee.build_pairs(req, 4)
    assert len(pairs) == 4
    assert pairs[0].log_dir.endswith("/logs/baseline/openai-gpt-5")
    assert pairs[1].log_dir.endswith("/logs/baseline/anthropic-claude-sonnet-4-5")
    assert "--limit 1 " in pairs[0].preflight_command
    assert "--limit 3 " in pairs[0].retest_command
    assert "--limit 20 " in pairs[0].command
    assert "--metadata route=direct" in pairs[0].command


def test_validate_request_catches_contract_violations(tmp_path):
    _, req = make_request(tmp_path)
    req["conditions"]["Bad-Name"] = {"task": "missing.py"}
    req["overrides"] = {"sample_id": "x", "sample_limit": 3}
    problems = ee.validate_request(req)
    joined = "\n".join(problems)
    assert "not snake_case" in joined
    assert "task file not found" in joined
    assert "sample_id and sample_limit" in joined
    assert ee.validate_request({"experiment_name": "x"}) == [
        "missing required field: experiment_dir",
        "missing required field: conditions",
        "missing required field: models",
    ]


# ---------------------------------------------------------------------------
# D02 — default ceiling, overrides, CLI raise needs a rationale
# ---------------------------------------------------------------------------


def test_default_ceiling_and_override_sources(tmp_path):
    path, _ = make_request(tmp_path)
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    c = ee.Status.load(run_dir).data["concurrency"]
    assert (c["max_parallel"], c["max_parallel_source"]) == (1, "runner default")
    assert (c["max_connections"], c["max_connections_source"]) == (4, "runner default")

    path2, _ = make_request(tmp_path / "b", overrides={"max_parallel": 2, "max_connections": 8})
    run_dir2, _ = ee.prepare_run(path2)
    c2 = ee.Status.load(run_dir2).data["concurrency"]
    assert (c2["max_parallel"], c2["max_parallel_source"]) == (2, "request overrides")
    assert "--max-connections 8" in ee.Status.load(run_dir2).data["pairs"]["baseline__openai-gpt-5"]["command"]

    _, problems = ee.prepare_run(path2, max_parallel=3)
    assert problems and "rationale" in problems[0]
    run_dir3, problems = ee.prepare_run(path2, max_parallel=3, rationale="API-only eval, 2 pairs")
    assert not problems
    c3 = ee.Status.load(run_dir3).data["concurrency"]
    assert (c3["max_parallel"], c3["max_parallel_source"], c3["rationale"]) == (3, "cli override", "API-only eval, 2 pairs")
    report = ee.render_report(ee.Status.load(run_dir3).data)
    assert "Executor raised the runner ceiling via CLI override — API-only eval, 2 pairs" in report


def test_unrecognised_override_is_noted(tmp_path):
    path, _ = make_request(tmp_path, overrides={"bogus": 1})
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    notes = "\n".join(ee.Status.load(run_dir).data["notes"])
    assert "Unrecognised override keys ignored: bogus" in notes


def test_removed_runs_per_condition_is_refused(tmp_path):
    """Refused, not ignored: a caller still sending it must learn it never repeated anything."""
    path, _ = make_request(tmp_path, overrides={"runs_per_condition": 3})
    _, problems = ee.prepare_run(path)
    assert any("runs_per_condition was removed" in p and "epochs" in p for p in problems)


def test_stale_logs_are_refused_unless_quarantined(tmp_path):
    path, req = make_request(tmp_path, conditions={"baseline": {"task": "task.py", "args": {}}})
    stale_dir = Path(req["experiment_dir"]) / "logs" / "baseline"
    write_fixture_log(str(stale_dir / "old.eval"))
    _, problems = ee.prepare_run(path)
    assert problems and "already holds 1 .eval" in problems[0]
    run_dir, problems = ee.prepare_run(path, quarantine_existing_logs=True)
    assert not problems
    assert not list(stale_dir.glob("*.eval"))
    assert list((Path(req["experiment_dir"]) / "logs_quarantined").rglob("*.eval"))


# ---------------------------------------------------------------------------
# Worker: foreground end-to-end with preflight, status, draft report
# ---------------------------------------------------------------------------


def run_foreground(tmp_path, fake_eval_script, monkeypatch, *, mode="ok", overrides=None, conditions=None):
    monkeypatch.setenv("FAKE_EVAL_MODE", mode)
    path, req = make_request(tmp_path, overrides=overrides, conditions=conditions)
    run_dir, problems = ee.prepare_run(path)
    assert not problems, problems
    substitute_fake_eval(run_dir, fake_eval_script)
    code = ee.run_worker(run_dir)
    return run_dir, req, code


def test_foreground_run_with_preflight_is_score_free(tmp_path, fake_eval_script, monkeypatch):
    run_dir, req, code = run_foreground(tmp_path, fake_eval_script, monkeypatch, overrides={"epochs": 2})
    assert code == 0
    data = ee.Status.load(run_dir).data
    assert data["state"] == "completed"
    assert data["preflight"]["state"] == "done"
    assert {e["outcome"] for e in data["preflight"]["pairs"].values()} == {"passed"}
    for pid in data["pair_order"]:
        e = data["pairs"][pid]
        assert e["state"] == "success", e
        assert e["samples_completed"] == 2
        assert e["termination"] == {"no_assistant_msg": 0, "limit_hit": 0, "error_terminated": 0}
        assert e["model_usage"]["mockllm/model"]["total_tokens"] == 60
    # Preflight logs were quarantined out of the Analyst's tree.
    assert not (Path(req["experiment_dir"]) / "logs" / "_preflight").exists()
    assert list((Path(run_dir) / "private" / "preflight_logs").rglob("*.eval"))
    # One .eval per condition in the log tree.
    assert len(list((Path(req["experiment_dir"]) / "logs" / "baseline").glob("*.eval"))) == 1

    status_text = json.dumps(data)
    assert_no_leak(status_text, "status.json")
    assert_no_leak(ee.render_status(data), "status rendering")
    report = ee.render_report(data)
    assert_no_leak(report, "draft report")
    # The raw (score-bearing) streams exist only under private/.
    raw = "".join(p.read_text() for p in (Path(run_dir) / "private").rglob("*.stdout"))
    assert "accuracy: 0.73" in raw


def test_draft_report_has_contract_headings_in_order(tmp_path, fake_eval_script, monkeypatch):
    run_dir, _, _ = run_foreground(tmp_path, fake_eval_script, monkeypatch, overrides={"skip_preflight": True})
    report = ee.render_report(ee.Status.load(run_dir).data)
    headings = [
        "### Summary",
        "### Parent Log Directory",
        "### Preflight Exclusions",
        "### Condition-Model Execution Matrix",
        "### Error Summary",
        "#### Structural (0 condition-model pair(s))",
        "#### Sample-level (0 condition-model pair(s))",
        "#### Transient, recovered (0 condition-model pair(s))",
        "#### Pinned provider unavailable (0 condition-model pair(s))",
        "### Transcript Termination Metadata",
        "### Concurrency Decision",
        "### Execution Summary",
        "### Additional Notes",
    ]
    positions = [report.index(h) for h in headings]
    assert positions == sorted(positions)
    assert report.startswith("### Summary")
    assert "Preflight skipped (skip_preflight=true)." in report
    assert "**Zero-sample pairs:** none." in report
    assert "- **Chosen `max_parallel`**: 1" in report
    assert "| baseline | openai/gpt-5 | direct |" in report
    assert "| treatment | openai/gpt-5 | 0 | 0 | 0 |" in report


def test_zero_sample_pair_is_flagged_distinctly(tmp_path, fake_eval_script, monkeypatch):
    run_dir, _, _ = run_foreground(tmp_path, fake_eval_script, monkeypatch, mode="zero", overrides={"skip_preflight": True, "epochs": 1})
    data = ee.Status.load(run_dir).data
    assert data["zero_sample_pairs"] == ["baseline__openai-gpt-5", "treatment__openai-gpt-5"]
    assert all(e["zero_samples"] for e in data["pairs"].values())
    assert "ZERO-SAMPLE PAIRS (completed 0 samples" in ee.render_status(data)
    report = ee.render_report(data)
    assert "**Zero-sample pairs (completed 0 samples;" in report
    assert "`baseline / openai/gpt-5`, `treatment / openai/gpt-5`" in report
    assert "- **Zero samples completed**: 2" in report


def test_structural_failure_is_excluded_at_preflight_with_evidence(tmp_path, fake_eval_script, monkeypatch):
    run_dir, _, _ = run_foreground(tmp_path, fake_eval_script, monkeypatch, mode="structural", conditions={"baseline": {"task": "task.py", "args": {}}})
    data = ee.Status.load(run_dir).data
    e = data["pairs"]["baseline__openai-gpt-5"]
    assert e["state"] == "excluded-at-preflight"
    pf = data["preflight"]["pairs"]["baseline__openai-gpt-5"]
    assert pf["outcome"] == "excluded-structural"
    assert pf["evidence"]["exit_code"] == "1"
    assert "ModuleNotFoundError" in pf["evidence"]["output_tail"]
    report = ee.render_report(data)
    assert "- **Reason**: `structural`" in report
    assert "ModuleNotFoundError" in report
    assert "| baseline | openai/gpt-5 | direct | — | excluded-at-preflight |" in report
    assert "- **Excluded at preflight**: 1" in report
    assert_no_leak(report, "draft report (structural)")
    # The scrubbed tail dropped the score-bearing progress line.
    assert "accuracy" not in pf["evidence"]["output_tail"]


def test_sample_errors_map_to_sample_level(tmp_path, fake_eval_script, monkeypatch):
    run_dir, _, _ = run_foreground(tmp_path, fake_eval_script, monkeypatch, mode="sample_error", overrides={"skip_preflight": True, "epochs": 3}, conditions={"baseline": {"task": "task.py", "args": {}}})
    data = ee.Status.load(run_dir).data
    e = data["pairs"]["baseline__openai-gpt-5"]
    assert e["state"] == "sample-level-errors"
    assert e["sample_errors"] == 1
    report = ee.render_report(data)
    assert "#### Sample-level (1 condition-model pair(s))" in report
    assert "- **`baseline / openai/gpt-5`**: 1 of 3 samples errored." in report
    assert "| baseline | openai/gpt-5 | 0 | 0 | 1 |" in report


# ---------------------------------------------------------------------------
# Detached launch: receipt, status, wait, survives the caller
# ---------------------------------------------------------------------------


def test_detached_run_round_trip(tmp_path, fake_eval_script, monkeypatch):
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")
    path, _ = make_request(tmp_path, overrides={"skip_preflight": True, "epochs": 2})
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    substitute_fake_eval(run_dir, fake_eval_script)
    pid = ee.launch_detached(run_dir)
    assert pid and os.getsid(pid) != os.getsid(os.getpid()), "worker must run in its own session"

    rc = subprocess.run([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "60", "--interval", "1"], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    assert "state: completed" in rc.stdout
    assert_no_leak(rc.stdout, "wait stdout")
    rc = subprocess.run([sys.executable, SCRIPT, "status", run_dir], capture_output=True, text=True)
    assert rc.returncode == 0 and "zero-sample pairs: none" in rc.stdout
    rc = subprocess.run([sys.executable, SCRIPT, "report", run_dir], capture_output=True, text=True)
    assert rc.returncode == 0 and rc.stdout.startswith("### Summary")
    assert (Path(run_dir) / "draft_report.md").read_text() == rc.stdout.rstrip("\n") + "\n"
    assert_no_leak(rc.stdout, "report stdout")


def test_wait_treats_dead_worker_as_terminal(tmp_path, fake_eval_script, monkeypatch):
    """A worker killed before writing a terminal state must not strand `wait`."""
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")
    path, _ = make_request(tmp_path, conditions={"baseline": {"task": "task.py", "args": {}}}, overrides={"skip_preflight": True})
    run_dir, _ = ee.prepare_run(path)
    substitute_fake_eval(run_dir, fake_eval_script)
    plan = json.load(open(os.path.join(run_dir, "plan.json")))
    plan["pairs"][0]["command"] = "sleep 30 && " + plan["pairs"][0]["command"]
    json.dump(plan, open(os.path.join(run_dir, "plan.json"), "w"))
    pid = ee.launch_detached(run_dir)
    assert (Path(run_dir) / "worker.pid").read_text() == str(pid)
    time.sleep(1.0)
    os.killpg(pid, 9)
    data = ee.wait_for(run_dir, timeout=20, interval=0.5)
    assert data["state"] == "failed"
    assert any("died before writing a terminal state" in e for e in data["errors"])
    assert "WORKER DIED" in ee.render_status(ee.Status.load(run_dir).data)


def test_run_subcommand_prints_receipt_and_wait_returns_3_while_running(tmp_path, fake_eval_script, monkeypatch):
    """`run` via the CLI returns a receipt immediately; `wait` with a short timeout exits 3."""
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")
    path, req = make_request(tmp_path, conditions={"baseline": {"task": "task.py", "args": {}}}, overrides={"skip_preflight": True})
    # Make the (fake) eval slow enough that wait times out: prepend a sleep.
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    substitute_fake_eval(run_dir, fake_eval_script)
    plan = json.load(open(os.path.join(run_dir, "plan.json")))
    plan["pairs"][0]["command"] = "sleep 3 && " + plan["pairs"][0]["command"]
    json.dump(plan, open(os.path.join(run_dir, "plan.json"), "w"))
    ee.launch_detached(run_dir)
    rc = subprocess.run([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "1", "--interval", "0.5"], capture_output=True, text=True)
    assert rc.returncode == 3
    assert "state: running" in rc.stdout or "state: launching" in rc.stdout
    rc = subprocess.run([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "60", "--interval", "0.5"], capture_output=True, text=True)
    assert rc.returncode == 0 and "state: completed" in rc.stdout

    # And the CLI `run` path itself (detached) prints a JSON receipt.
    # A mock model so the suite can never reach a paid provider.
    path2, _ = make_request(tmp_path / "second", conditions={"baseline": {"task": "task.py", "args": {}}}, models=["mockllm/model"], overrides={"skip_preflight": True})
    rc = subprocess.run([sys.executable, SCRIPT, "run", path2], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr
    receipt = json.loads(rc.stdout)
    assert receipt["status"] == "launched" and os.path.isfile(receipt["status_file"])
    assert receipt["pairs"] == ["baseline__mockllm-model"]
    # That worker will fail (no real inspect here) — wait for it so nothing is orphaned.
    subprocess.run([sys.executable, SCRIPT, "wait", receipt["run_dir"], "--timeout", "120", "--interval", "1"], capture_output=True, text=True)
    final = ee.Status.load(receipt["run_dir"]).data
    assert final["state"] in ee.TERMINAL_STATES
