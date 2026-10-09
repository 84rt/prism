"""Shared fixtures for the Prism scaffold tests.

The tests are self-contained: they run with the pinned versions the six
forensics runs used and never touch the case directories' venvs. Run with::

    uv run --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" \
        --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/

The scaffold has no package install, so the repo root (for `subagents.*`) and
`scripts/` are put on `sys.path` here. Regression tests over the persisted
forensics corpus are skipped (not failed) when the corpus is absent — it is
container-local and never committed.
"""

from __future__ import annotations

import os
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
        created=now,
        task="fixture_task",
        task_id="t1",
        task_version=0,
        task_file="task.py",
        dataset=EvalDataset(samples=1, sample_ids=["s1"]),
        model="mockllm/model",
        config=EvalConfig(epochs=epochs, sample_id=["s1"]),
        metadata={"condition": "baseline"},
        tags=["exp:fixture", "cond:baseline"],
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
    ``structural`` (prints a structural error, exits 1) or ``zero`` (a log with
    zero completed samples). Stdout mimics Inspect's plain display, including
    the score-bearing progress line and results panel.
    """
    script = tmp_path / "fake_eval.py"
    script.write_text(
        "import sys, os\n"
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
        "write_fixture_log(os.path.join(log_dir, 'f.eval'), epochs=epochs, with_error=(mode == 'sample_error'))\n"
        "print('Steps:  10/10 100% Samples:   2/2 accuracy: 0.73')\n"
        "print('\\u256d\\u2500 fixture_task \\u2500\\u256e')\n"
        "print('\\u2502 accuracy  0.7317  \\u2502')\n"
        "print('\\u2570\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u256f')\n"
    )
    return str(script)
