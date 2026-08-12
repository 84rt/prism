"""Eval execution engine for the SoE agent scaffold.

A pure execution engine that runs Inspect AI evaluations with configurable
concurrency, error recovery, and structured reporting. The Executor sub-agent
constructs eval commands and decides execution strategy; this script handles
process management.

Usage:
    uv run python scripts/execute_evals.py input.json

Input: JSON file specifying commands and execution parameters.
Output: Structured JSON report to stdout.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
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
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CommandSpec:
    id: str
    command: str
    log_dir: str


@dataclass
class ExecutionConfig:
    max_parallel: int = 1
    max_retries: int = 3
    retry_backoff_seconds: list[int] = field(default_factory=lambda: [10, 30, 60])


@dataclass
class EvalResult:
    id: str
    log_path: str | None = None
    status: str = "not_run"
    samples_completed: int | None = None
    samples_total: int | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    duration_seconds: float | None = None
    process_retries: int = 0
    batch: int = 0
    errors: list[str] = field(default_factory=list)


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
# Input parsing & validation
# ---------------------------------------------------------------------------


def parse_input(path: str) -> tuple[list[CommandSpec], ExecutionConfig]:
    """Load and validate input JSON."""
    with open(path) as f:
        data = json.load(f)

    if "commands" not in data or not isinstance(data["commands"], list):
        raise ValueError("Input must contain a 'commands' list")

    commands = []
    for cmd in data["commands"]:
        if not all(k in cmd for k in ("id", "command", "log_dir")):
            raise ValueError(f"Command missing required fields: {cmd}")
        commands.append(CommandSpec(id=cmd["id"], command=cmd["command"], log_dir=cmd["log_dir"]))

    exec_data = data.get("execution", {})
    if "max_parallel" not in exec_data:
        raise ValueError("execution.max_parallel is required")

    config = ExecutionConfig(
        max_parallel=exec_data["max_parallel"],
        max_retries=exec_data.get("max_retries", 3),
        retry_backoff_seconds=exec_data.get("retry_backoff_seconds", [10, 30, 60]),
    )

    # Validate uniqueness
    ids = [c.id for c in commands]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate command IDs: {[x for x in ids if ids.count(x) > 1]}")

    log_dirs = [c.log_dir for c in commands]
    if len(log_dirs) != len(set(log_dirs)):
        raise ValueError(f"Duplicate log directories: {[x for x in log_dirs if log_dirs.count(x) > 1]}")

    return commands, config


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


def combine_output(stdout: str, stderr: str, max_lines: int = 50) -> str:
    """Merge a failed command's output streams into a diagnostic tail.

    Inspect writes startup failures (missing API key, unresolvable model,
    provider initialisation errors) to **stdout**, not stderr — so classifying
    or reporting on stderr alone silently discards the only evidence of the most
    common structural failures. Both streams are combined here.

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
        The trailing lines of the combined output, stripped.
    """
    parts = [part.strip() for part in (stderr, stdout) if part.strip()]
    combined = "\n".join(parts)
    lines = combined.splitlines()
    return "\n".join(lines[-max_lines:]).strip()


def classify_error(returncode: int, output: str, log_status: str | None) -> str:
    """Classify an error as routing, structural, transient, resource_contention, or unknown.

    `output` should be the combined stdout+stderr from `combine_output`, since
    Inspect reports prerequisite failures on stdout.
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

    # Log says error but stderr didn't match known patterns
    if log_status == "error":
        return "unknown"

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
# Log verification
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
    """Read the newest eval log in a directory and extract key fields."""
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
            if log.results.scores and len(log.results.scores) > 0:
                metrics = {}
                for name, metric in log.results.scores[0].metrics.items():
                    if hasattr(metric, "value") and isinstance(metric.value, (int, float)):
                        metrics[name] = metric.value
                result["metrics"] = metrics

        if log.stats is not None:
            started = log.stats.started_at
            completed = log.stats.completed_at
            if started and completed:
                from datetime import datetime

                if isinstance(started, str):
                    started = datetime.fromisoformat(started)
                if isinstance(completed, str):
                    completed = datetime.fromisoformat(completed)
                result["duration_seconds"] = (completed - started).total_seconds()

        if log.error is not None:
            result["error"] = str(log.error.message) if hasattr(log.error, "message") else str(log.error)

        return result

    except Exception as e:
        return {"log_path": log_path, "status": "verification_error", "error": str(e)}


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 3600  # 1 hour


def run_single(command: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, float, str, str]:
    """Run a single subprocess, return (returncode, duration, stdout, stderr)."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            duration = time.monotonic() - start
            return -1, duration, "", f"Process timed out after {timeout}s"

        duration = time.monotonic() - start
        return (
            proc.returncode or 0,
            duration,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )
    except Exception as e:
        duration = time.monotonic() - start
        return -1, duration, "", str(e)


def run_batch(
    commands: list[CommandSpec],
    concurrency: int,
    timeout: int = DEFAULT_TIMEOUT,
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
            future = executor.submit(run_single, cmd.command, timeout)
            future_to_cmd[future] = cmd

        for future in as_completed(future_to_cmd):
            cmd = future_to_cmd[future]
            try:
                returncode, duration, stdout, stderr = future.result()
            except Exception as e:
                returncode, duration, stdout, stderr = -1, 0.0, "", str(e)
            results.append((cmd, returncode, duration, stdout, stderr))

    return results


# ---------------------------------------------------------------------------
# Main execution pipeline
# ---------------------------------------------------------------------------


def execute(commands: list[CommandSpec], config: ExecutionConfig) -> ExecutionReport:
    """Run all commands with error handling, retries, and graceful degradation."""
    report = ExecutionReport(concurrency_used=config.max_parallel)
    total_start = time.monotonic()

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

        # Run the batch
        batch_results = run_batch(batch, current_concurrency)

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
                metrics=log_info.get("metrics", {}),
                duration_seconds=log_info.get("duration_seconds", duration),
                process_retries=retry_counts[cmd.id],
                batch=batch_num,
            )

            if log_info.get("error"):
                result.errors.append(log_info["error"])

            # Determine success
            if returncode == 0 and log_info.get("status") == "success":
                completed_results[cmd.id] = result
            else:
                # Both streams: Inspect reports prerequisite failures on stdout.
                diagnostic = combine_output(stdout, stderr)
                error_class = classify_error(
                    returncode, diagnostic, log_info.get("status")
                )

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

                if should_retry(error_class, retry_counts[cmd.id], config.max_retries):
                    retry_counts[cmd.id] += 1
                    failed_in_batch.append((cmd, error_class))
                else:
                    # Exhausted retries or structural failure
                    result.process_retries = retry_counts[cmd.id]
                    completed_results[cmd.id] = result

        # Cascading failure detection
        backoff_multiplier = 1.0
        if len(batch) > 1 and len(failed_in_batch) > len(batch) / 2:
            error_classes = [ec for _, ec in failed_in_batch]
            if all(ec == "resource_contention" for ec in error_classes):
                new_concurrency = max(1, current_concurrency // 2)
                if new_concurrency < current_concurrency:
                    report.concurrency_reductions.append(
                        ConcurrencyReduction(
                            batch=batch_num,
                            previous_level=current_concurrency,
                            new_level=new_concurrency,
                            reason=f"Resource contention: {resource_contention_count}/{len(batch)} commands failed",
                        )
                    )
                    current_concurrency = new_concurrency
            elif all(ec == error_classes[0] for ec in error_classes) and error_classes[0] in ("structural", "routing"):
                # All the same unretryable error — systemic problem, abort
                # remaining. A uniform routing failure means the pinned provider
                # is down for every command, so continuing only wastes time.
                report.errors.append(
                    f"Batch {batch_num}: all failures are {error_classes[0]} — aborting remaining commands"
                )
                # Mark remaining pending as not_run
                for remaining_cmd in pending:
                    completed_results[remaining_cmd.id] = EvalResult(
                        id=remaining_cmd.id,
                        status="aborted",
                        errors=[f"Aborted due to systemic {error_classes[0]} failure"],
                    )
                pending = []
                break
            elif all(ec == "transient" for ec in error_classes):
                # All same transient error — double the backoff for retries
                backoff_multiplier = 2.0
        elif resource_contention_count > 0 and current_concurrency > 1:
            # Even a single resource contention in a batch warrants reduction
            new_concurrency = max(1, current_concurrency // 2)
            if new_concurrency < current_concurrency:
                report.concurrency_reductions.append(
                    ConcurrencyReduction(
                        batch=batch_num,
                        previous_level=current_concurrency,
                        new_level=new_concurrency,
                        reason=f"Resource contention detected in batch {batch_num}",
                    )
                )
                current_concurrency = new_concurrency

        # Queue retries with backoff
        if failed_in_batch:
            max_backoff = max(
                compute_backoff(retry_counts[cmd.id] - 1, config.retry_backoff_seconds)
                for cmd, _ in failed_in_batch
            ) * backoff_multiplier
            if not _SHUTDOWN.is_set():
                time.sleep(max_backoff)
            # Prepend retries so they run in the next iteration
            retry_commands = [cmd for cmd, _ in failed_in_batch]
            pending = retry_commands + pending

    # Handle shutdown
    if _SHUTDOWN.is_set():
        report.status = "interrupted"
        for remaining_cmd in pending:
            if remaining_cmd.id not in completed_results:
                completed_results[remaining_cmd.id] = EvalResult(
                    id=remaining_cmd.id,
                    status="interrupted",
                    errors=["Execution interrupted by signal"],
                )

    # Assemble final results in original command order
    report.results = [
        completed_results.get(cmd.id, EvalResult(id=cmd.id, status="not_run"))
        for cmd in commands
    ]
    report.concurrency_used = config.max_parallel
    report.total_wall_clock_seconds = round(time.monotonic() - total_start, 1)

    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) != 2:
        print(json.dumps({"status": "error", "errors": ["Usage: execute_evals.py <input.json>"]}))
        sys.exit(1)

    input_path = sys.argv[1]

    try:
        commands, config = parse_input(input_path)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(json.dumps({"status": "error", "errors": [f"Input error: {e}"]}))
        sys.exit(1)

    report = execute(commands, config)
    print(json.dumps(asdict(report), indent=2, default=str))


if __name__ == "__main__":
    main()
