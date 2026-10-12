"""Regression tests for the pre-push audit fixes (A3, A4).

Each test names the defect it pins. Most of these failed silently before the
fix — a run that "succeeded" with the wrong sandbox, a transient failure
classified structural, a score surviving the scrubber, a caller polling a dead
sub-agent forever — so the test is the only thing that notices a regression.

Run with the self-contained recipe in ``conftest.py``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import make_request, substitute_fake_eval

from subagents import runner
from subagents.experiment_executor import agent as executor
from subagents.transcript_analyst import agent as analyst
from subagents.transcript_analyst.system_prompt import EXECUTION_NOTES_RULE

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import execute_evals as ee  # noqa: E402
import launch_subagent as ls  # noqa: E402
import spend  # noqa: E402

# --- A3.1: each condition runs from its task file's directory -------------------------


def test_task_location_splits_the_task_path():
    """`cond_a/task.py` runs from `<exp>/cond_a` as `task.py`; a task directory runs as `.`."""
    assert ee.task_location("/exp", "cond_a/task.py") == ("/exp/cond_a", "task.py")
    assert ee.task_location("/exp", "task.py") == ("/exp", "task.py")


def test_each_condition_runs_from_its_own_task_directory(tmp_path, fake_eval_script, monkeypatch):
    """Sibling conditions must not share the parent's cwd (compose file, Sample.files: gotcha 11)."""
    monkeypatch.setenv("FAKE_EVAL_MODE", "ok")
    conditions = {"c_a": {"task": "c_a/task.py", "args": {}}, "c_b": {"task": "c_b/task.py", "args": {}}}
    path, req = make_request(tmp_path, conditions=conditions, overrides={"skip_preflight": True})
    for c in conditions:
        (Path(req["experiment_dir"]) / c).mkdir()
        (Path(req["experiment_dir"]) / c / "task.py").write_text("# task\n")
    run_dir, problems = ee.prepare_run(path)
    assert not problems

    plan = json.loads((Path(run_dir) / "plan.json").read_text())
    for p in plan["pairs"]:
        assert p["cwd"] == os.path.join(req["experiment_dir"], p["condition"])
        assert " inspect eval task.py " in p["command"]  # relative to that cwd
        assert os.path.isabs(p["command"].split("--log-dir ")[1].split()[0])  # logs stay absolute

    # The worker really runs each command there: the gate prefix records the cwd.
    cwds = tmp_path / "cwds.txt"
    substitute_fake_eval(run_dir, fake_eval_script, prefix=f"pwd >> {cwds} && ")
    assert ee.run_worker(run_dir) == 0
    assert set(cwds.read_text().split()) == {os.path.join(req["experiment_dir"], c) for c in conditions}


# --- A3.3: Inspect's run header is not a structural failure ----------------------------


def test_mid_run_transient_with_the_dataset_header_is_retried():
    """`dataset: <name>` heads every run's stdout; it must not make a connection reset structural."""
    stdout = (ROOT / "tests" / "fixture_inspect_plain_stdout.txt").read_text().split("total time:")[0]
    assert "dataset" in stdout.lower()
    tail = ee.combine_output(stdout, "httpx.ConnectError: [Errno 104] Connection reset by peer")
    assert ee.classify_error(1, tail) == "transient"
    assert ee.should_retry("transient", 0, 3)


def test_named_dataset_failures_stay_structural():
    assert ee.classify_error(1, "datasets.exceptions.DatasetNotFoundError: Dataset 'x/y' doesn't exist") == "structural"


# --- A3.4: grouped metric names with spaces, and log error messages ----------------------


def test_scrubber_removes_grouped_metrics_from_the_real_grid():
    """Inspect's grouped metrics print `goal conflict  0.500`; every value must go."""
    from inspect_ai._display.core.results import task_scores
    from inspect_ai.log import EvalMetric, EvalScore
    from rich.console import Console

    def score(name, metrics):
        return EvalScore(name=name, scorer=name, metrics={m: EvalMetric(name=m, value=v) for m, v in metrics.items()})

    scores = [score("classifier", {"accuracy": 0.733, "stderr": 0.081}),
              score("grouped", {"goal conflict": 0.5, "no goal": 0.25, "all": 0.375})]
    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=False, color_system=None)
    for row in task_scores(scores):
        console.print(row)
    scrubbed = ee.scrub_scores(buf.getvalue())
    for value in ("0.733", "0.081", "0.500", "0.250", "0.375"):
        assert value not in scrubbed, scrubbed


def test_log_error_message_is_scrubbed_before_it_is_stored(tmp_path, monkeypatch):
    """A failed log's error text can quote scorer output; it enters `errors` scrubbed."""
    monkeypatch.setattr(ee, "verify_eval_log", lambda log_dir: {
        "log_path": None, "status": "error", "error": "scorer failed after accuracy: 0.731",
    })
    cmd = ee.CommandSpec(id="c", command="exit 1", log_dir=str(tmp_path))
    config = ee.ExecutionConfig(max_parallel=1, max_retries=0, retry_backoff_seconds=[0], command_timeout=30)
    report = ee.execute([cmd], config)
    assert report.results[0].errors[0] == "scorer failed after accuracy: <redacted>"


# --- A3.5: a dead sub-agent is terminal, and pid reuse is not life -----------------------


def _receipt(tmp_path, **fields) -> str:
    receipt = {"role": "analyst", "tag": "t", "n": 1, "state": "running", "pid": None, "supervisor_pid": None,
               "started_at": "2020-01-01T00:00:00+00:00", "report": str(tmp_path / "r.md"),
               "stderr": str(tmp_path / "e.log"), "cost": str(tmp_path / "c.json"), "request": str(tmp_path / "q.json")}
    receipt.update(fields)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    return str(path)


def test_wait_on_a_dead_process_exits_4(tmp_path):
    """Exit 3 ("still running") on a dead process made callers loop forever."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path = _receipt(tmp_path, pid=dead.pid)
    rc = ls.main(["wait", path, "--timeout", "0", "--interval", "0"])
    assert rc == ls.EXIT_DEAD == 4
    assert ls.main(["status", path]) == 4


def test_a_recycled_pid_is_not_the_recorded_process(tmp_path):
    """A live pid whose start time differs from the receipt's is someone else's process."""
    me = os.getpid()
    assert ls._pid_alive(me, ls._pid_start_time(me))
    assert not ls._pid_alive(me, "1")
    assert ls.exit_code_for(json.loads(Path(_receipt(tmp_path, pid=me, pid_start="1")).read_text())) == 4
    assert ls.exit_code_for(json.loads(Path(_receipt(tmp_path, pid=me, pid_start=ls._pid_start_time(me))).read_text())) == 3
    # The batch runner's worker check uses the same guard.
    assert ee._pid_alive(me, ee._pid_start_time(me)) and not ee._pid_alive(me, "1")


# --- A3.6: credential classification -------------------------------------------------------

# One documented example per pattern, each as the provider words it.
CREDENTIAL_EXAMPLES = [
    "anthropic.BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.'}}",
    "Error code: 402 - {'error': {'message': 'Insufficient credits. Add more using https://openrouter.ai/credits'}}",
    "openai.RateLimitError: Error code: 429 - {'error': {'message': 'You exceeded your current quota, please "
    "check your plan and billing details.', 'type': 'insufficient_quota', 'code': 'insufficient_quota'}}",
]


@pytest.mark.parametrize("text", CREDENTIAL_EXAMPLES)
def test_credential_examples_are_recognised_by_both_classifiers(text):
    """Including OpenAI's quota 429, which used to retry as a rate limit, untagged."""
    assert runner.classify_failure(text) == runner.FAILURE_CREDENTIALS_EXHAUSTED
    assert ee.classify_error(1, text) == "structural"
    assert ee.is_provider_credential_error(text)


@pytest.mark.parametrize(
    "wording",
    ["Your credit balance is too low", "Insufficient credits", "You exceeded your current quota", "insufficient_quota"],
)
def test_each_wording_is_recognised_without_a_status_code(wording):
    """The wording alone must classify: a CLI may relay the message without the 402/429 code."""
    text = f"API Error: {wording}."
    assert runner.classify_failure(text) == runner.FAILURE_CREDENTIALS_EXHAUSTED
    assert ee.is_credential_exhaustion(text)


def test_task_level_sample_error_mentioning_credit_is_not_a_cap():
    """A billing-themed eval's own tool error is the task's behaviour, not the provider refusing."""
    assert not ee.is_provider_credential_error("ToolError: payment declined - insufficient credits on card")


# --- A3.7: model_routing keys must name requested models -----------------------------------


def test_model_routing_typo_and_non_mapping_are_rejected(tmp_path):
    _, req = make_request(tmp_path, routing={"openai/gtp-5": {"route": "openrouter"}})
    assert any("not one of the request's models" in p for p in ee.validate_request(req))
    _, req = make_request(tmp_path, routing={"openai/gpt-5": "openrouter"})
    assert any("must be a mapping" in p for p in ee.validate_request(req))


# --- A3.9: launch race and request persistence ----------------------------------------------


def test_reserve_index_is_unique_under_concurrency(tmp_path):
    """Check-then-write let two simultaneous launches take the same `n`."""
    got: list[int] = []
    threads = [threading.Thread(target=lambda: got.append(ls.reserve_index(str(tmp_path), "t", "analyst")))
               for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 17))


def test_executor_request_files_never_collide(tmp_path):
    data = {"experiment_dir": str(tmp_path), "experiment_name": "x"}
    paths = {executor._persist_request(data) for _ in range(3)}
    assert len(paths) == 3


# --- A3.10: the lead's allow-list keeps the score blackout ----------------------------------


def test_lead_cannot_run_or_dump_evals_without_asking():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]
    assert "Bash(uv run inspect *)" not in settings["allow"]
    assert {"Bash(uv run inspect eval *)", "Bash(uv run inspect log *)"} <= set(settings["deny"])
    # Under --no-project the worker lacks inspect_ai and marks successes failed.
    assert not any("--no-project python scripts/execute_evals.py" in a for a in settings["allow"])


# --- A3.11: spend counts every attempt ------------------------------------------------------


def test_spend_sums_every_attempt_and_falls_back_for_old_status_files(tmp_path):
    usage = {"m": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    new = {"schema": 1, "run_id": "a", "pairs": {"p": {"model_usage": usage, "usage_attempts": [usage, usage, usage]}}}
    old = {"schema": 1, "run_id": "b", "pairs": {"p": {"model_usage": usage}}}
    for name, data in (("a", new), ("b", old)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "status.json").write_text(json.dumps(data))
    summary = spend.collect(str(tmp_path))
    assert summary["evals"]["tokens_by_model"]["m"]["total_tokens"] == 15 * 4


def test_preflight_and_retried_attempts_are_recorded(tmp_path, fake_eval_script, monkeypatch):
    """Probe + failed first attempt + successful retry: three attempts' tokens, not one."""
    monkeypatch.setenv("FAKE_EVAL_MODE", "transient_then_ok")
    path, _ = make_request(tmp_path, conditions={"c_1": {"task": "task.py", "args": {}}})
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    substitute_fake_eval(run_dir, fake_eval_script)
    ee.run_worker(run_dir)
    entry = ee.Status.load(run_dir).data["pairs"]
    (pair,) = entry.values()
    # The probe's first attempt fails without usage, its retry succeeds; the
    # full run succeeds first time — every attempt that spent tokens is kept.
    assert len(pair["usage_attempts"]) >= 2
    assert pair["usage_attempts"][-1] == pair["model_usage"]


# --- A4: execution_notes ---------------------------------------------------------------------


def _analyst_prompt(monkeypatch, **data) -> str:
    got: dict = {}

    async def fake_run_agent(**kwargs):
        got.update(kwargs)
        return "ok"

    monkeypatch.setattr(analyst, "run_agent", fake_run_agent)
    import asyncio

    asyncio.run(analyst.run_transcript_analyst(topic="t", transcript_source={"condition_A": "/a"}, **data))
    return got["prompt"]


@pytest.mark.parametrize("spec", [None, {"description": "d"}])  # Orchestrator mode, forensic mode
def test_execution_notes_render_with_their_rule_in_both_modes(monkeypatch, spec):
    extra = {"behaviour_spec": spec} if spec else {}
    prompt = _analyst_prompt(monkeypatch, execution_notes=["condition_A: 3 samples errored"], **extra)
    assert "## Execution Notes\n\n- condition_A: 3 samples errored\n\n" + EXECUTION_NOTES_RULE in prompt


def test_execution_notes_accept_a_string_and_skip_blank(monkeypatch):
    assert "- one fact" in _analyst_prompt(monkeypatch, execution_notes="one fact")
    assert "## Execution Notes" not in _analyst_prompt(monkeypatch, execution_notes=["  "])


def test_malformed_execution_notes_are_an_agent_error(monkeypatch):
    """Dropped silently, the Analyst would never hear about attrition; rejected, the CLI exits 2."""
    with pytest.raises(RuntimeError, match="execution_notes"):
        _analyst_prompt(monkeypatch, execution_notes={"condition_A": 3})
