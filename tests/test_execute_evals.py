"""Offline tests for scripts/execute_evals.py — the eval batch runner.

Covers the deficiencies WP1 owns: the engine-level scorer leak (metrics never
reach stdout, the status file or the draft report), D02 (default ceiling),
D36 (zero-sample flag), D01/D24 (detached launch + status + draft report), and
the request→command rendering the Executor agent used to do by hand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import check_report as cr
import execute_evals as ee
from conftest import (
    LEAK_TOKENS,
    gate_prefix,
    make_request,
    substitute_fake_eval,
    wait_for_file,
    write_fixture_log,
)

SCRIPT = str(Path(ee.__file__))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def assert_no_leak(text: str, where: str) -> None:
    for tok in LEAK_TOKENS:
        assert tok not in text, f"scorer token {tok!r} leaked into {where}"


def assert_tree_has_no_leak(root: str | Path) -> list[Path]:
    """Walk a directory tree and assert no scorer token is in any file outside `private/`.

    `.eval` logs are skipped: they are the Analyst's input and legitimately
    carry scores (zip-compressed, so a byte scan would prove nothing anyway).
    Everything else the runner writes — status, plan, request copy, draft
    report, `worker.log` — is readable by the Executor or the lead.

    Parameters
    ----------
    root : str | Path
        The tree to walk (an experiment directory or a run directory).

    Returns
    -------
    list[Path]
        The files that were checked, so callers can assert coverage.
    """
    checked = []
    for path in Path(root).rglob("*"):
        if not path.is_file() or "private" in path.relative_to(root).parts or path.suffix == ".eval":
            continue
        assert_no_leak(path.read_bytes().decode("utf-8", "replace"), str(path))
        checked.append(path)
    return checked


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
        "credential_sample_errors": 0,  # "boom" is not a credit refusal
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
    assert f"--sample-id blackmail_none-none_replacement --epochs 30 --max-connections {ee.DEFAULT_MAX_CONNECTIONS}" in cmd
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
    assert (c["max_parallel"], c["max_parallel_source"]) == (ee.DEFAULT_MAX_PARALLEL, "runner default")
    assert (c["max_connections"], c["max_connections_source"]) == (ee.DEFAULT_MAX_CONNECTIONS, "runner default")

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


FOUR_CONDITIONS = {f"c{i}": {"task": "task.py", "args": {}} for i in range(4)}


def _spy_on_execute(monkeypatch):
    """Wrap ``ee.execute`` to record how many probes, and how many retests, ran at once.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Used to swap in the wrapper.

    Returns
    -------
    dict[str, int]
        Peak concurrency per kind, e.g. ``{"preflight": 3, "retest": 1}``, filled as the run goes.
    """
    real = ee.execute
    active = {"preflight": 0, "retest": 0}
    peak = {"preflight": 0, "retest": 0}
    lock = threading.Lock()

    def spy(commands, *args, **kwargs):
        kind = commands[0].id.split("_", 1)[0]
        if kind not in active:
            return real(commands, *args, **kwargs)
        with lock:
            active[kind] += 1
            peak[kind] = max(peak[kind], active[kind])
        time.sleep(0.3)  # long enough for side-by-side probes to overlap
        try:
            return real(commands, *args, **kwargs)
        finally:
            with lock:
                active[kind] -= 1

    monkeypatch.setattr(ee, "execute", spy)
    return peak


def test_preflight_probes_run_side_by_side_up_to_the_batch_width(tmp_path, fake_eval_script, monkeypatch):
    seen = _spy_on_execute(monkeypatch)
    run_dir, _, code = run_foreground(
        tmp_path, fake_eval_script, monkeypatch, conditions=FOUR_CONDITIONS,
        overrides={"max_parallel": 1, "max_connections": 3},
    )
    assert code == 0
    # Three samples in flight → three probes at once, never all four.
    assert seen == {"preflight": 3, "retest": 0}
    data = ee.Status.load(run_dir).data
    assert list(data["preflight"]["pairs"]) == data["pair_order"]
    assert {e["outcome"] for e in data["preflight"]["pairs"].values()} == {"passed"}


def test_errored_probes_are_retested_a_third_as_wide(tmp_path, fake_eval_script, monkeypatch):
    seen = _spy_on_execute(monkeypatch)
    run_dir, _, _ = run_foreground(
        tmp_path, fake_eval_script, monkeypatch, mode="sample_error", conditions=FOUR_CONDITIONS,
        overrides={"max_parallel": 1, "max_connections": 3},
    )
    pf = ee.Status.load(run_dir).data["preflight"]["pairs"]
    # Every probe errored, so every pair was retested and judged on the retest.
    assert all(e["retest"] is not None for e in pf.values())
    assert {e["outcome"] for e in pf.values()} == {"excluded-all-samples-deterministic"}
    # Retests are three samples each, so at three in flight they run one at a time.
    assert seen == {"preflight": 3, "retest": 1}


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
    assert f"- **Chosen `max_parallel`**: {ee.DEFAULT_MAX_PARALLEL}" in report
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

    rc = subprocess.run([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "60", "--interval", "0.2"], capture_output=True, text=True)
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
    # Hold the pair on a gate that never opens; kill once it is really running
    # (the `started` sentinel), rather than after a guessed sleep.
    started = tmp_path / "started"
    substitute_fake_eval(run_dir, fake_eval_script, prefix=gate_prefix(tmp_path / "never", started))
    pid = ee.launch_detached(run_dir)
    # First field is the pid (the runner may append a start-time guard after it).
    assert (Path(run_dir) / "worker.pid").read_text().split()[0] == str(pid)
    wait_for_file(started)
    os.killpg(pid, 9)
    data = ee.wait_for(run_dir, timeout=20, interval=0.2)
    assert data["state"] == "failed"
    assert any("died before writing a terminal state" in e for e in data["errors"])
    assert "WORKER DIED" in ee.render_status(ee.Status.load(run_dir).data)


def test_wait_returns_3_while_running_then_0(tmp_path, fake_eval_script, monkeypatch):
    """`wait` with a short timeout exits 3 on a live run, and 0 once it completes."""
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")
    path, _ = make_request(tmp_path, conditions={"baseline": {"task": "task.py", "args": {}}}, overrides={"skip_preflight": True})
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    gate, started = tmp_path / "gate", tmp_path / "started"
    substitute_fake_eval(run_dir, fake_eval_script, prefix=gate_prefix(gate, started))
    ee.launch_detached(run_dir)
    wait_for_file(started)  # the pair is running and held on the gate
    rc = subprocess.run([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "0.3", "--interval", "0.1"], capture_output=True, text=True)
    assert rc.returncode == 3
    assert "state: running" in rc.stdout
    gate.touch()
    rc = subprocess.run([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "60", "--interval", "0.2"], capture_output=True, text=True)
    assert rc.returncode == 0 and "state: completed" in rc.stdout


def test_run_subcommand_prints_receipt_and_runs_offline(tmp_path, fake_eval_script, monkeypatch):
    """The CLI `run` path (prepare + detached launch) prints a JSON receipt and completes.

    A fake `uv` on PATH forwards `uv run … inspect eval <args>` to the fake
    eval, so the real launch path runs end to end with no Inspect CLI, no model
    and no network — and the outcome can be asserted exactly rather than as
    "some terminal state".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "i = args.index('eval')\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, {fake_eval_script!r}] + args[i + 1:])\n"
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")

    path, _ = make_request(tmp_path, conditions={"baseline": {"task": "task.py", "args": {}}}, models=["mockllm/model"], overrides={"skip_preflight": True})
    rc = subprocess.run([sys.executable, SCRIPT, "run", path], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr
    receipt = json.loads(rc.stdout)
    assert receipt["status"] == "launched" and os.path.isfile(receipt["status_file"])
    assert receipt["pairs"] == ["baseline__mockllm-model"]
    rc = subprocess.run([sys.executable, SCRIPT, "wait", receipt["run_dir"], "--timeout", "60", "--interval", "0.2"], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    final = ee.Status.load(receipt["run_dir"]).data
    assert final["state"] == "completed"
    assert final["pairs"]["baseline__mockllm-model"]["state"] == "success"
    assert final["pairs"]["baseline__mockllm-model"]["error_class"] is None


# ---------------------------------------------------------------------------
# Blackout: nothing outside private/ carries a score (foreground and detached)
# ---------------------------------------------------------------------------


def test_foreground_run_tree_is_score_free(tmp_path, fake_eval_script, monkeypatch):
    """Every file the foreground worker leaves outside `private/` is score-free.

    The per-field assertions elsewhere check the fields they name; this walk
    catches a *new* file or field that starts carrying a score.
    """
    run_dir, req, code = run_foreground(tmp_path, fake_eval_script, monkeypatch, overrides={"epochs": 2})
    assert code == 0
    report = ee.render_report(ee.Status.load(run_dir).data)
    (Path(run_dir) / "draft_report.md").write_text(report)
    checked = assert_tree_has_no_leak(req["experiment_dir"])
    names = {p.name for p in checked}
    assert {"status.json", "plan.json", "draft_report.md"} <= names
    # The walk is meaningful: the raw streams under private/ do carry scores.
    raw = "".join(p.read_text() for p in (Path(run_dir) / "private").rglob("*.stdout"))
    assert "accuracy: 0.73" in raw


def test_detached_run_tree_is_score_free_including_worker_log(tmp_path, fake_eval_script, monkeypatch):
    """The detached path adds `worker.log` and the CLI's printed outputs; all score-free."""
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")
    path, req = make_request(tmp_path, overrides={"epochs": 2})
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    substitute_fake_eval(run_dir, fake_eval_script)
    ee.launch_detached(run_dir)
    for sub in ("wait", "status", "report"):
        args = [sys.executable, SCRIPT, sub, run_dir] + (["--timeout", "60", "--interval", "0.2"] if sub == "wait" else [])
        rc = subprocess.run(args, capture_output=True, text=True)
        assert rc.returncode == 0, rc.stdout + rc.stderr
        assert_no_leak(rc.stdout + rc.stderr, f"`{sub}` output")
    checked = assert_tree_has_no_leak(req["experiment_dir"])
    assert {"worker.log", "status.json", "draft_report.md"} <= {p.name for p in checked}


# ---------------------------------------------------------------------------
# Retries and the zero-sample flag
# ---------------------------------------------------------------------------


def test_transient_failure_is_retried_and_the_failed_attempt_stashed(tmp_path, fake_eval_script, monkeypatch):
    """One transient failure, then success: one retry, one `.eval` in the pool.

    The failed attempt's partial log must leave the condition's log directory
    (a second log there would double the Analyst's pool) and land under
    `private/…/failed_attempts/`.
    """
    run_dir, req, code = run_foreground(
        tmp_path, fake_eval_script, monkeypatch, mode="transient_then_ok",
        overrides={"skip_preflight": True}, conditions={"baseline": {"task": "task.py", "args": {}}},
    )
    assert code == 0
    e = ee.Status.load(run_dir).data["pairs"]["baseline__openai-gpt-5"]
    assert e["state"] == "success", e
    assert e["process_retries"] == 1
    pool = list((Path(req["experiment_dir"]) / "logs" / "baseline").rglob("*.eval"))
    assert [p.name for p in pool] == ["f.eval"]
    stashed = [p for p in (Path(run_dir) / "private").rglob("*.eval") if "failed_attempts" in p.parts]
    assert [p.name for p in stashed] == ["partial.eval"]
    assert "baseline__openai-gpt-5" in stashed[0].parts


def test_failure_without_a_log_is_not_zero_sample(tmp_path, fake_eval_script, monkeypatch):
    """Zero-sample means "a final log exists and completed nothing"; no log is a failure."""
    run_dir, _, _ = run_foreground(
        tmp_path, fake_eval_script, monkeypatch, mode="structural",
        overrides={"skip_preflight": True}, conditions={"baseline": {"task": "task.py", "args": {}}},
    )
    data = ee.Status.load(run_dir).data
    e = data["pairs"]["baseline__openai-gpt-5"]
    assert e["error_class"] == "structural" and not e["log_path"]
    assert e["zero_samples"] is False
    assert data["zero_sample_pairs"] == []


def test_rendered_report_passes_check_report(tmp_path, fake_eval_script, monkeypatch):
    """The runner's draft report is the Executor's report: it must pass its own conformance check."""
    for mode, conditions in (("ok", None), ("structural", {"baseline": {"task": "task.py", "args": {}}})):
        sub = tmp_path / mode
        sub.mkdir()
        run_dir, _, _ = run_foreground(sub, fake_eval_script, monkeypatch, mode=mode, conditions=conditions)
        defects, _ = cr.check_report("executor", ee.render_report(ee.Status.load(run_dir).data))
        assert defects == [], (mode, defects)


# ---------------------------------------------------------------------------
# Live sample counts while an eval runs
# ---------------------------------------------------------------------------


def test_live_count_reads_the_attempts_own_log(tmp_path):
    log_dir = tmp_path / "logs"
    assert ee.live_samples_completed(str(log_dir), since=0) is None  # no log yet
    write_fixture_log(str(log_dir / "run.eval"), epochs=3)
    assert ee.live_samples_completed(str(log_dir), since=0) == 3
    # A log older than the attempt belongs to an earlier run
    assert ee.live_samples_completed(str(log_dir), since=os.path.getmtime(log_dir / "run.eval") + 60) is None


def test_live_counter_updates_running_pairs_only(tmp_path):
    import threading
    import time

    running, done = tmp_path / "logs_a", tmp_path / "logs_b"
    write_fixture_log(str(running / "a.eval"), epochs=3)
    write_fixture_log(str(done / "b.eval"), epochs=2)
    # Dated after the counter starts, as an attempt's own log would be
    for p in (running / "a.eval", done / "b.eval"):
        os.utime(p, (time.time() + 5, time.time() + 5))
    status = ee.Status(str(tmp_path / "status.json"), {"pairs": {
        "a": {"state": "running", "attempts": 1, "log_dir": str(running), "samples_completed": None},
        "b": {"state": "success", "attempts": 1, "log_dir": str(done), "samples_completed": 7},
    }})

    stop = threading.Event()
    counter = threading.Thread(target=ee.watch_live_counts, args=(status, stop, 0.05), daemon=True)
    counter.start()
    try:
        deadline = time.time() + 10
        while status.pair("a")["samples_completed"] is None and time.time() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        counter.join()

    saved = json.loads((tmp_path / "status.json").read_text())
    assert saved["pairs"]["a"]["samples_completed"] == 3
    # A finished pair keeps its final count from the finished log
    assert saved["pairs"]["b"]["samples_completed"] == 7
    assert_no_leak(json.dumps(saved), "status.json")
