"""Eval batch runner for the Prism scaffold.

Runs the condition-model pairs of an Executor request (the contract's request
JSON, see ``executor_interface_contract.md``) against Inspect AI, and reports
what happened — never how anything scored.

The runner is the deterministic core of the execution layer. It builds the
``inspect eval`` commands itself, runs preflight, launches the batch
**detached** (its own session, so it survives the caller's teardown), keeps a
score-free status file up to date, and assembles a draft report in the
contract's section shape. The Experiment Executor sub-agent drives it and
supplies the judgement — diagnosing structural failures, deciding on a re-run,
and signing off the report.

Usage
-----
    uv run python scripts/execute_evals.py run <request.json> [options]
    uv run python scripts/execute_evals.py status <run_dir> [--json]
    uv run python scripts/execute_evals.py wait <run_dir> [--timeout S]
    uv run python scripts/execute_evals.py report <run_dir>

``run`` validates the request, builds the commands, launches the batch
detached and prints a receipt (run directory, pid, status path). ``status``
and ``wait`` read the status file; ``wait`` blocks until the run reaches a
terminal state or the timeout passes (exit 0 terminal, 3 still running).
``report`` renders the draft report from the status file.

Options for ``run``: ``--foreground`` (run the batch inline — tests and quick
evals), ``--max-parallel N`` / ``--max-connections N`` (raise or lower the
ceiling; must be accompanied by ``--rationale``), ``--only <pair_id,...>``
(re-run a subset of pairs), ``--quarantine-existing-logs`` (move stale
``.eval`` files out of a pair's log directory instead of refusing to run).

Score blackout
--------------
Nothing this script prints, and nothing in the status file or draft report,
carries a scorer output. Scorer metrics are never extracted from the log
header; sample summaries are read for their *shape* fields only (errors,
limits, retries, turn counts); each command's raw stdout/stderr — which
Inspect's plain display fills with running metrics — is written under
``<run_dir>/private/`` and never re-read except to build a *scrubbed*
diagnostic tail for failed commands.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Shutdown event — set by SIGINT / SIGTERM handlers
# ---------------------------------------------------------------------------
_SHUTDOWN = threading.Event()


def _handle_signal(signum: int, frame: Any) -> None:
    _SHUTDOWN.set()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default concurrency ceiling. Sized for a shared host running one Docker
# sandbox per sample: one eval subprocess at a time, four concurrent model
# connections inside it. These are the values R06 recovered by hand after an
# unconstrained Executor opened 24 containers on a 10-core box. A request may
# override them via `overrides`; the Executor agent may raise them only via
# `--max-parallel` / `--max-connections` with a `--rationale`, which the draft
# report records verbatim.
DEFAULT_MAX_PARALLEL = 1
DEFAULT_MAX_CONNECTIONS = 4

# Per-command wall-clock limit. Generous: a 30-epoch sandboxed eval with a
# 600 s task limit at four connections is over an hour, and the batch runs
# detached so nothing above it is waiting.
DEFAULT_COMMAND_TIMEOUT = 4 * 3600

# `openai` floor Inspect's OpenRouter provider needs; see the contract.
OPENROUTER_OPENAI_PIN = "openai>=2.45.0"

# Override keys the runner acts on. Anything else is recorded as unrecognised.
RECOGNISED_OVERRIDES = {
    "sample_limit",
    "sample_id",
    "epochs",
    "skip_preflight",
    "max_parallel",
    "max_connections",
    "command_timeout_seconds",
}

PREFLIGHT_DIRNAME = "_preflight"

# Terminal run states.
TERMINAL_STATES = {"completed", "interrupted", "failed"}

# Status schema version, bumped if the file's shape changes incompatibly.
STATUS_SCHEMA = 1


# ---------------------------------------------------------------------------
# Data classes (engine level)
# ---------------------------------------------------------------------------


@dataclass
class CommandSpec:
    id: str
    command: str
    log_dir: str
    cwd: str | None = None


@dataclass
class ExecutionConfig:
    max_parallel: int = DEFAULT_MAX_PARALLEL
    max_retries: int = 3
    retry_backoff_seconds: list[int] = field(default_factory=lambda: [10, 30, 60])
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT


@dataclass
class EvalResult:
    id: str
    log_path: str | None = None
    status: str = "not_run"
    samples_completed: int | None = None
    samples_total: int | None = None
    duration_seconds: float | None = None
    process_retries: int = 0
    batch: int = 0
    errors: list[str] = field(default_factory=list)
    error_class: str | None = None
    model_usage: dict[str, dict[str, int | None]] = field(default_factory=dict)


@dataclass
class ConcurrencyReduction:
    batch: int
    previous_level: int
    new_level: int
    reason: str


@dataclass
class ExecutionReport:
    status: str = "completed"
    results: list[EvalResult] = field(default_factory=list)
    concurrency_used: int = 1
    concurrency_reductions: list[ConcurrencyReduction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_wall_clock_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_STRUCTURAL_PATTERNS = [
    "ModuleNotFoundError",
    "ImportError",
    "FileNotFoundError",
    "SyntaxError",
    "TaskError",
    "invalid model",
    "AuthenticationError",
    "API key",
    # Provider prerequisite failures. Inspect names the env var directly
    # ("No OPENROUTER_API_KEY defined in the environment"), which the spaced
    # "API key" pattern above does not match — without these, a missing key
    # classifies as unknown and gets pointlessly retried.
    "API_KEY",
    "defined in the environment",
    "Unable to initialise",
    "Unable to initialize",
    # Missing/outdated provider SDKs. Inspect's OpenRouter provider requires a
    # recent `openai` package and refuses to start without it; retrying cannot
    # help, and the remedy (pin the dependency on the invocation) is the
    # orchestrator's to apply.
    "requires optional dependencies",
    "requires at least version",
    "No such file",
    "PermissionError",
    "dataset",
    "No module named",
]

_RESOURCE_CONTENTION_PATTERNS = [
    "too many open files",
    "Cannot allocate memory",
    "no space left on device",
    "pool is full",
    "max_sandboxes",
    "Docker daemon",
    "error creating container",
    "Cannot connect to the Docker",
    "Resource temporarily unavailable",
]

# OpenRouter routing failures. These are structural in the sense that retrying
# the identical command cannot help — the pinned provider is unavailable, or the
# account cannot pay — but they are kept separate from _STRUCTURAL_PATTERNS so
# the Executor can report them under their own category. The remedy is the
# orchestrator's (wait, re-pin, or drop the model), not a configuration fix, and
# relaxing the pin to make the command succeed would silently change the
# measurement instrument mid-experiment.
_ROUTING_PATTERNS = [
    "no allowed providers",
    "no endpoints found",
    "no providers available",
    "requires more credits",
    "insufficient credits",
    "402",
]

_TRANSIENT_PATTERNS = [
    "ConnectionError",
    "TimeoutError",
    "rate limit",
    "429",
    "503",
    "502",
    "CUDA",
    "RuntimeError",
    "Temporary failure",
    "Network",
    "unexpected EOF",
    "Connection reset",
]


# Lines of Inspect's plain display that carry scorer values. The progress line
# appends the headline metric after the sample counter ("Samples: 3/10
# accuracy: 0.67"); the final results block prints metrics as space-aligned
# columns ("accuracy  0.000  accuracy  0.000" / "stderr    0.000"), sometimes
# inside a rich box. All three shapes are dropped wholesale, and any residual
# "name: 0.xx" token is redacted, before a diagnostic tail is stored anywhere
# readable.
_PROGRESS_LINE = re.compile(r"Samples:\s*\d+\s*/\s*\d+")
_PANEL_LINE = re.compile(r"^\s*[─-╿]")  # box-drawing characters
# Metric names are arbitrary (`gpt-4o`, `accuracy[max]`, `level-1` for grouped
# metrics), hence `\S+` rather than a word class. `_METRIC_TOKEN` will also
# redact innocent `name: 1.5` tokens in error text (`version: 2.45`); that
# collateral is accepted because the blackout is absolute.
_METRIC_COLUMNS = re.compile(r"^\s*(\S+\s{2,}(n/a|-?\d+\.\d+)\s*)+$")
_METRIC_TOKEN = re.compile(r"(?<!\S)([^\s:]+):\s*(n/a|-?\d+\.\d+)\b")


def scrub_scores(text: str) -> str:
    """Remove scorer-bearing lines and tokens from captured eval output.

    Parameters
    ----------
    text : str
        Raw stdout or stderr of an ``inspect eval`` subprocess.

    Returns
    -------
    str
        The same text with progress lines, results-panel lines and residual
        ``name: 0.xx`` tokens removed or redacted.
    """
    kept: list[str] = []
    for line in text.splitlines():
        if _PROGRESS_LINE.search(line) or _PANEL_LINE.match(line) or _METRIC_COLUMNS.match(line):
            continue
        kept.append(_METRIC_TOKEN.sub(r"\1: <redacted>", line))
    return "\n".join(kept)


def combine_output(stdout: str, stderr: str, max_lines: int = 50) -> str:
    """Merge a failed command's output streams into a scrubbed diagnostic tail.

    Inspect writes startup failures (missing API key, unresolvable model,
    provider initialisation errors) to **stdout**, not stderr — so classifying
    or reporting on stderr alone silently discards the only evidence of the most
    common structural failures. Both streams are combined here, after the
    scorer-bearing lines have been removed from each.

    The tail is taken rather than the head because progress output precedes the
    error: with a per-sample display mode, the first lines of a failing run are
    routine progress and the actual cause is last.

    Parameters
    ----------
    stdout : str
        Captured standard output.
    stderr : str
        Captured standard error.
    max_lines : int
        Number of trailing lines to retain, matching the contract's ~50-line
        stderr-tail requirement for structural-failure evidence.

    Returns
    -------
    str
        The trailing lines of the combined, scrubbed output, stripped.
    """
    parts = [scrub_scores(part).strip() for part in (stderr, stdout)]
    combined = "\n".join(p for p in parts if p)
    lines = combined.splitlines()
    return "\n".join(lines[-max_lines:]).strip()


def classify_error(returncode: int, output: str, log_status: str | None) -> str:
    """Classify an error as routing, structural, transient, resource_contention, or unknown.

    Parameters
    ----------
    returncode : int
        The subprocess exit code (negative when killed by a signal).
    output : str
        Combined stdout+stderr from `combine_output` — Inspect reports
        prerequisite failures on stdout.
    log_status : str | None
        The status of the newest eval log, if one was found.

    Returns
    -------
    str
        One of ``resource_contention``, ``routing``, ``structural``,
        ``transient`` or ``unknown``.
    """
    combined = output.lower()

    for pattern in _RESOURCE_CONTENTION_PATTERNS:
        if pattern.lower() in combined:
            return "resource_contention"

    # Checked before structural: an OpenRouter routing failure often also
    # mentions the model, which would otherwise match a structural pattern and
    # obscure the actual (and differently-remedied) cause.
    for pattern in _ROUTING_PATTERNS:
        if pattern.lower() in combined:
            return "routing"

    for pattern in _STRUCTURAL_PATTERNS:
        if pattern.lower() in combined:
            return "structural"

    for pattern in _TRANSIENT_PATTERNS:
        if pattern.lower() in combined:
            return "transient"

    # Killed by signal
    if returncode < 0:
        return "transient"

    return "unknown"


def should_retry(error_class: str, attempt: int, max_retries: int) -> bool:
    """Decide whether to retry based on error class and attempt count."""
    # Routing failures share structural's "retrying the same command cannot
    # help" property: the pinned provider is unavailable or the account cannot
    # pay, and neither resolves within a backoff window.
    if error_class in ("structural", "routing"):
        return False
    if error_class == "unknown":
        return attempt < min(1, max_retries)  # retry unknown at most once, but respect max_retries
    return attempt < max_retries


def compute_backoff(attempt: int, schedule: list[int]) -> float:
    """Get backoff duration for a given attempt."""
    if not schedule:
        return 10.0
    return float(schedule[min(attempt, len(schedule) - 1)])


# ---------------------------------------------------------------------------
# Log verification (score-free)
# ---------------------------------------------------------------------------


def find_newest_log(log_dir: str) -> str | None:
    """Find the most recently modified .eval file in a directory."""
    log_path = Path(log_dir)
    if not log_path.is_dir():
        return None

    eval_files = sorted(log_path.glob("*.eval"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not eval_files:
        return None
    return str(eval_files[0])


def verify_eval_log(log_dir: str) -> dict[str, Any]:
    """Read the newest eval log's header and extract execution-shape fields.

    Scorer metrics are deliberately **not** extracted. The header carries
    ``results.scores[*].metrics``; reading them here would put a rate in front
    of whoever reads this script's output before the Analyst reports, which is
    the blackout the pipeline exists to keep. Token usage is execution shape,
    not a scorer output, and is kept for the spend ledger.

    Parameters
    ----------
    log_dir : str
        Directory whose newest ``.eval`` file is verified.

    Returns
    -------
    dict
        ``log_path``, ``status``, and when available ``samples_total``,
        ``samples_completed``, ``duration_seconds``, ``model_usage`` and
        ``error``. Example::

            {"log_path": ".../x.eval", "status": "success",
             "samples_total": 30, "samples_completed": 30,
             "duration_seconds": 248.1,
             "model_usage": {"openai/gpt-5": {"input_tokens": 1200, ...}}}
    """
    log_path = find_newest_log(log_dir)
    if log_path is None:
        return {"log_path": None, "status": "no_log", "error": "No .eval file found in log directory"}

    try:
        from inspect_ai.log import read_eval_log

        log = read_eval_log(log_path, header_only=True)

        result: dict[str, Any] = {
            "log_path": log_path,
            "status": str(log.status),
        }

        if log.results is not None:
            result["samples_total"] = log.results.total_samples
            result["samples_completed"] = log.results.completed_samples

        if log.stats is not None:
            started = log.stats.started_at
            completed = log.stats.completed_at
            if started and completed:
                if isinstance(started, str):
                    started = datetime.fromisoformat(started)
                if isinstance(completed, str):
                    completed = datetime.fromisoformat(completed)
                result["duration_seconds"] = (completed - started).total_seconds()
            usage = {}
            for model_name, mu in (log.stats.model_usage or {}).items():
                usage[model_name] = {
                    "input_tokens": getattr(mu, "input_tokens", None),
                    "output_tokens": getattr(mu, "output_tokens", None),
                    "total_tokens": getattr(mu, "total_tokens", None),
                    "input_tokens_cache_read": getattr(mu, "input_tokens_cache_read", None),
                    "input_tokens_cache_write": getattr(mu, "input_tokens_cache_write", None),
                    "reasoning_tokens": getattr(mu, "reasoning_tokens", None),
                }
            result["model_usage"] = usage

        if log.error is not None:
            result["error"] = str(log.error.message) if hasattr(log.error, "message") else str(log.error)

        return result

    except Exception as e:
        return {"log_path": log_path, "status": "verification_error", "error": str(e)}


def sample_shape(log_path: str) -> dict[str, Any]:
    """Count transcript endpoints in a log without touching its scores.

    Uses the sample summaries (which carry shape fields alongside scores; only
    the shape fields are read) and falls back to the samples' message roles for
    the no-assistant-message count when the summaries predate ``turn_count``.

    Parameters
    ----------
    log_path : str
        Path to a ``.eval`` file.

    Returns
    -------
    dict
        ``samples``, ``sample_errors``, ``sample_retries``, and
        ``termination`` = ``{no_assistant_msg, limit_hit, error_terminated}``.
        Example: ``{"samples": 30, "sample_errors": 1, "sample_retries": 2,
        "termination": {"no_assistant_msg": 0, "limit_hit": 3,
        "error_terminated": 1}}``.
    """
    from inspect_ai.log import read_eval_log_sample_summaries

    summaries = read_eval_log_sample_summaries(log_path)
    errors = sum(1 for s in summaries if s.error is not None)
    retries = sum(int(s.retries or 0) for s in summaries)
    limit_hit = sum(1 for s in summaries if s.limit is not None)

    turn_counts = [s.turn_count for s in summaries]
    if summaries and all(tc is not None for tc in turn_counts):
        no_assistant = sum(1 for tc in turn_counts if tc == 0)
    else:
        # Older logs (and synthetic ones) have no turn_count; read message
        # roles from the samples. Only the roles are looked at.
        from inspect_ai.log import read_eval_log

        full = read_eval_log(log_path)
        no_assistant = sum(
            1
            for s in (full.samples or [])
            if not any(getattr(m, "role", None) == "assistant" for m in (s.messages or []))
        )

    return {
        "samples": len(summaries),
        "sample_errors": errors,
        "sample_retries": retries,
        "termination": {
            "no_assistant_msg": no_assistant,
            "limit_hit": limit_hit,
            "error_terminated": errors,
        },
    }


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate a child and everything in its process group."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()


def run_single(
    command: str,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    cwd: str | None = None,
    capture_dir: str | None = None,
    capture_name: str | None = None,
) -> tuple[int, float, str, str]:
    """Run a single subprocess in its own process group.

    Parameters
    ----------
    command : str
        Shell command line.
    timeout : int
        Wall-clock limit in seconds.
    cwd : str | None
        Working directory for the subprocess.
    capture_dir : str | None
        If set, raw stdout/stderr are also written to
        ``<capture_dir>/<capture_name>.stdout`` / ``.stderr``. These files are
        score-bearing (Inspect prints running metrics) and live under the run
        directory's ``private/`` area.
    capture_name : str | None
        Basename for the capture files.

    Returns
    -------
    tuple[int, float, str, str]
        ``(returncode, duration_seconds, stdout, stderr)``.
    """
    start = time.monotonic()
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # Poll so a shutdown signal can stop the child promptly.
        deadline = start + timeout
        while True:
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
                break
            except subprocess.TimeoutExpired:
                if _SHUTDOWN.is_set():
                    _kill_group(proc)
                    return -1, time.monotonic() - start, "", "Interrupted by shutdown signal"
                if time.monotonic() > deadline:
                    _kill_group(proc)
                    return -1, time.monotonic() - start, "", f"Process timed out after {timeout}s"

        duration = time.monotonic() - start
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if capture_dir and capture_name:
            Path(capture_dir).mkdir(parents=True, exist_ok=True)
            Path(capture_dir, f"{capture_name}.stdout").write_text(stdout)
            Path(capture_dir, f"{capture_name}.stderr").write_text(stderr)
        return proc.returncode or 0, duration, stdout, stderr
    except Exception as e:
        if proc is not None:
            _kill_group(proc)
        return -1, time.monotonic() - start, "", str(e)


def run_batch(
    commands: list[CommandSpec],
    concurrency: int,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    capture_dir: str | None = None,
    attempt_index: dict[str, int] | None = None,
) -> list[tuple[CommandSpec, int, float, str, str]]:
    """Run a batch of commands with the given concurrency level.

    Returns list of (command, returncode, duration, stdout, stderr).
    """
    results: list[tuple[CommandSpec, int, float, str, str]] = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_cmd = {}
        for cmd in commands:
            if _SHUTDOWN.is_set():
                break
            n = (attempt_index or {}).get(cmd.id, 0)
            future = executor.submit(
                run_single, cmd.command, timeout, cmd.cwd, capture_dir, f"{cmd.id}.attempt{n}"
            )
            future_to_cmd[future] = cmd

        for future in as_completed(future_to_cmd):
            cmd = future_to_cmd[future]
            try:
                returncode, duration, stdout, stderr = future.result()
            except Exception as e:
                returncode, duration, stdout, stderr = -1, 0.0, "", str(e)
            results.append((cmd, returncode, duration, stdout, stderr))

    return results


def _stash_failed_attempt_logs(cmd: CommandSpec, attempt: int, capture_dir: str | None) -> None:
    """Move any .eval a failed attempt wrote out of the pair's log directory.

    Parameters
    ----------
    cmd : CommandSpec
        The command whose log directory is cleaned.
    attempt : int
        The attempt index that just failed.
    capture_dir : str | None
        The run's private capture area; when None the files are left alone.
    """
    if not capture_dir or not os.path.isdir(cmd.log_dir):
        return
    stale = list(Path(cmd.log_dir).glob("*.eval"))
    if not stale:
        return
    dest = os.path.join(capture_dir, "failed_attempts", cmd.id, f"attempt{attempt}")
    os.makedirs(dest, exist_ok=True)
    for s in stale:
        shutil.move(str(s), os.path.join(dest, s.name))


# ---------------------------------------------------------------------------
# Engine: run a set of commands with retries and graceful degradation
# ---------------------------------------------------------------------------


def execute(
    commands: list[CommandSpec],
    config: ExecutionConfig,
    capture_dir: str | None = None,
    on_event: Any = None,
) -> ExecutionReport:
    """Run all commands with error handling, retries, and graceful degradation.

    Parameters
    ----------
    commands : list[CommandSpec]
        Commands to run.
    config : ExecutionConfig
        Concurrency, retry and timeout parameters.
    capture_dir : str | None
        Where raw per-attempt stdout/stderr are written (score-bearing; see
        `run_single`).
    on_event : callable | None
        ``on_event(kind, payload)`` called with ``("batch_start", [ids])``,
        ``("result", EvalResult)`` and ``("reduction", ConcurrencyReduction)``
        so a status file can be kept current while the batch runs.

    Returns
    -------
    ExecutionReport
        Per-command results in the original order plus batch-level fields.
    """
    report = ExecutionReport(concurrency_used=config.max_parallel)
    total_start = time.monotonic()

    def emit(kind: str, payload: Any) -> None:
        if on_event is not None:
            on_event(kind, payload)

    if not commands:
        report.total_wall_clock_seconds = 0.0
        return report

    # Track per-command state
    pending = list(commands)
    completed_results: dict[str, EvalResult] = {}
    retry_counts: dict[str, int] = {c.id: 0 for c in commands}
    current_concurrency = config.max_parallel
    batch_num = 0

    while pending and not _SHUTDOWN.is_set():
        batch_num += 1

        # Partition into batches of current_concurrency
        batch = pending[:current_concurrency]
        pending = pending[current_concurrency:]
        emit("batch_start", [c.id for c in batch])

        # Run the batch
        batch_results = run_batch(
            batch, current_concurrency, config.command_timeout, capture_dir, retry_counts
        )

        # Process results
        failed_in_batch: list[tuple[CommandSpec, str]] = []  # (cmd, error_class)
        resource_contention_count = 0

        for cmd, returncode, duration, stdout, stderr in batch_results:
            # Verify the log
            log_info = verify_eval_log(cmd.log_dir)

            result = EvalResult(
                id=cmd.id,
                log_path=log_info.get("log_path"),
                status=log_info.get("status", "unknown"),
                samples_completed=log_info.get("samples_completed"),
                samples_total=log_info.get("samples_total"),
                duration_seconds=log_info.get("duration_seconds", duration),
                process_retries=retry_counts[cmd.id],
                batch=batch_num,
                model_usage=log_info.get("model_usage", {}),
            )

            if log_info.get("error"):
                result.errors.append(log_info["error"])

            # Determine success
            if returncode == 0 and log_info.get("status") == "success":
                completed_results[cmd.id] = result
                emit("result", result)
            else:
                # Both streams: Inspect reports prerequisite failures on stdout.
                diagnostic = combine_output(stdout, stderr)
                error_class = classify_error(returncode, diagnostic, log_info.get("status"))
                result.error_class = error_class

                if error_class == "resource_contention":
                    resource_contention_count += 1

                if diagnostic:
                    result.errors.append(f"[{error_class}] {diagnostic[:2000]}")
                else:
                    # A non-zero exit with no output at all leaves the
                    # orchestrator nothing to diagnose from, so say so
                    # explicitly rather than reporting an empty error list.
                    result.errors.append(
                        f"[{error_class}] Command exited {returncode} with no output "
                        "on stdout or stderr and produced no log."
                    )
                result.errors.append(f"exit_code={returncode}")

                if should_retry(error_class, retry_counts[cmd.id], config.max_retries):
                    # A failed attempt may have left a partial .eval behind.
                    # Move it out before retrying: a second log in the pair's
                    # directory would double the Analyst's pool.
                    _stash_failed_attempt_logs(cmd, retry_counts[cmd.id], capture_dir)
                    retry_counts[cmd.id] += 1
                    failed_in_batch.append((cmd, error_class))
                    result.status = "retrying"
                    emit("result", result)
                else:
                    # Exhausted retries or structural failure
                    result.process_retries = retry_counts[cmd.id]
                    completed_results[cmd.id] = result
                    emit("result", result)

        # Cascading failure detection
        backoff_multiplier = 1.0
        if len(batch) > 1 and len(failed_in_batch) > len(batch) / 2:
            error_classes = [ec for _, ec in failed_in_batch]
            if all(ec == "resource_contention" for ec in error_classes):
                new_concurrency = max(1, current_concurrency // 2)
                if new_concurrency < current_concurrency:
                    reduction = ConcurrencyReduction(
                        batch=batch_num,
                        previous_level=current_concurrency,
                        new_level=new_concurrency,
                        reason=f"Resource contention: {resource_contention_count}/{len(batch)} commands failed",
                    )
                    report.concurrency_reductions.append(reduction)
                    emit("reduction", reduction)
                    current_concurrency = new_concurrency
            elif all(ec == error_classes[0] for ec in error_classes) and error_classes[0] in ("structural", "routing"):
                # All the same unretryable error — systemic problem, abort
                # remaining. A uniform routing failure means the pinned provider
                # is down for every command, so continuing only wastes time.
                report.errors.append(
                    f"Batch {batch_num}: all failures are {error_classes[0]} — aborting remaining commands"
                )
                for remaining_cmd in pending:
                    aborted = EvalResult(
                        id=remaining_cmd.id,
                        status="aborted",
                        errors=[f"Aborted due to systemic {error_classes[0]} failure"],
                    )
                    completed_results[remaining_cmd.id] = aborted
                    emit("result", aborted)
                pending = []
                break
            elif all(ec == "transient" for ec in error_classes):
                # All same transient error — double the backoff for retries
                backoff_multiplier = 2.0
        elif resource_contention_count > 0 and current_concurrency > 1:
            # Even a single resource contention in a batch warrants reduction
            new_concurrency = max(1, current_concurrency // 2)
            if new_concurrency < current_concurrency:
                reduction = ConcurrencyReduction(
                    batch=batch_num,
                    previous_level=current_concurrency,
                    new_level=new_concurrency,
                    reason=f"Resource contention detected in batch {batch_num}",
                )
                report.concurrency_reductions.append(reduction)
                emit("reduction", reduction)
                current_concurrency = new_concurrency

        # Queue retries with backoff
        if failed_in_batch:
            max_backoff = max(
                compute_backoff(retry_counts[cmd.id] - 1, config.retry_backoff_seconds)
                for cmd, _ in failed_in_batch
            ) * backoff_multiplier
            if not _SHUTDOWN.is_set():
                _SHUTDOWN.wait(max_backoff)
            # Prepend retries so they run in the next iteration
            retry_commands = [cmd for cmd, _ in failed_in_batch]
            pending = retry_commands + pending

    # Handle shutdown
    if _SHUTDOWN.is_set():
        report.status = "interrupted"
        for remaining_cmd in pending:
            if remaining_cmd.id not in completed_results:
                interrupted = EvalResult(
                    id=remaining_cmd.id,
                    status="interrupted",
                    errors=["Execution interrupted by signal"],
                )
                completed_results[remaining_cmd.id] = interrupted
                emit("result", interrupted)

    # Assemble final results in original command order
    report.results = [
        completed_results.get(cmd.id, EvalResult(id=cmd.id, status="not_run"))
        for cmd in commands
    ]
    report.concurrency_used = config.max_parallel
    report.total_wall_clock_seconds = round(time.monotonic() - total_start, 1)

    return report


# ---------------------------------------------------------------------------
# Request → commands (the contract's request JSON)
# ---------------------------------------------------------------------------


def _sanitise(s: str) -> str:
    """Make a model string safe for use in a path or id."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")


def pair_id(condition: str, model: str) -> str:
    """Stable identifier for a condition-model pair."""
    return f"{condition}__{_sanitise(model)}"


def _render_task_arg(value: Any) -> str:
    """Render a `-T` value: strings verbatim, everything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value)


def _route_label(routing: dict[str, Any] | None) -> str:
    """Render the contract's route label for the log metadata and matrix."""
    if not routing or routing.get("route", "direct") == "direct":
        return "direct"
    slug = routing.get("provider_slug")
    return f"openrouter:{slug}" if slug else "openrouter:unpinned"


def validate_request(request: dict[str, Any]) -> list[str]:
    """Check a request against the contract's required fields and shapes.

    Parameters
    ----------
    request : dict
        Parsed request JSON.

    Returns
    -------
    list[str]
        Problems found; empty if the request is usable.
    """
    problems: list[str] = []
    for key in ("experiment_name", "experiment_dir", "conditions", "models"):
        if key not in request:
            problems.append(f"missing required field: {key}")
    if problems:
        return problems

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(request["experiment_name"])):
        problems.append("experiment_name must be filesystem-safe (snake_case)")
    exp_dir = str(request["experiment_dir"])
    if not os.path.isabs(exp_dir):
        problems.append("experiment_dir must be an absolute path")
    elif not os.path.isdir(exp_dir):
        problems.append(f"experiment_dir does not exist: {exp_dir}")

    conditions = request["conditions"]
    if not isinstance(conditions, dict) or not conditions:
        problems.append("conditions must be a non-empty mapping")
    else:
        for name, spec in conditions.items():
            if not re.fullmatch(r"[A-Za-z0-9_]+", str(name)):
                problems.append(f"condition name is not snake_case: {name!r}")
            task = spec.get("task") if isinstance(spec, dict) else spec
            if not isinstance(task, str) or not task:
                problems.append(f"condition {name!r} has no task path")
            elif os.path.isabs(task):
                problems.append(
                    f"condition {name!r}: task path must be relative to experiment_dir "
                    "(Inspect rejects absolute task paths)"
                )
            elif os.path.isdir(exp_dir) and not os.path.exists(os.path.join(exp_dir, task)):
                problems.append(f"condition {name!r}: task file not found: {os.path.join(exp_dir, task)}")
            if isinstance(spec, dict) and not isinstance(spec.get("args", {}), dict):
                problems.append(f"condition {name!r}: args must be a mapping")

    models = request["models"]
    if not isinstance(models, list) or not models or not all(isinstance(m, str) for m in models):
        problems.append("models must be a non-empty list of provider/model strings")

    routing = request.get("model_routing") or {}
    if not isinstance(routing, dict):
        problems.append("model_routing must be a mapping")
    else:
        for m, r in routing.items():
            if r.get("route", "direct") not in ("direct", "openrouter"):
                problems.append(f"model_routing[{m!r}].route must be direct|openrouter")
            if not isinstance(r.get("model_args", {}) or {}, dict):
                problems.append(f"model_routing[{m!r}].model_args must be a mapping")

    overrides = request.get("overrides") or {}
    if not isinstance(overrides, dict):
        problems.append("overrides must be a mapping")
    else:
        if "sample_id" in overrides and "sample_limit" in overrides:
            problems.append("overrides: sample_id and sample_limit cannot both be set (Inspect rejects the pair)")
        # Refused rather than ignored: it was documented for long enough that a
        # caller may still send it believing it repeats the run, and an ignored
        # key would leave them believing that.
        if "runs_per_condition" in overrides:
            problems.append(
                "overrides: runs_per_condition was removed (it was never applied); "
                "use epochs, with sample_id, for repeated sampling"
            )
        for k in ("sample_limit", "epochs", "max_parallel", "max_connections", "command_timeout_seconds"):
            if k in overrides and (not isinstance(overrides[k], int) or overrides[k] < 1):
                problems.append(f"overrides.{k} must be a positive integer")
    return problems


@dataclass
class PairPlan:
    """One condition-model pair, fully rendered."""

    id: str
    condition: str
    model: str
    route: str
    log_dir: str
    command: str
    preflight_command: str
    preflight_log_dir: str
    retest_command: str


def build_pairs(
    request: dict[str, Any],
    max_connections: int,
) -> list[PairPlan]:
    """Render every condition-model pair of a request into commands.

    Policy flags (the contract's "policy, not configuration") are applied to
    every invocation: ``--display plain``, per-condition ``--log-dir``,
    namespaced ``--tags``, ``--metadata condition/model/route``,
    ``--no-fail-on-error``, ``--retry-on-error 3``, ``--max-connections``.
    Routing ``model_args`` become ``-M key=<json>``; ``openrouter/`` models get
    the ``openai`` pin on the ``uv run`` prefix.

    Parameters
    ----------
    request : dict
        The contract's request JSON.
    max_connections : int
        The resolved ``--max-connections`` value.

    Returns
    -------
    list[PairPlan]
        One entry per pair, in ``conditions × models`` order. Example
        command::

            uv run inspect eval task.py --model openai/gpt-5 --display plain
              --log-dir /abs/exp/logs/c_0e4d --tags exp:x,cond:c_0e4d
              --metadata condition=c_0e4d --metadata model=openai/gpt-5
              --metadata route=direct --no-fail-on-error --retry-on-error 3
              --sample-id 1 --epochs 30 --max-connections 4
    """
    name = request["experiment_name"]
    exp_dir = os.path.abspath(request["experiment_dir"])
    models: list[str] = request["models"]
    routing: dict[str, Any] = request.get("model_routing") or {}
    overrides: dict[str, Any] = request.get("overrides") or {}
    multi_model = len(models) > 1

    pairs: list[PairPlan] = []
    for cond, spec in request["conditions"].items():
        task = spec["task"] if isinstance(spec, dict) else spec
        args = (spec.get("args") or {}) if isinstance(spec, dict) else {}
        for model in models:
            pid = pair_id(cond, model)
            r = routing.get(model) or {}
            route = _route_label(r)
            model_args = r.get("model_args") or {}

            # One log directory per condition; per pair when several models
            # share a condition, so the newest-log lookup is unambiguous.
            log_dir = os.path.join(exp_dir, "logs", cond)
            if multi_model:
                log_dir = os.path.join(log_dir, _sanitise(model))
            preflight_log_dir = os.path.join(exp_dir, "logs", PREFLIGHT_DIRNAME, pid)

            prefix = ["uv", "run"]
            if model.startswith("openrouter/"):
                prefix += ["--with", OPENROUTER_OPENAI_PIN]

            def render(log_directory: str, *, limit: int | None, sample_id: Any, epochs: int | None) -> str:
                parts = prefix + ["inspect", "eval", task, "--model", model, "--display", "plain"]
                parts += ["--log-dir", log_directory]
                parts += ["--tags", f"exp:{name},cond:{cond}"]
                parts += ["--metadata", f"condition={cond}", "--metadata", f"model={model}"]
                parts += ["--metadata", f"route={route}"]
                parts += ["--no-fail-on-error", "--retry-on-error", "3"]
                for k, v in args.items():
                    parts += ["-T", f"{k}={_render_task_arg(v)}"]
                if limit is not None:
                    parts += ["--limit", str(limit)]
                if sample_id is not None:
                    parts += ["--sample-id", str(sample_id)]
                if epochs is not None:
                    parts += ["--epochs", str(epochs)]
                parts += ["--max-connections", str(max_connections)]
                for k, v in model_args.items():
                    parts += ["-M", f"{k}={json.dumps(v) if not isinstance(v, str) else v}"]
                return " ".join(shlex.quote(p) for p in parts)

            sample_id = overrides.get("sample_id")
            full = render(
                log_dir,
                limit=overrides.get("sample_limit"),
                sample_id=sample_id,
                epochs=overrides.get("epochs"),
            )
            # Preflight probe: --limit 1, or — when the request pins sample ids
            # (Inspect rejects --sample-id with --limit) — the pinned invocation
            # without --epochs, which is itself a single rollout.
            if sample_id is not None:
                probe = render(preflight_log_dir, limit=None, sample_id=sample_id, epochs=None)
                retest = render(preflight_log_dir, limit=None, sample_id=sample_id, epochs=3)
            else:
                probe = render(preflight_log_dir, limit=1, sample_id=None, epochs=None)
                retest = render(preflight_log_dir, limit=3, sample_id=None, epochs=None)

            pairs.append(
                PairPlan(
                    id=pid,
                    condition=cond,
                    model=model,
                    route=route,
                    log_dir=log_dir,
                    command=full,
                    preflight_command=probe,
                    preflight_log_dir=preflight_log_dir,
                    retest_command=retest,
                )
            )
    return pairs


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host_resources() -> dict[str, Any]:
    """Snapshot the host for the Concurrency Decision fields."""
    free_mb: int | None = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    free_mb = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    return {"nproc": os.cpu_count(), "free_mem_mb": free_mb}


def _sandbox_hint(exp_dir: str, task: str) -> bool:
    """Guess whether the eval is sandboxed from compose/Docker files near the task."""
    candidates = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "Dockerfile")
    for base in {exp_dir, os.path.dirname(os.path.join(exp_dir, task))}:
        for c in candidates:
            if os.path.exists(os.path.join(base, c)):
                return True
    return False


class Status:
    """Score-free run status, persisted as JSON after every event.

    Parameters
    ----------
    path : str
        Path of ``status.json``.
    data : dict | None
        Existing contents (when loading) or None to start fresh.
    """

    def __init__(self, path: str, data: dict[str, Any] | None = None):
        self.path = path
        self.data: dict[str, Any] = data or {}
        self._lock = threading.Lock()

    @classmethod
    def load(cls, run_dir: str) -> "Status":
        path = os.path.join(run_dir, "status.json")
        with open(path) as f:
            return cls(path, json.load(f))

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = _now()
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2, default=str)
            os.replace(tmp, self.path)

    def pair(self, pid: str) -> dict[str, Any]:
        return self.data["pairs"][pid]


def init_status(
    run_dir: str,
    request: dict[str, Any],
    request_path: str,
    pairs: list[PairPlan],
    max_parallel: int,
    max_parallel_source: str,
    max_connections: int,
    max_connections_source: str,
    rationale: str | None,
    skip_preflight: bool,
    notes: list[str],
) -> Status:
    """Create the status file for a fresh run."""
    exp_dir = os.path.abspath(request["experiment_dir"])
    first_task = next(iter(request["conditions"].values()))
    first_task = first_task["task"] if isinstance(first_task, dict) else first_task
    data: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "run_id": os.path.basename(run_dir),
        "run_dir": run_dir,
        "pid": None,
        "experiment_name": request["experiment_name"],
        "experiment_dir": exp_dir,
        "parent_log_dir": os.path.join(exp_dir, "logs"),
        "request_path": request_path,
        "state": "launching",
        "started_at": _now(),
        "updated_at": None,
        "finished_at": None,
        "total_wall_clock_seconds": None,
        "concurrency": {
            "max_parallel": max_parallel,
            "max_parallel_source": max_parallel_source,
            "max_connections": max_connections,
            "max_connections_source": max_connections_source,
            "rationale": rationale,
            "reductions": [],
            "resources": _host_resources(),
            "sandbox_files_present": _sandbox_hint(exp_dir, first_task),
            "sandbox_detected": None,
            "sandbox_evidence": None,
        },
        "preflight": {"skipped": skip_preflight, "state": "skipped" if skip_preflight else "pending", "pairs": {}},
        "pair_order": [p.id for p in pairs],
        "pairs": {
            p.id: {
                "condition": p.condition,
                "model": p.model,
                "route": p.route,
                "log_dir": p.log_dir,
                "command": p.command,
                "state": "pending",
                "attempts": 0,
                "process_retries": 0,
                "log_path": None,
                "inspect_status": None,
                "samples_completed": None,
                "samples_total": None,
                "zero_samples": False,
                "duration_seconds": None,
                "error_class": None,
                "errors": [],
                "sample_errors": None,
                "sample_retries": None,
                "termination": None,
                "model_usage": {},
            }
            for p in pairs
        },
        "zero_sample_pairs": [],
        "notes": notes,
        "errors": [],
    }
    return Status(os.path.join(run_dir, "status.json"), data)


# ---------------------------------------------------------------------------
# The worker: preflight + full execution, updating status as it goes
# ---------------------------------------------------------------------------


def _quarantine_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "private")


def _apply_shape(entry: dict[str, Any], log_path: str | None) -> None:
    """Fill a pair's sample-shape fields from its log, if readable."""
    if not log_path:
        return
    try:
        shape = sample_shape(log_path)
    except Exception as e:  # unreadable log: record, never crash the run
        entry["errors"].append(f"sample summaries unreadable: {type(e).__name__}: {e}")
        return
    entry["sample_errors"] = shape["sample_errors"]
    entry["sample_retries"] = shape["sample_retries"]
    entry["termination"] = shape["termination"]


def _final_pair_state(result: EvalResult, entry: dict[str, Any]) -> str:
    """Map an engine result to the contract's matrix status vocabulary."""
    if result.status in ("aborted", "interrupted", "not_run", "retrying"):
        return result.status
    if result.status == "success":
        if entry.get("sample_errors"):
            return "sample-level-errors"
        return "success"
    return "failed"


def run_preflight(status: Status, pairs: list[PairPlan], config: ExecutionConfig, run_dir: str) -> list[PairPlan]:
    """Run the contract's preflight probe for every pair; return those that proceed.

    Outcomes per pair are recorded under ``status.data["preflight"]["pairs"]``
    with the contract's vocabulary. Preflight logs are quarantined under
    ``<run_dir>/private/preflight_logs/`` afterwards so they never appear in
    the log tree the Analyst receives.

    Parameters
    ----------
    status : Status
        The run's status file.
    pairs : list[PairPlan]
        Pairs to probe.
    config : ExecutionConfig
        Retry/timeout parameters (concurrency is forced to 1).
    run_dir : str
        The run directory.

    Returns
    -------
    list[PairPlan]
        Pairs that passed preflight, in the original order.
    """
    pf = status.data["preflight"]
    pf["state"] = "running"
    status.save()
    capture = os.path.join(_quarantine_dir(run_dir), "preflight_output")
    probe_config = ExecutionConfig(
        max_parallel=1,
        max_retries=config.max_retries,
        retry_backoff_seconds=config.retry_backoff_seconds,
        command_timeout=config.command_timeout,
    )
    surviving: list[PairPlan] = []
    durations: list[float] = []

    for p in pairs:
        if _SHUTDOWN.is_set():
            break
        status.pair(p.id)["state"] = "preflight"
        status.save()
        shutil.rmtree(p.preflight_log_dir, ignore_errors=True)
        cmd = CommandSpec(id=f"preflight_{p.id}", command=p.preflight_command, log_dir=p.preflight_log_dir, cwd=status.data["experiment_dir"])
        report = execute([cmd], probe_config, capture_dir=capture)
        r = report.results[0]
        entry: dict[str, Any] = {
            "outcome": None,
            "process_retries": r.process_retries,
            "duration_seconds": r.duration_seconds,
            "retest": None,
            "evidence": None,
        }
        if r.duration_seconds:
            durations.append(float(r.duration_seconds))

        if r.status == "success":
            # Sample error on the single probe → --limit 3 retest.
            shape = None
            try:
                shape = sample_shape(r.log_path) if r.log_path else None
            except Exception as e:
                entry["evidence"] = {"note": f"probe log unreadable: {e}"}
            if shape and shape["sample_errors"] > 0:
                shutil.rmtree(p.preflight_log_dir, ignore_errors=True)
                retest_cmd = CommandSpec(id=f"retest_{p.id}", command=p.retest_command, log_dir=p.preflight_log_dir, cwd=status.data["experiment_dir"])
                rr = execute([retest_cmd], probe_config, capture_dir=capture).results[0]
                try:
                    rshape = sample_shape(rr.log_path) if rr.log_path else {"samples": 0, "sample_errors": 0}
                except Exception:
                    rshape = {"samples": 0, "sample_errors": 0}
                n, errs = rshape["samples"], rshape["sample_errors"]
                entry["retest"] = {"samples": n, "sample_errors": errs, "process_retries": rr.process_retries}
                if n and errs >= n:
                    entry["outcome"] = "excluded-all-samples-deterministic"
                    entry["evidence"] = {"retest_errors": f"{errs} of {n} samples errored in the --limit 3 retest"}
                elif errs == 0:
                    entry["outcome"] = "sample-transient"
                else:
                    entry["outcome"] = "sample-deterministic"
            else:
                entry["outcome"] = "passed-after-retries" if r.process_retries else "passed"
        else:
            cls = r.error_class or "structural"
            entry["outcome"] = "excluded-routing" if cls == "routing" else "excluded-structural"
            entry["evidence"] = {
                "error_class": cls,
                "exit_code": next((e.split("=", 1)[1] for e in r.errors if e.startswith("exit_code=")), None),
                "command": p.preflight_command,
                "output_tail": next((e for e in r.errors if e.startswith("[")), "") ,
            }

        pf["pairs"][p.id] = entry
        if entry["outcome"].startswith("excluded"):
            status.pair(p.id)["state"] = "excluded-at-preflight"
            status.pair(p.id)["error_class"] = entry["evidence"].get("error_class") if entry["evidence"] else None
        else:
            status.pair(p.id)["state"] = "pending"
            surviving.append(p)
        status.save()

    # Sandbox detection from probe timing (10–30 s setup vs 1–3 s API-only).
    # Timing alone is not decisive: a slow reasoning model makes an API-only
    # probe take minutes (deepseek-r1 took 133 s with no sandbox), so timing
    # only *confirms* a sandbox when compose/Docker files are present.
    conc = status.data["concurrency"]
    if durations:
        slowest = max(durations)
        files = bool(conc.get("sandbox_files_present"))
        conc["sandbox_detected"] = files
        conc["sandbox_evidence"] = (
            f"sandbox files present: {files}; slowest preflight probe {slowest:.0f}s"
            + ("" if files or slowest < 10.0 else " (slow probe without sandbox files — likely a slow model, not a sandbox)")
        )

    # Quarantine the preflight tree: it must not appear in the Analyst's logs.
    pf_root = os.path.join(status.data["parent_log_dir"], PREFLIGHT_DIRNAME)
    if os.path.isdir(pf_root):
        dest = os.path.join(_quarantine_dir(run_dir), "preflight_logs")
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(pf_root, dest)
    pf["state"] = "done"
    status.save()
    return surviving


def run_worker(run_dir: str) -> int:
    """Execute a prepared run to completion, keeping the status file current.

    Parameters
    ----------
    run_dir : str
        A run directory created by ``prepare_run``.

    Returns
    -------
    int
        Process exit code: 0 completed, 2 interrupted, 1 failed to start.
    """
    status = Status.load(run_dir)
    status.data["pid"] = os.getpid()
    with open(os.path.join(run_dir, "plan.json")) as f:
        plan = json.load(f)
    pairs = [PairPlan(**p) for p in plan["pairs"]]
    config = ExecutionConfig(**plan["execution"])
    exp_dir = status.data["experiment_dir"]
    start = time.monotonic()

    try:
        # --- Preflight -----------------------------------------------------
        if status.data["preflight"]["skipped"]:
            status.data["state"] = "running"
            status.save()
            to_run = pairs
        else:
            status.data["state"] = "preflight"
            status.save()
            to_run = run_preflight(status, pairs, config, run_dir)

        # --- Full execution ------------------------------------------------
        status.data["state"] = "running"
        status.save()
        if status.data["concurrency"].get("sandbox_detected") is None:
            files = bool(status.data["concurrency"].get("sandbox_files_present"))
            status.data["concurrency"]["sandbox_detected"] = files
            status.data["concurrency"]["sandbox_evidence"] = f"sandbox files present: {files} (preflight skipped)"

        by_id = {p.id: p for p in to_run}
        commands = [CommandSpec(id=p.id, command=p.command, log_dir=p.log_dir, cwd=exp_dir) for p in to_run]
        for p in to_run:
            Path(p.log_dir).mkdir(parents=True, exist_ok=True)

        def on_event(kind: str, payload: Any) -> None:
            if kind == "batch_start":
                for pid in payload:
                    e = status.pair(pid)
                    e["state"] = "running"
                    e["attempts"] += 1
            elif kind == "result":
                r: EvalResult = payload
                e = status.pair(r.id)
                e["log_path"] = r.log_path
                e["inspect_status"] = r.status if r.status not in ("retrying",) else e.get("inspect_status")
                e["samples_completed"] = r.samples_completed
                e["samples_total"] = r.samples_total
                e["duration_seconds"] = r.duration_seconds
                e["process_retries"] = r.process_retries
                e["error_class"] = r.error_class
                e["errors"] = list(r.errors)
                e["model_usage"] = r.model_usage
                if r.status == "retrying":
                    e["state"] = "retrying"
                else:
                    _apply_shape(e, r.log_path)
                    e["state"] = _final_pair_state(r, e)
                    # Zero-sample means "a final log exists and it completed
                    # nothing" — a distinct diagnostic from a failure with no
                    # log at all, which the Error Summary already covers.
                    e["zero_samples"] = bool(r.log_path) and not (r.samples_completed or 0)
                    if e["zero_samples"] and r.id not in status.data["zero_sample_pairs"]:
                        status.data["zero_sample_pairs"].append(r.id)
            elif kind == "reduction":
                status.data["concurrency"]["reductions"].append(asdict(payload))
            status.save()

        report = execute(commands, config, capture_dir=os.path.join(_quarantine_dir(run_dir), "eval_output"), on_event=on_event)
        status.data["errors"].extend(report.errors)
        status.data["state"] = "interrupted" if report.status == "interrupted" else "completed"
        # Anything still pending (e.g. excluded before launch) keeps its state.
        _ = by_id
    except Exception as e:  # never leave a run without a terminal state
        status.data["errors"].append(f"runner exception: {type(e).__name__}: {e}")
        status.data["state"] = "failed"
    finally:
        status.data["finished_at"] = _now()
        status.data["total_wall_clock_seconds"] = round(time.monotonic() - start, 1)
        status.save()

    return {"completed": 0, "interrupted": 2}.get(status.data["state"], 1)


# ---------------------------------------------------------------------------
# run: prepare + launch
# ---------------------------------------------------------------------------


def _resolve_int(cli: int | None, overrides: dict[str, Any], key: str, default: int) -> tuple[int, str]:
    if cli is not None:
        return cli, "cli override"
    if key in overrides:
        return int(overrides[key]), "request overrides"
    return default, "runner default"


def prepare_run(
    request_path: str,
    max_parallel: int | None = None,
    max_connections: int | None = None,
    rationale: str | None = None,
    only: list[str] | None = None,
    quarantine_existing_logs: bool = False,
) -> tuple[str, list[str]]:
    """Validate a request, render its commands and create the run directory.

    Parameters
    ----------
    request_path : str
        Path to the contract's request JSON.
    max_parallel, max_connections : int | None
        CLI overrides of the concurrency ceiling (rationale required).
    rationale : str | None
        Recorded verbatim in the Concurrency Decision when the CLI raises the
        ceiling.
    only : list[str] | None
        Restrict the run to these pair ids (a re-run affordance).
    quarantine_existing_logs : bool
        Move stale ``.eval`` files out of a pair's log directory instead of
        refusing to run into it.

    Returns
    -------
    tuple[str, list[str]]
        ``(run_dir, problems)``; ``run_dir`` is empty when problems is not.
    """
    problems: list[str] = []
    request_path = os.path.abspath(request_path)
    try:
        with open(request_path) as f:
            request = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return "", [f"cannot read request: {e}"]

    problems = validate_request(request)
    if problems:
        return "", problems

    overrides = request.get("overrides") or {}
    notes: list[str] = []
    unrecognised = sorted(k for k in overrides if k not in RECOGNISED_OVERRIDES)
    if unrecognised:
        notes.append(f"Unrecognised override keys ignored: {', '.join(unrecognised)}.")
    if (max_parallel is not None or max_connections is not None) and not rationale:
        return "", ["--max-parallel / --max-connections require --rationale (recorded in the Concurrency Decision)"]

    mp, mp_src = _resolve_int(max_parallel, overrides, "max_parallel", DEFAULT_MAX_PARALLEL)
    mc, mc_src = _resolve_int(max_connections, overrides, "max_connections", DEFAULT_MAX_CONNECTIONS)
    pairs = build_pairs(request, mc)
    if only:
        wanted = set(only)
        unknown = wanted - {p.id for p in pairs}
        if unknown:
            return "", [f"--only names unknown pair ids: {', '.join(sorted(unknown))}; known: {', '.join(p.id for p in pairs)}"]
        pairs = [p for p in pairs if p.id in wanted]
        notes.append(f"Run restricted with --only to: {', '.join(p.id for p in pairs)}.")

    # Run directory under the experiment's artefacts.
    exp_dir = os.path.abspath(request["experiment_dir"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(exp_dir, "artefacts", "executor", "runs", f"{request['experiment_name']}_{stamp}")
    n = 1
    base = run_dir
    while os.path.exists(run_dir):
        n += 1
        run_dir = f"{base}_{n}"
    os.makedirs(os.path.join(run_dir, "private"), exist_ok=True)

    # Stale logs: a second .eval in a pair's directory silently doubles the
    # Analyst's pool, so refuse unless told to quarantine.
    for p in pairs:
        stale = sorted(Path(p.log_dir).glob("*.eval")) if os.path.isdir(p.log_dir) else []
        if not stale:
            continue
        if not quarantine_existing_logs:
            problems.append(
                f"{p.log_dir} already holds {len(stale)} .eval file(s); a re-run would append to the "
                "Analyst's pool. Pass --quarantine-existing-logs to move them aside."
            )
            continue
        dest = os.path.join(exp_dir, "logs_quarantined", os.path.basename(run_dir), p.condition)
        os.makedirs(dest, exist_ok=True)
        for s in stale:
            shutil.move(str(s), os.path.join(dest, s.name))
        notes.append(f"Quarantined {len(stale)} stale .eval file(s) from {p.log_dir} to {dest}.")
    if problems:
        shutil.rmtree(run_dir, ignore_errors=True)
        return "", problems

    config = ExecutionConfig(
        max_parallel=mp,
        max_retries=3,
        retry_backoff_seconds=[10, 30, 60],
        command_timeout=int(overrides.get("command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT)),
    )
    shutil.copy(request_path, os.path.join(run_dir, "request.json"))
    with open(os.path.join(run_dir, "plan.json"), "w") as f:
        json.dump({"pairs": [asdict(p) for p in pairs], "execution": asdict(config)}, f, indent=2)
    status = init_status(
        run_dir, request, request_path, pairs, mp, mp_src, mc, mc_src, rationale,
        bool(overrides.get("skip_preflight", False)), notes,
    )
    status.save()
    return run_dir, []


def launch_detached(run_dir: str) -> int:
    """Start the worker in its own session and return its pid.

    The child is spawned with ``start_new_session`` (the ``setsid`` semantics),
    stdin closed and stdout/stderr redirected to ``<run_dir>/worker.log``, so
    it survives the caller's session ending — a sub-agent's Bash timeout, a
    harness teardown, or the terminal closing.

    Parameters
    ----------
    run_dir : str
        A prepared run directory.

    Returns
    -------
    int
        The worker's pid.
    """
    # Use this script's *unresolved* path: in an installed target it is a
    # symlink into the scaffold repo, and the worker must be found at the same
    # place the caller used.
    script = os.path.abspath(__file__)
    log = open(os.path.join(run_dir, "worker.log"), "ab")
    proc = subprocess.Popen(
        [sys.executable, script, "_worker", run_dir],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=os.getcwd(),
        close_fds=True,
    )
    log.close()
    # Record the pid in a file the worker never writes: touching status.json
    # here would race the worker's own first save.
    with open(os.path.join(run_dir, "worker.pid"), "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def _worker_pid(data: dict[str, Any]) -> int | None:
    """The worker's pid: from the status file once it has saved, else worker.pid."""
    if data.get("pid"):
        return int(data["pid"])
    try:
        with open(os.path.join(data.get("run_dir", ""), "worker.pid")) as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


def _worker_dead(data: dict[str, Any]) -> bool:
    """True when the run is not terminal and its worker process is gone."""
    if data.get("state") in TERMINAL_STATES:
        return False
    pid = _worker_pid(data)
    return pid is not None and not _pid_alive(pid)


def _pid_alive(pid: int | None) -> bool:
    """True if `pid` exists and is not a zombie.

    `os.kill(pid, 0)` succeeds on a zombie (a dead child nobody has reaped),
    so the process state is read from /proc where available.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line.split()[1]
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# status / wait rendering
# ---------------------------------------------------------------------------


def render_status(data: dict[str, Any]) -> str:
    """Render a compact, score-free status summary.

    Parameters
    ----------
    data : dict
        Contents of ``status.json``.

    Returns
    -------
    str
        Multi-line text. Example::

            run: iter_00_20260917T120000Z  state: running  pid: 4242 (alive)
            concurrency: max_parallel=1 (runner default) max_connections=4 (runner default)
            preflight: done — 2 passed, 0 excluded
            pairs:
              c_0e4d / openai/gpt-5   running   samples -/-   attempts 1
            zero-sample pairs: none
    """
    pid = _worker_pid(data)
    alive = _pid_alive(pid)
    state = data.get("state")
    if _worker_dead(data):
        state = f"{state} — WORKER DIED (pid {pid} not alive, no terminal state written; check worker.log)"
    lines = [f"run: {data.get('run_id')}  state: {state}  pid: {pid} ({'alive' if alive else 'not running'})"]
    lines.append(f"started: {data.get('started_at')}  updated: {data.get('updated_at')}  finished: {data.get('finished_at')}")
    c = data.get("concurrency", {})
    lines.append(
        f"concurrency: max_parallel={c.get('max_parallel')} ({c.get('max_parallel_source')}) "
        f"max_connections={c.get('max_connections')} ({c.get('max_connections_source')})"
    )
    pf = data.get("preflight", {})
    if pf.get("skipped"):
        lines.append("preflight: skipped (skip_preflight=true)")
    else:
        outcomes = [e.get("outcome") for e in pf.get("pairs", {}).values()]
        excluded = sum(1 for o in outcomes if o and o.startswith("excluded"))
        lines.append(f"preflight: {pf.get('state')} — {len(outcomes) - excluded} passed, {excluded} excluded")
    lines.append("pairs:")
    for pid in data.get("pair_order", []):
        e = data["pairs"][pid]
        done = e.get("samples_completed")
        total = e.get("samples_total")
        samples = f"{done if done is not None else '-'}/{total if total is not None else '-'}"
        extra = f"  [{e['error_class']}]" if e.get("error_class") else ""
        lines.append(f"  {e['condition']} / {e['model']}   {e['state']}   samples {samples}   attempts {e.get('attempts', 0)}{extra}")
    zs = data.get("zero_sample_pairs") or []
    if zs:
        pretty = ", ".join(f"{data['pairs'][p]['condition']} / {data['pairs'][p]['model']}" for p in zs)
        lines.append(f"ZERO-SAMPLE PAIRS (completed 0 samples — distinct from a zero rate): {pretty}")
    else:
        lines.append("zero-sample pairs: none")
    if data.get("errors"):
        lines.append("run errors:")
        lines.extend(f"  - {e}" for e in data["errors"])
    lines.append(f"status file: {os.path.join(data.get('run_dir', ''), 'status.json')}")
    return "\n".join(lines)


def wait_for(run_dir: str, timeout: float, interval: float = 15.0) -> dict[str, Any]:
    """Block until the run is terminal or the timeout elapses; return status."""
    deadline = time.monotonic() + timeout
    while True:
        data = Status.load(run_dir).data
        if data.get("state") in TERMINAL_STATES:
            return data
        if _worker_dead(data):
            # Worker died without writing a terminal state: report it as
            # terminal so a caller polling `wait` is never stranded.
            data["state"] = "failed"
            data.setdefault("errors", []).append(
                f"worker pid {_worker_pid(data)} died before writing a terminal state (see worker.log)"
            )
            return data
        if time.monotonic() >= deadline or _SHUTDOWN.is_set():
            return data
        _SHUTDOWN.wait(min(interval, max(0.0, deadline - time.monotonic())))


# ---------------------------------------------------------------------------
# Draft report in the contract's section shape
# ---------------------------------------------------------------------------


def _pair_label(e: dict[str, Any]) -> str:
    return f"`{e['condition']} / {e['model']}`"


def render_report(data: dict[str, Any]) -> str:
    """Assemble the draft Executor report from a status file.

    Every section heading of ``executor_interface_contract.md §Report Format``
    is emitted verbatim, in order, with ``None.`` / ``No failures.`` for empty
    sections. No scorer output can appear: the status file holds none.

    Parameters
    ----------
    data : dict
        Contents of ``status.json``.

    Returns
    -------
    str
        The markdown report, starting at ``### Summary``.
    """
    pairs = [data["pairs"][pid] for pid in data["pair_order"]]
    pf = data.get("preflight", {})
    c = data.get("concurrency", {})
    zero = [data["pairs"][p] for p in data.get("zero_sample_pairs", [])]

    excluded = [e for e in pairs if e["state"] == "excluded-at-preflight"]
    ran = [e for e in pairs if e["state"] not in ("excluded-at-preflight",)]
    success = [e for e in ran if e["state"] == "success"]
    sample_err = [e for e in ran if e["state"] == "sample-level-errors"]
    failed = [e for e in ran if e["state"] == "failed"]
    other = [e for e in ran if e["state"] in ("aborted", "interrupted", "not_run", "pending", "running", "retrying", "preflight")]
    structural = [e for e in failed if e.get("error_class") in ("structural", "unknown", "resource_contention", "transient", None)]
    routing = [e for e in failed if e.get("error_class") == "routing"]
    transient_recovered = [e for e in ran if e["state"] in ("success", "sample-level-errors") and e.get("process_retries", 0) > 0]

    out: list[str] = []

    # --- Summary -----------------------------------------------------------
    out.append("### Summary")
    out.append("")
    s = (
        f"{len(pairs)} condition-model pair(s) were submitted: {len(success)} completed successfully, "
        f"{len(sample_err)} completed with sample-level errors, {len(excluded)} excluded at preflight, "
        f"{len(failed)} failed during full execution"
        + (f", {len(other)} did not reach a final state ({', '.join(sorted({e['state'] for e in other}))})" if other else "")
        + f". Full execution ran at max_parallel={c.get('max_parallel')} ({c.get('max_parallel_source')}) "
        f"with max_connections={c.get('max_connections')} ({c.get('max_connections_source')}); run state: {data.get('state')}."
    )
    out.append(s)
    # Decision-critical observation heuristics: asymmetry by condition, retries.
    observations: list[str] = []
    by_cond: dict[str, list[dict[str, Any]]] = {}
    for e in pairs:
        by_cond.setdefault(e["condition"], []).append(e)
    bad_conds = {k for k, v in by_cond.items() if all(x["state"] in ("excluded-at-preflight", "failed") for x in v)}
    good_conds = {k for k, v in by_cond.items() if any(x["state"] in ("success", "sample-level-errors") for x in v)}
    if bad_conds and good_conds:
        observations.append(
            f"Asymmetric outcome across conditions: {', '.join(sorted(bad_conds))} produced no usable run while "
            f"{', '.join(sorted(good_conds))} did — treat as a possible condition-specific setup break, not as data."
        )
    if sample_err:
        observations.append(
            "Sample-level attrition: " + "; ".join(f"{e['condition']} / {e['model']} {e['sample_errors']} of {e['samples_completed']}" for e in sample_err) + "."
        )
    if transient_recovered:
        observations.append("Process-level retries were needed for: " + ", ".join(f"{e['condition']} / {e['model']} ({e['process_retries']})" for e in transient_recovered) + ".")
    if c.get("reductions"):
        observations.append(f"Concurrency was reduced {len(c['reductions'])} time(s) during execution.")
    if not observations:
        observations.append("No asymmetric exclusions, attrition, or retry activity was observed.")
    out.append(" ".join(observations))
    out.append("")
    if zero:
        out.append(
            "**Zero-sample pairs (completed 0 samples; an execution outcome, distinct from a zero behaviour rate — "
            "check `sample_id` against the task's actual sample ids and any `-T` argument that changes them):** "
            + ", ".join(_pair_label(e) for e in zero) + "."
        )
    else:
        out.append("**Zero-sample pairs:** none.")
    out.append("")

    # --- Parent Log Directory --------------------------------------------
    out.append("### Parent Log Directory")
    out.append("")
    out.append(f"`{data.get('parent_log_dir')}`")
    out.append("")

    # --- Preflight Exclusions --------------------------------------------
    out.append("### Preflight Exclusions")
    out.append("")
    if pf.get("skipped"):
        out.append("Preflight skipped (skip_preflight=true).")
    elif not excluded:
        out.append("None.")
    else:
        for e in excluded:
            entry = pf.get("pairs", {}).get(next(pid for pid in data["pair_order"] if data["pairs"][pid] is e), {})
            ev = entry.get("evidence") or {}
            out.append(f"- **{_pair_label(e)}**")
            outcome = entry.get("outcome", "")
            if outcome == "excluded-all-samples-deterministic":
                out.append("  - **Reason**: `all-samples-deterministic-sample-error`")
                out.append(f"  - **Evidence**: {ev.get('retest_errors')}; sample error traces are in the quarantined preflight logs under the run directory's `private/preflight_logs/`.")
            else:
                reason = "pinned-provider-unavailable (routing)" if outcome == "excluded-routing" else "structural"
                out.append(f"  - **Reason**: `{reason}`")
                out.append(f"  - **Final exit code**: {ev.get('exit_code')}")
                out.append(f"  - **Command**: `{ev.get('command')}`")
                out.append("  - **Evidence** (combined stderr+stdout tail, scorer lines scrubbed):")
                out.append("    ```")
                out.extend("    " + line for line in (ev.get("output_tail") or "(no output)").splitlines())
                out.append("    ```")
    out.append("")

    # --- Execution Matrix --------------------------------------------------
    out.append("### Condition-Model Execution Matrix")
    out.append("")
    out.append("| Condition | Model | Route | Log Path | Status | Samples (done/total) | Process Retries | Sample-Level Retries | Preflight Retries | Duration |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for pid in data["pair_order"]:
        e = data["pairs"][pid]
        if e["state"] == "excluded-at-preflight":
            out.append(f"| {e['condition']} | {e['model']} | {e['route']} | — | excluded-at-preflight | — | — | — | — | — |")
            continue
        pfe = pf.get("pairs", {}).get(pid, {})
        pre_retries = 1 if pfe.get("retest") else 0
        done = e.get("samples_completed")
        total = e.get("samples_total")
        samples = f"{done if done is not None else '-'}/{total if total is not None else '-'}"
        dur = f"{e['duration_seconds']:.0f}s" if e.get("duration_seconds") is not None else "—"
        sr = e.get("sample_retries")
        out.append(
            f"| {e['condition']} | {e['model']} | {e['route']} | {e.get('log_path') or '—'} | {e['state']} | {samples} | "
            f"{e.get('process_retries', 0)} | {sr if sr is not None else '—'} | {pre_retries} | {dur} |"
        )
    out.append("")
    out.append("Rows marked `excluded-at-preflight` are detailed in Preflight Exclusions above.")
    out.append("")

    # --- Error Summary -----------------------------------------------------
    out.append("### Error Summary")
    out.append("")
    out.append(f"#### Structural ({len(structural)} condition-model pair(s))")
    out.append("")
    if not structural:
        out.append("None.")
    for e in structural:
        exit_code = next((x.split("=", 1)[1] for x in e.get("errors", []) if x.startswith("exit_code=")), "—")
        tail = next((x for x in e.get("errors", []) if x.startswith("[")), "(no output captured)")
        out.append(f"- **{_pair_label(e)}**")
        out.append(f"  - **Final exit code**: {exit_code}")
        out.append(f"  - **Command**: `{e['command']}`")
        out.append(f"  - **Stderr (last ~50 lines)** (combined stderr+stdout, scorer lines scrubbed; class tag `{e.get('error_class')}`):")
        out.append("    ```")
        out.extend("    " + line for line in tail.splitlines())
        out.append("    ```")
    out.append("")
    out.append(f"#### Sample-level ({len(sample_err)} condition-model pair(s))")
    out.append("")
    if not sample_err:
        out.append("None.")
    for e in sample_err:
        out.append(f"- **{_pair_label(e)}**: {e.get('sample_errors')} of {e.get('samples_completed')} samples errored.")
    out.append("")
    out.append(f"#### Transient, recovered ({len(transient_recovered)} condition-model pair(s))")
    out.append("")
    if not transient_recovered:
        out.append("None.")
    for e in transient_recovered:
        out.append(f"- **{_pair_label(e)}**: {e.get('process_retries')} process retries required; final attempt succeeded.")
    out.append("")
    out.append(f"#### Pinned provider unavailable ({len(routing)} condition-model pair(s))")
    out.append("")
    if not routing:
        out.append("None.")
    for e in routing:
        tail = next((x for x in e.get("errors", []) if x.startswith("[routing]")), "")
        out.append(f"- **{_pair_label(e)}**: route `{e['route']}`; {e.get('process_retries', 0)} process retries attempted. OpenRouter error:")
        out.append("    ```")
        out.extend("    " + line for line in (tail or "(no output captured)").splitlines()[-10:])
        out.append("    ```")
    out.append("")

    # --- Transcript Termination Metadata ----------------------------------
    out.append("### Transcript Termination Metadata")
    out.append("")
    out.append("| Condition | Model | No-Assistant-Msg | Limit-Hit | Error-Terminated |")
    out.append("|---|---|---|---|---|")
    any_rows = False
    for e in ran:
        t = e.get("termination")
        if not t:
            continue
        any_rows = True
        out.append(f"| {e['condition']} | {e['model']} | {t['no_assistant_msg']} | {t['limit_hit']} | {t['error_terminated']} |")
    if not any_rows:
        out.append("| — | — | — | — | — |")
        out.append("")
        out.append("None. (No pair produced a readable log.)")
    out.append("")

    # --- Concurrency Decision ----------------------------------------------
    out.append("### Concurrency Decision")
    out.append("")
    override = "no"
    if c.get("max_parallel_source") == "request overrides":
        override = f"yes ({c.get('max_parallel')})"
    res = c.get("resources") or {}
    out.append(f"- **Chosen `max_parallel`**: {c.get('max_parallel')}")
    out.append(f"- **Orchestrator override provided**: {override}")
    out.append(f"- **Resources observed**: nproc={res.get('nproc')}, free_mem_mb={res.get('free_mem_mb')}")
    sd = c.get("sandbox_detected")
    out.append(f"- **Sandbox detected**: {'yes' if sd else 'no' if sd is False else 'unknown'} (evidence: {c.get('sandbox_evidence') or 'none recorded'})")
    out.append(f"- **Concurrency preflight test run**: {'not applicable (only 1 pair)' if len(pairs) == 1 else 'no'}")
    if c.get("reductions"):
        out.append("- **Reductions applied during execution**: " + "; ".join(f"batch {r['batch']}: {r['previous_level']}→{r['new_level']} ({r['reason']})" for r in c["reductions"]))
    else:
        out.append("- **Reductions applied during execution**: none")
    if c.get("max_parallel_source") == "cli override":
        out.append(f"- **Rationale**: Executor raised the runner ceiling via CLI override — {c.get('rationale')}")
    elif c.get("max_parallel_source") == "request overrides":
        out.append(f"- **Rationale**: the request's `overrides.max_parallel` was applied as given; the runner's ceiling (max_parallel={DEFAULT_MAX_PARALLEL}, max_connections={DEFAULT_MAX_CONNECTIONS}) is the default only.")
    else:
        out.append(f"- **Rationale**: runner default ceiling for a shared host with one sandbox per sample (max_parallel={DEFAULT_MAX_PARALLEL}, max_connections={DEFAULT_MAX_CONNECTIONS}); no override was requested.")
    out.append("")

    # --- Execution Summary -------------------------------------------------
    out.append("### Execution Summary")
    out.append("")
    out.append(f"- **Total attempted**: {len(pairs)}")
    out.append(f"- **Completed successfully**: {len(success)}")
    out.append(f"- **Completed with sample errors**: {len(sample_err)}")
    out.append(f"- **Excluded at preflight**: {len(excluded)}")
    out.append(f"- **Failed during full execution**: {len(failed)}")
    if other:
        out.append(f"- **Did not reach a final state**: {len(other)}")
    out.append(f"- **Zero samples completed**: {len(zero)}")
    out.append("")

    # --- Additional Notes --------------------------------------------------
    out.append("### Additional Notes")
    out.append("")
    notes = list(data.get("notes") or [])
    notes.extend(data.get("errors") or [])
    notes.append(f"Run directory: `{data.get('run_dir')}` (status file, plan, worker log; raw eval output is quarantined under `private/` and is score-bearing — do not read it).")
    for n in notes:
        out.append(f"- {n}")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(description="Prism eval batch runner (score-free).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="validate a request, build commands, launch detached")
    p_run.add_argument("request")
    p_run.add_argument("--foreground", action="store_true", help="run the batch inline instead of detaching")
    p_run.add_argument("--max-parallel", type=int, default=None)
    p_run.add_argument("--max-connections", type=int, default=None)
    p_run.add_argument("--rationale", default=None, help="required with --max-parallel/--max-connections")
    p_run.add_argument("--only", default=None, help="comma-separated pair ids to (re-)run")
    p_run.add_argument("--quarantine-existing-logs", action="store_true")

    p_status = sub.add_parser("status", help="print the run's score-free status")
    p_status.add_argument("run_dir")
    p_status.add_argument("--json", action="store_true")

    p_wait = sub.add_parser("wait", help="block until the run is terminal or --timeout elapses")
    p_wait.add_argument("run_dir")
    p_wait.add_argument("--timeout", type=float, default=540.0, help="seconds (default 540, under a 10-minute tool cap)")
    p_wait.add_argument("--interval", type=float, default=15.0)
    p_wait.add_argument("--json", action="store_true")

    p_report = sub.add_parser("report", help="render the draft report from the status file")
    p_report.add_argument("run_dir")

    p_worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    p_worker.add_argument("run_dir")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
        run_dir, problems = prepare_run(
            args.request,
            max_parallel=args.max_parallel,
            max_connections=args.max_connections,
            rationale=args.rationale,
            only=only,
            quarantine_existing_logs=args.quarantine_existing_logs,
        )
        if problems:
            print(json.dumps({"status": "error", "errors": problems}, indent=2))
            return 1
        if args.foreground:
            code = run_worker(run_dir)
            print(render_status(Status.load(run_dir).data))
            return code
        pid = launch_detached(run_dir)
        receipt = {
            "status": "launched",
            "run_dir": run_dir,
            "pid": pid,
            "status_file": os.path.join(run_dir, "status.json"),
            "worker_log": os.path.join(run_dir, "worker.log"),
            "pairs": Status.load(run_dir).data["pair_order"],
            "next": f"uv run python scripts/execute_evals.py wait {shlex.quote(run_dir)} --timeout 540",
        }
        print(json.dumps(receipt, indent=2))
        return 0

    if args.cmd == "status":
        data = Status.load(args.run_dir).data
        print(json.dumps(data, indent=2) if args.json else render_status(data))
        return 0 if data.get("state") in TERMINAL_STATES else 3

    if args.cmd == "wait":
        data = wait_for(args.run_dir, args.timeout, args.interval)
        print(json.dumps(data, indent=2) if args.json else render_status(data))
        return 0 if data.get("state") in TERMINAL_STATES else 3

    if args.cmd == "report":
        data = Status.load(args.run_dir).data
        text = render_report(data)
        with open(os.path.join(args.run_dir, "draft_report.md"), "w") as f:
            f.write(text)
        print(text)
        return 0

    if args.cmd == "_worker":
        return run_worker(args.run_dir)

    return 2


if __name__ == "__main__":
    sys.exit(main())
