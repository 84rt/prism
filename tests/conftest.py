"""Shared fixtures for the Prism scaffold tests.

The tests are self-contained: they run with the pinned versions the six
forensics runs used and never touch the case directories' venvs. Run with::

    uv run --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" \
        --with claude-agent-sdk==0.2.165 --with python-dotenv --with pytest pytest tests/

The scaffold has no package install, so the repo root (for `subagents.*`) and
`scripts/` are put on `sys.path` here. Regression tests over the persisted
forensics corpus are skipped (not failed) when the corpus is absent — it is
container-local and never committed.

Every test runs under an autouse spend guard (see `_spend_guard`): the Agent
SDK's `query` raises and the provider keys are unset, so no test can reach a
paid provider by accident.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT  # alias used by the Critic/ledger tests
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Never write bytecode into the read-only corpus when a test imports a persisted
# grader module from a case directory.
sys.dont_write_bytecode = True


# --- Spend guard (every test) ---
#
# The suite must never reach a paid provider. Every sub-agent path ends in the
# Agent SDK's `query`, and every eval path needs a provider key, so the guard
# makes `query` raise and removes the keys for every test (subprocesses inherit
# the cleaned environment). A test that stubs `query` itself simply patches
# over the guard; a test that genuinely needs otherwise opts out with
# ``@pytest.mark.allow_sdk``.

PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")


def pytest_configure(config):
    """Register the spend-guard opt-out marker.

    Parameters
    ----------
    config : pytest.Config
        The session config.
    """
    config.addinivalue_line(
        "markers",
        "allow_sdk: opt out of the autouse spend guard (real SDK `query` and provider keys)",
    )


def _refuse_query(*args, **kwargs):
    """Stand-in for `claude_agent_sdk.query` that fails loudly instead of spending.

    Raises
    ------
    RuntimeError
        Always.
    """
    raise RuntimeError(
        "spend guard: a test reached claude_agent_sdk.query; stub `subagents.runner.query` "
        "or mark the test allow_sdk"
    )


@pytest.fixture(autouse=True)
def _spend_guard(request, monkeypatch):
    """Make the Agent SDK's `query` raise and clear provider keys, per test.

    Parameters
    ----------
    request : pytest.FixtureRequest
        Used to honour the ``allow_sdk`` opt-out marker.
    monkeypatch : pytest.MonkeyPatch
        Undoes both changes after the test.
    """
    if request.node.get_closest_marker("allow_sdk"):
        return
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    try:
        import claude_agent_sdk

        monkeypatch.setattr(claude_agent_sdk, "query", _refuse_query)
        # The runner bound the name at import time, so patch its copy too.
        from subagents import runner

        monkeypatch.setattr(runner, "query", _refuse_query)
    except ImportError:  # SDK not installed: nothing can spend through it
        pass


# --- Forensics corpus (container-local; see the module docstring) ---

# A Path, so `CASES_ROOT / "case-…"` works; it also formats cleanly inside
# f-strings and is accepted by `os.path.exists`.
CASES_ROOT = Path("/workspaces/forensics-cases")


def requires_cases(path: str | Path = CASES_ROOT):
    """Skip marker for tests that need the on-disk forensics corpus.

    Parameters
    ----------
    path : str | Path
        A path that must exist for the test to run.

    Returns
    -------
    pytest.MarkDecorator
    """
    return pytest.mark.skipif(not os.path.exists(path), reason=f"forensics corpus not present at {path}")


def case_path(relative: str) -> Path:
    """Resolve a path inside the forensics corpus, skipping if it is missing.

    The in-test counterpart of `requires_cases`, for tests that only learn
    which fixture they need at run time (e.g. parametrised over reports).

    Parameters
    ----------
    relative : str
        Path relative to ``/workspaces/forensics-cases``.

    Returns
    -------
    Path
        The absolute path, guaranteed to exist.
    """
    path = CASES_ROOT / relative
    if not path.exists():
        pytest.skip(f"forensics corpus fixture not present: {path}")
    return path


@pytest.fixture
def repo_root() -> Path:
    """The scaffold checkout under test.

    Returns
    -------
    Path
        Absolute path to the repository root.
    """
    return REPO_ROOT


# --- Execution-layer fixtures (scored logs for the blackout tests) ---


def write_fixture_log(
    path: str,
    *,
    epochs: int = 2,
    metrics: dict[str, float] | None = None,
    with_error: bool = False,
    error_message: str = "boom",
    errored: int = 1,
    no_assistant: int = 0,
    status: str = "success",
    completed: int | None = None,
    task: str = "fixture_task",
    task_file: str = "task.py",
    tags: list[str] | None = None,
    metadata: dict | None = None,
    task_args: dict | None = None,
    dataset_name: str | None = None,
    created: str | None = None,
) -> str:
    """Write a small **scored** .eval log so the blackout tests have something to leak.

    Parameters
    ----------
    path : str
        Destination ``.eval`` path.
    epochs : int
        Number of samples (all id ``s1``, one per epoch).
    metrics : dict | None
        Scorer metrics to embed in the header (defaults to a recognisable pair).
    with_error : bool
        Give the last ``errored`` samples an error.
    error_message : str
        The errored samples' message, e.g. a provider's credit-cap refusal.
    errored : int
        How many trailing samples error when ``with_error`` is set.
    no_assistant : int
        How many samples carry no assistant message.
    status : str
        Log status.
    completed : int | None
        ``completed_samples`` in the header (defaults to ``epochs``).
    task, task_file : str
        The header's task name and ``task_file`` (the blinding tests craft
        leaky ones).
    tags, metadata : list[str] | dict | None
        Header tags and metadata; default to a ``baseline`` condition.
    task_args : dict | None
        The header's ``task_args`` (the ``-T`` values).
    dataset_name : str | None
        The header's dataset name (the blinding tests' dataset channel).
    created : str | None
        The header's ``created`` timestamp (defaults to now); the blinding
        tests use an older one to stand in for a log from another run.

    Returns
    -------
    str
        The path written.
    """
    from inspect_ai.log import (
        EvalConfig,
        EvalDataset,
        EvalLog,
        EvalMetric,
        EvalResults,
        EvalSample,
        EvalScore,
        EvalSpec,
        EvalStats,
        write_eval_log,
    )
    from inspect_ai.log._log import EvalError
    from inspect_ai.model import ChatMessageAssistant, ChatMessageUser, ModelUsage
    from inspect_ai.scorer import Score

    metrics = metrics or {"accuracy": 0.7317, "stderr": 0.0912}
    now = datetime.now(timezone.utc).isoformat()
    spec = EvalSpec(
        run_id="fixture-run",
        created=created or now,
        task=task,
        task_id="t1",
        task_version=0,
        task_file=task_file,
        task_args=task_args or {},
        dataset=EvalDataset(name=dataset_name, samples=1, sample_ids=["s1"]),
        model="mockllm/model",
        config=EvalConfig(epochs=epochs, sample_id=["s1"]),
        metadata={"condition": "baseline"} if metadata is None else metadata,
        tags=["exp:fixture", "cond:baseline"] if tags is None else tags,
    )
    samples = []
    for i in range(epochs):
        msgs = [ChatMessageUser(content="hi")]
        if i >= no_assistant:
            msgs.append(ChatMessageAssistant(content="LEAKY_ANSWER"))
        err = None
        if with_error and i >= epochs - errored:
            err = EvalError(message=error_message, traceback="tb", traceback_ansi="tb")
        samples.append(
            EvalSample(
                id="s1",
                epoch=i + 1,
                input="hi",
                target="yes",
                messages=msgs,
                scores={"grader": Score(value=0.9871)},
                error=err,
                model_usage={"mockllm/model": ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15)},
            )
        )
    results = EvalResults(
        total_samples=epochs,
        completed_samples=epochs if completed is None else completed,
        scores=[
            EvalScore(
                name="grader",
                scorer="grader",
                metrics={k: EvalMetric(name=k, value=v) for k, v in metrics.items()},
            )
        ],
    )
    stats = EvalStats(
        started_at=now,
        completed_at=now,
        model_usage={
            "mockllm/model": ModelUsage(
                input_tokens=20 * epochs, output_tokens=10 * epochs, total_tokens=30 * epochs
            )
        },
    )
    log = EvalLog(version=2, status=status, eval=spec, results=results, stats=stats, samples=samples)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_eval_log(log, path)
    return path


# Values that must never appear in anything the runner prints or persists
# outside private/: the metric names and values, and the per-sample score.
LEAK_TOKENS = ["accuracy", "0.7317", "stderr\"", "0.0912", "0.9871"]


@pytest.fixture
def fixture_log(tmp_path):
    return write_fixture_log(str(tmp_path / "logs" / "baseline" / "fixture.eval"))


@pytest.fixture
def fake_eval_script(tmp_path):
    """A stand-in for `inspect eval`: writes a scored fixture log into --log-dir.

    Tests substitute it for the rendered commands in a run's ``plan.json``, so
    the worker exercises its real code path with no model and no Inspect CLI.
    ``FAKE_EVAL_MODE`` selects the outcome: ``ok`` (default), ``sample_error``,
    ``structural`` (prints a structural error, exits 1), ``zero`` (a log with
    zero completed samples) or ``transient_then_ok`` (the first attempt per log
    directory leaves a partial ``status=error`` log, prints a transient error
    and exits 1; later attempts succeed — the per-directory attempt count is
    kept under ``<tmp_path>/fake_eval_state``). Stdout mimics Inspect's plain
    display, including the score-bearing progress line and results panel.
    """
    state = tmp_path / "fake_eval_state"
    script = tmp_path / "fake_eval.py"
    script.write_text(
        "import sys, os, hashlib\n"
        f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
        "from conftest import write_fixture_log\n"
        "args = sys.argv[1:]\n"
        "log_dir = args[args.index('--log-dir') + 1]\n"
        "epochs = int(args[args.index('--epochs') + 1]) if '--epochs' in args else 1\n"
        "mode = os.environ.get('FAKE_EVAL_MODE', 'ok')\n"
        "if mode == 'structural':\n"
        "    print('Steps:   0/10   0% Samples:   0/2 accuracy: 0.00')\n"
        "    print('ModuleNotFoundError: No module named agentic_misalignment')\n"
        "    sys.exit(1)\n"
        "if mode == 'zero':\n"
        "    write_fixture_log(os.path.join(log_dir, 'z.eval'), epochs=epochs, completed=0)\n"
        "    sys.exit(0)\n"
        "if mode == 'transient_then_ok':\n"
        f"    state = {str(state)!r}\n"
        "    os.makedirs(state, exist_ok=True)\n"
        "    counter = os.path.join(state, hashlib.sha1(log_dir.encode()).hexdigest())\n"
        "    n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
        "    with open(counter, 'w') as f:\n"
        "        f.write(str(n + 1))\n"
        "    if n == 0:\n"
        "        write_fixture_log(os.path.join(log_dir, 'partial.eval'), epochs=epochs, status='error', completed=0)\n"
        "        print('ConnectionError: Connection reset by peer')\n"
        "        sys.exit(1)\n"
        "write_fixture_log(os.path.join(log_dir, 'f.eval'), epochs=epochs, with_error=(mode == 'sample_error'))\n"
        "print('Steps:  10/10 100% Samples:   2/2 accuracy: 0.73')\n"
        "print('\\u256d\\u2500 fixture_task \\u2500\\u256e')\n"
        "print('\\u2502 accuracy  0.7317  \\u2502')\n"
        "print('\\u2570\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u256f')\n"
    )
    return str(script)


# --- Batch-runner request helpers (shared by the execute_evals and route tests) ---


def make_request(tmp_path, *, conditions=None, models=None, overrides=None, routing=None):
    """Write a minimal valid `execute_evals.py` request under ``tmp_path``.

    Parameters
    ----------
    tmp_path : Path
        Directory to hold ``exp/`` (the experiment, with a ``task.py``) and
        ``request.json``.
    conditions, models, overrides, routing : optional
        Override the request's ``conditions``, ``models``, ``overrides`` and
        ``model_routing``; the defaults are a two-condition, one-model request.

    Returns
    -------
    tuple[str, dict]
        The request path and the request as a dict.
    """
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


def substitute_fake_eval(run_dir: str, fake_script: str, *, prefix: str = "") -> None:
    """Point every rendered command at the fake eval, keeping the flags.

    Also zeroes the retry backoff schedule: retries are exercised by the
    ``transient_then_ok`` mode, and the runner's default 10 s first backoff
    would otherwise be a fixed sleep in the suite.

    Parameters
    ----------
    run_dir : str
        A run directory prepared by `execute_evals.prepare_run`.
    fake_script : str
        The `fake_eval_script` fixture's path.
    prefix : str
        Shell text placed before the main command only (not the preflight or
        retest probes), e.g. a sentinel-file gate that holds the pair running.
    """
    plan_path = Path(run_dir) / "plan.json"
    plan = json.loads(plan_path.read_text())
    for p in plan["pairs"]:
        for key in ("command", "preflight_command", "retest_command"):
            parts = shlex.split(p[key])
            i = parts.index("eval")  # "... inspect eval task.py ..."
            p[key] = " ".join(shlex.quote(x) for x in [sys.executable, fake_script] + parts[i + 1 :])
        p["command"] = prefix + p["command"]
    plan["execution"]["retry_backoff_seconds"] = [0, 0, 0]
    plan_path.write_text(json.dumps(plan))


def gate_prefix(gate: Path, started: Path | None = None) -> str:
    """Shell prefix that holds a command until ``gate`` exists.

    Replaces fixed ``sleep N &&`` prefixes: the test decides when the command
    proceeds, so it is neither racy on a loaded host nor slower than needed.

    Parameters
    ----------
    gate : Path
        File whose creation releases the command.
    started : Path | None
        Touched first, so the test can wait until the command is really running.

    Returns
    -------
    str
        e.g. ``"touch /t/started && while [ ! -e /t/gate ]; do sleep 0.05; done && "``.
    """
    touch = f"touch {shlex.quote(str(started))} && " if started else ""
    return f"{touch}while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.05; done && "


def wait_for_file(path: Path, timeout: float = 30.0) -> None:
    """Poll until ``path`` exists (a sentinel the code under test creates).

    Parameters
    ----------
    path : Path
        The sentinel file.
    timeout : float
        Seconds before failing the test.
    """
    import time

    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"sentinel {path} never appeared")
        time.sleep(0.02)
