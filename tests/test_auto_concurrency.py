"""Automatic sizing of samples in flight, and the rolling launcher it runs on.

The runner measures preflight's sandboxes, works out what the machine can
hold, splits that evenly with other live runs on the machine, and re-asks
before every condition starts. The engine starts each condition as soon as a
slot frees rather than in fixed groups. These tests drive the pure sizing
rules directly and the engine with a stand-in for each eval subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import execute_evals as ee
from conftest import make_request, substitute_fake_eval

# --- Sizing rules -----------------------------------------------------------------


def test_docker_memory_readings_are_read_in_mb():
    assert ee.parse_mem_mb("12.3MiB / 15.6GiB") == 12.3
    assert ee.parse_mem_mb("1.5GiB / 15.6GiB") == 1536
    assert ee.parse_mem_mb("784KiB / 15.6GiB") == 784 / 1024
    assert ee.parse_mem_mb("--") is None


def test_capacity_follows_whichever_limit_binds():
    # The sandbagging eval on this machine: tiny sandboxes, so cores bind
    light = {"containers_seen": 2, "peak_container_mb": 2.0, "min_free_mb": 9300}
    assert ee.machine_capacity(light, 10, True)[0] == 20
    assert ee.machine_capacity(light, 10, True)[1].startswith("cores")
    # Heavy sandboxes: memory binds, 9300 MB × 0.5 / 1000 MB
    heavy = {"containers_seen": 2, "peak_container_mb": 1000.0, "min_free_mb": 9300}
    assert ee.machine_capacity(heavy, 10, True) [0] == 4
    # No sandboxes: only the hard cap; sandboxes unmeasured: the old default
    assert ee.machine_capacity(None, 10, False)[0] == ee.AUTO_MAX_IN_FLIGHT
    assert ee.machine_capacity(None, 10, True)[0] == ee.AUTO_MIN_ALONE
    assert ee.machine_capacity({"containers_seen": 0, "peak_container_mb": None, "min_free_mb": 9300}, 10, True)[0] == ee.AUTO_MIN_ALONE


def test_a_newcomer_takes_its_even_share_at_once():
    # A run holding all 30 does not lock a second run out: the newcomer takes
    # 15 straight away, and the older run drops to 15 at its next start
    assert ee.target_in_flight(30, 1) == 30
    assert ee.target_in_flight(30, 2) == 15
    # Floors: alone never below the old default; shared never below a few
    assert ee.target_in_flight(4, 1) == ee.AUTO_MIN_ALONE
    assert ee.target_in_flight(30, 20) == ee.AUTO_MIN_SHARED


def test_automatic_runs_split_only_what_hand_set_runs_leave():
    # Beside a hand-set run holding 15 of a capacity of 20, an automatic run
    # takes the other 5; two automatic runs would split them
    assert ee.target_in_flight(20, 1, hand_set_in_flight=15) == 5
    assert ee.target_in_flight(20, 2, hand_set_in_flight=10) == 5
    # A hand-set run holding everything still leaves a few, as when shared
    assert ee.target_in_flight(20, 1, hand_set_in_flight=30) == ee.AUTO_MIN_SHARED


def test_samples_in_flight_are_spread_over_conditions():
    assert ee.split_in_flight(30, 11) == (3, 10)
    assert ee.split_in_flight(10, 11) == (2, 5)
    assert ee.split_in_flight(15, 4) == (2, 7)
    assert ee.split_in_flight(30, 1) == (1, 30)


def test_only_the_connections_flag_changes():
    cmd = "uv run inspect eval task.py --epochs 30 --max-connections 5 -M x=1"
    assert ee.with_connections(cmd, 12) == "uv run inspect eval task.py --epochs 30 --max-connections 12 -M x=1"


# --- Measuring this run's sandboxes -----------------------------------------------


def test_only_this_experiments_sandboxes_are_measured(tmp_path):
    exp = tmp_path / "exp"
    (exp / "c_a").mkdir(parents=True)
    calls = []

    def docker(args):
        calls.append(args)
        if args[0] == "ps":
            return f"ours-1\tinspect-t-iours\t{exp / 'c_a'}\ntheirs-1\tinspect-t-itheirs\t/somewhere/else\n"
        return "ours-1\t41.2MiB / 15.6GiB\n"

    assert ee.experiment_sandbox_memory(str(exp), docker) == {"ours-1": 41.2}
    # Only our container was passed to `docker stats`
    assert calls[1][-1] == "ours-1" and "theirs-1" not in calls[1]
    assert ee.experiment_sandbox_memory(str(exp), lambda args: None) is None


# --- The machine's live runs ------------------------------------------------------


def _register_other(registry, pid):
    """Write a registry entry for another process, as another run would."""
    os.makedirs(registry, exist_ok=True)
    with open(os.path.join(registry, f"{pid}.json"), "w") as f:
        f.write('{"pid": %d, "pid_start": "%s", "run_dir": "elsewhere"}' % (pid, ee._pid_start_time(pid)))


def test_live_runs_are_counted_and_dead_ones_pruned(tmp_path):
    registry = os.environ["PRISM_RUN_REGISTRY"]
    entry = ee.register_live_run(str(tmp_path))
    assert ee.count_live_runs() == 1
    # Another live run (this test's parent process stands in for it)
    _register_other(registry, os.getppid())
    assert ee.count_live_runs() == 2
    # A run that died without cleaning up drops out, and its entry goes
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _register_other(registry, dead.pid)
    assert ee.count_live_runs() == 2
    assert not os.path.exists(os.path.join(registry, f"{dead.pid}.json"))
    ee.unregister_live_run(entry)
    assert ee.count_live_runs() == 1


def test_a_hand_set_run_is_counted_with_the_samples_it_holds(tmp_path):
    entry = ee.register_live_run(str(tmp_path), hand_set_in_flight=30)
    assert ee.machine_load() == (0, 1, 30)
    # An automatic run arrives (this test's parent process stands in for it)
    _register_other(os.environ["PRISM_RUN_REGISTRY"], os.getppid())
    assert ee.machine_load() == (1, 1, 30)
    assert ee.count_live_runs() == 2
    ee.unregister_live_run(entry)


# --- The rolling launcher ---------------------------------------------------------


def _engine(monkeypatch, durations, outputs=None):
    """Stand in for each eval subprocess and its log check.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Used to swap in the stand-ins.
    durations : dict[str, float]
        Seconds each command id "runs" for.
    outputs : dict[str, list[tuple[int, str]]] | None
        Per command id, (exit code, stdout) per attempt; success by default.

    Returns
    -------
    dict
        ``{"started": [(id, time, command)], "finished": {id: time}, "peak": int}``.
    """
    seen = {"started": [], "finished": {}, "peak": 0, "active": 0}
    attempts: dict[str, int] = {}
    last_code: dict[str, int] = {}
    lock = threading.Lock()

    def run_single(command, timeout, cwd, capture_dir, capture_name):
        cid = capture_name.split(".attempt")[0]
        with lock:
            seen["started"].append((cid, time.monotonic(), command))
            seen["active"] += 1
            seen["peak"] = max(seen["peak"], seen["active"])
            n = attempts[cid] = attempts.get(cid, -1) + 1
        time.sleep(durations.get(cid, 0.05))
        script = (outputs or {}).get(cid) or [(0, "")]
        code, out = script[min(n, len(script) - 1)]  # the last entry repeats
        with lock:
            seen["active"] -= 1
            seen["finished"][cid] = time.monotonic()
            last_code[cid] = code
        return code, 0.1, out, ""

    def verify(log_dir):
        ok = last_code.get(os.path.basename(log_dir)) == 0
        return {"log_path": f"{log_dir}/x.eval", "status": "success" if ok else "error"}

    monkeypatch.setattr(ee, "run_single", run_single)
    monkeypatch.setattr(ee, "verify_eval_log", verify)
    monkeypatch.setattr(ee, "EXECUTE_POLL", 0.02)
    return seen


def _commands(tmp_path, ids):
    return [ee.CommandSpec(id=i, command=f"inspect eval {i} --max-connections 5", log_dir=str(tmp_path / i)) for i in ids]


def _config(width):
    return ee.ExecutionConfig(max_parallel=width, max_retries=2, retry_backoff_seconds=[0, 0, 0], command_timeout=30)


def test_a_freed_slot_starts_the_next_condition_at_once(tmp_path, monkeypatch):
    # Two at once: "slow" holds one slot; "c" must start when "b" ends, not
    # wait for "slow" as the old fixed groups did
    seen = _engine(monkeypatch, {"slow": 0.6, "b": 0.1, "c": 0.1})
    report = ee.execute(_commands(tmp_path, ["slow", "b", "c"]), _config(2))
    assert [r.status for r in report.results] == ["success"] * 3
    started = {cid: t for cid, t, _ in seen["started"]}
    assert started["c"] < seen["finished"]["slow"]
    assert seen["peak"] == 2


def test_each_start_is_resized_and_an_older_run_shrinks_when_another_arrives(tmp_path, monkeypatch):
    # The worker's own sizing rule over the machine's live runs, with a
    # capacity of 30: alone, three conditions at ten; once a second run
    # registers mid-run, later starts get its even share of fifteen
    seen = _engine(monkeypatch, {"a": 0.15, "b": 0.15, "c": 0.15, "d": 0.05, "e": 0.05})
    registry = os.environ["PRISM_RUN_REGISTRY"]
    ee.register_live_run(str(tmp_path))
    sizes = []

    def size_launch(unfinished):
        if len(seen["started"]) == 3 and not sizes[-1:] == ["arrived"]:
            _register_other(registry, os.getppid())  # the second run arrives
            sizes.append("arrived")
        n = ee.target_in_flight(30, ee.count_live_runs())
        return ee.split_in_flight(n, unfinished)

    report = ee.execute(_commands(tmp_path, ["a", "b", "c", "d", "e"]), _config(2), size_launch=size_launch)
    assert [r.status for r in report.results] == ["success"] * 5
    connections = {cid: cmd.split("--max-connections ")[1] for cid, _, cmd in seen["started"]}
    assert [connections[c] for c in "abc"] == ["10", "10", "10"]
    assert connections["d"] == "7" and connections["e"] == "7"
    assert seen["peak"] == 3


def test_resource_contention_halves_how_many_run_at_once_once(tmp_path, monkeypatch):
    oom = [(1, "Cannot allocate memory"), (0, "")]
    seen = _engine(monkeypatch, {}, outputs={c: oom for c in "abcd"})
    report = ee.execute(_commands(tmp_path, list("abcd")), _config(4))
    assert [r.status for r in report.results] == ["success"] * 4
    assert [(r.previous_level, r.new_level) for r in report.concurrency_reductions] == [(4, 2)]


def test_the_same_structural_failure_across_the_run_aborts_the_rest(tmp_path, monkeypatch):
    broken = [(1, "ModuleNotFoundError: No module named agentic_misalignment")]
    _engine(monkeypatch, {"a": 0.05, "b": 0.05}, outputs={c: broken for c in "abcd"})
    report = ee.execute(_commands(tmp_path, list("abcd")), _config(2))
    statuses = [r.status for r in report.results]
    assert statuses[:2] == ["error", "error"]
    # "c" took the slot "a" freed before "b" showed the failure was shared,
    # so it fails fast too; everything not yet started is aborted
    assert statuses[2] in ("error", "aborted") and statuses[3] == "aborted"
    assert any("aborting remaining commands" in e for e in report.errors)


def test_retries_wait_out_their_backoff_without_holding_up_others(tmp_path, monkeypatch):
    flaky = [(1, "ConnectionError: Connection reset by peer"), (0, "")]
    seen = _engine(monkeypatch, {"b": 0.05}, outputs={"a": flaky})
    config = ee.ExecutionConfig(max_parallel=1, max_retries=2, retry_backoff_seconds=[0.4], command_timeout=30)
    report = ee.execute(_commands(tmp_path, ["a", "b"]), config)
    assert [r.status for r in report.results] == ["success", "success"]
    order = [cid for cid, _, _ in seen["started"]]
    # "b" ran while "a" waited out its backoff, then "a" was retried
    assert order == ["a", "b", "a"]
    assert report.results[0].process_retries == 1


# --- The worker and the report ----------------------------------------------------


def _run(tmp_path, fake_eval_script, monkeypatch, overrides=None):
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")
    path, _ = make_request(tmp_path, overrides=overrides)
    run_dir, problems = ee.prepare_run(path)
    assert not problems, problems
    substitute_fake_eval(run_dir, fake_eval_script)
    assert ee.run_worker(run_dir) == 0
    return ee.Status.load(run_dir).data


def test_a_run_with_no_override_is_sized_automatically_and_says_so(tmp_path, fake_eval_script, monkeypatch):
    data = _run(tmp_path, fake_eval_script, monkeypatch, overrides={"epochs": 2})
    auto = data["concurrency"]["auto"]
    # No sandbox files beside the fixture task: only the hard cap applies
    assert auto["capacity"] == ee.AUTO_MAX_IN_FLIGHT
    assert [l["live_runs"] for l in auto["launches"]] == [1, 1]
    report = ee.render_report(data)
    assert "- **Automatic sizing**: capacity 40" in report
    assert "automatic" in ee.render_status(data)
    # The run left the machine's registry when it ended
    assert ee.count_live_runs() == 1 and not os.listdir(os.environ["PRISM_RUN_REGISTRY"])


def test_a_requested_number_turns_automatic_sizing_off(tmp_path, fake_eval_script, monkeypatch):
    data = _run(tmp_path, fake_eval_script, monkeypatch, overrides={"epochs": 2, "max_parallel": 1, "max_connections": 3})
    assert "auto" not in data["concurrency"]
    assert "Automatic sizing" not in ee.render_report(data)
    # It still registered while it ran, so other runs could leave its share
    # alone, and left the registry when it ended
    assert not os.listdir(os.environ["PRISM_RUN_REGISTRY"])
