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
import glob
import importlib.util
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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
# sandbox per sample: samples in flight (and so sandboxes) is the product,
# 2 × 5 = 10. An unconstrained Executor once opened 24 containers on a 10-core,
# 8 GB box and nearly ran it out of memory; a sandbagging rerun then held 10
# in flight on a 10-core, 16 GB box for 28 hours with no errors (each idle
# sandbox used about 2 MB). Two subprocesses rather than one at ten keeps
# slots busy while a pair's slowest last samples finish. These are the
# starting values: unless a request or the Executor sets either, automatic
# sizing (below) replaces them once preflight has measured the sandboxes. A
# request may override them via `overrides`; the Executor agent may raise them
# only via `--max-parallel` / `--max-connections` with a `--rationale`, which
# the draft report records verbatim.
DEFAULT_MAX_PARALLEL = 2
DEFAULT_MAX_CONNECTIONS = 5

# Automatic sizing. Samples in flight = the smallest of: the share of free
# memory it may plan to use over the heaviest sandbox's peak, a few sandboxes
# per core, and a hard cap; split evenly with other live runs on the machine
# and re-asked before every condition starts. Measured on a 10-core / 16 GB
# box: the sandbagging eval at 20 in flight held free memory at ~9.3 GB;
# at 30 memory stayed fine but load peaked at 41 on 10 cores.
AUTO_MEMORY_SHARE = 0.5  # the other half is headroom, which also absorbs a newcomer's overlap
AUTO_PER_CORE = 2  # busy sandboxes compete for the processor
AUTO_MAX_IN_FLIGHT = 40  # provider limits above ~10 at once are untested
AUTO_MIN_CONTAINER_MB = 100  # idle sandboxes read ~2 MB; this covers the eval process's share
AUTO_MIN_ALONE = DEFAULT_MAX_PARALLEL * DEFAULT_MAX_CONNECTIONS  # never below the old default alone
AUTO_MIN_SHARED = 4  # a run sharing the machine still makes progress
AUTO_PER_CONDITION = 10  # samples per condition process when splitting across conditions
MEASURE_INTERVAL = 10  # seconds between sandbox memory samples during preflight
RUN_REGISTRY_ENV = "PRISM_RUN_REGISTRY"  # where live runs register; default ~/.cache/prism/live_runs

# The engine's thread pool only bounds how many commands *could* run; the
# width chosen per start decides how many do
EXECUTE_POOL = 64
EXECUTE_POLL = 5  # seconds between checks for a finished command or a due retry

# Per-command wall-clock limit. Generous: a 30-epoch sandboxed eval with a
# 600 s task limit at four connections is over an hour, and the batch runs
# detached so nothing above it is waiting.
DEFAULT_COMMAND_TIMEOUT = 4 * 3600

# Per-probe limit for preflight. A probe is one sample (or one per pinned id):
# the slowest seen took a few minutes (a reasoning model, or a first sandbox
# image build), so a probe still going after this is stalled, most likely on
# a provider that is not answering. Under the full limit it once sat 4 hours.
DEFAULT_PREFLIGHT_TIMEOUT = 15 * 60

# What run_single reports when a command outlives its limit.
TIMED_OUT = "Process timed out after"

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
    "preflight_timeout_seconds",
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
    # Whether a command that outlived command_timeout is tried again. Preflight
    # turns this off: a stalled probe would just stall again, once per retry.
    retry_stalled: bool = True
    preflight_timeout: int = DEFAULT_PREFLIGHT_TIMEOUT


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
    # Dataset loading failures, by their specific names only. A bare
    # "dataset" also matches Inspect's own run header (`dataset: <name>`),
    # which is in the stdout of every run that got started — so a transient
    # failure mid-run would classify structural and never be retried.
    "DatasetNotFoundError",
    "DatasetGenerationError",
    "dataset not found",
    "No such dataset",
    "Couldn't find a dataset",
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
# the identical command cannot help — the pinned provider is unavailable — but
# they are kept separate from _STRUCTURAL_PATTERNS so the Executor can report
# them under their own category. The remedy is the orchestrator's (wait,
# re-pin, or drop the model), not a configuration fix, and relaxing the pin to
# make the command succeed would silently change the measurement instrument
# mid-experiment.
_ROUTING_PATTERNS = [
    "no allowed providers",
    "no endpoints found",
    "no providers available",
]

# Target-model credential exhaustion: the provider refused on account credit
# or a usage cap. Classified **structural** (no retry can help, and the
# Executor taxonomy gains no category) but its evidence line is tagged
# `[credentials]`, because the remedy — restore credit, then resume — is
# neither a task fix nor a re-pin, and a lead agent reading a bare structural
# tail would go looking for a bug in the task. Checked before every other
# class: the Anthropic cap arrives as a 400 and the OpenRouter budget as a 403
# prefixed "Failed to authenticate", either of which would otherwise read as a
# misconfiguration. These were formerly routed via the `402`/credits entries
# of _ROUTING_PATTERNS; an account that cannot pay is not a pinned provider
# that is down. Kept identical to `subagents/runner.py`'s CREDENTIAL_PATTERNS
# (a test pins the two); see there for each string's archive provenance.
_CREDENTIAL_PATTERNS = [
    "reached your specified workspace api usage limits",  # Anthropic workspace cap (R04/R05 stderr)
    "workspace lifetime budget",  # OpenRouter workspace budget (R01/R02/R04)
    "credit balance is too low",  # Anthropic prepaid credit (documented)
    "requires more credits",  # OpenRouter 402 (documented)
    "insufficient credits",  # OpenRouter 402 (documented)
    # OpenAI quota exhaustion arrives as a 429, which would otherwise read as
    # a rate limit and be retried: error code `insufficient_quota`, message
    # "You exceeded your current quota, please check your plan and billing
    # details" (documented).
    "insufficient_quota",
    "you exceeded your current quota",
]
# A bare "402" also matches sample ids and hashes; count it only as a status.
_CREDENTIAL_STATUS_RE = re.compile(r"(?:error code|api error|http|status(?:[ _]code)?)[:=\s]*402\b", re.I)

# The shape of a provider API error inside a sample's error text: an SDK
# exception class or an HTTP status. A sample error that merely *mentions*
# credit (a tool in a billing-themed eval failing with "insufficient credits")
# is the task's own behaviour, not the provider refusing, and is not counted.
_PROVIDER_ERROR_RE = re.compile(
    r"error code:?\s*\d{3}|api ?error|apistatuserror|badrequesterror|ratelimiterror|"
    r"permissiondeniederror|authenticationerror|status(?:[ _]code)?[:=\s]*\d{3}|http \d{3}",
    re.I,
)

# Tag appended to the structural evidence line of a credential failure.
CREDENTIALS_TAG = "[credentials]"


# One line the report attaches to every `[credentials]` pair, so the reader
# need not infer the remedy from a provider's prose.
_CREDENTIALS_NOTE = (
    "the model's provider refused on account credit or a usage cap. Not a task, "
    "configuration or pin bug; re-running cannot help until credit is restored."
)


def is_credential_exhaustion(output: str) -> bool:
    """Whether captured eval output shows the provider refusing on credit.

    Parameters
    ----------
    output : str
        Combined, scrubbed stdout+stderr from `combine_output`.

    Returns
    -------
    bool
        True for a credit/usage-cap refusal of the target model (or of a
        model-graded scorer the eval itself calls).

    Examples
    --------
    >>> is_credential_exhaustion("anthropic.BadRequestError: Error code: 400 - "
    ...     "You have reached your specified workspace API usage limits.")
    True
    """
    low = output.lower()
    return any(p in low for p in _CREDENTIAL_PATTERNS) or bool(_CREDENTIAL_STATUS_RE.search(output))


def is_provider_credential_error(text: str) -> bool:
    """Whether one sample's error is a provider refusing on credit.

    Stricter than `is_credential_exhaustion` (which reads process output,
    where only providers speak): the text must also have a provider API
    error's shape (`_PROVIDER_ERROR_RE`), so a task-level error that happens
    to mention payment or credits is not counted.

    Parameters
    ----------
    text : str
        A sample's error message.

    Returns
    -------
    bool
        True for a provider credit/usage-cap refusal.

    Examples
    --------
    >>> is_provider_credential_error("ToolError: payment failed: insufficient credits")
    False
    >>> is_provider_credential_error("openai.RateLimitError: Error code: 429 - "
    ...     "{'error': {'code': 'insufficient_quota'}}")
    True
    """
    return is_credential_exhaustion(text) and bool(_PROVIDER_ERROR_RE.search(text))

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
# metrics), hence `\S+` rather than a word class, and a name may contain single
# spaces (a grouped metric's group, `goal conflict  0.500`); columns are
# separated by two or more. `_METRIC_TOKEN` will also redact innocent
# `name: 1.5` tokens in error text (`version: 2.45`); that collateral is
# accepted because the blackout is absolute.
_METRIC_COLUMNS = re.compile(r"^\s*(\S+(?: \S+)*\s{2,}(n/a|-?\d+\.\d+)\s*)+$")
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


def classify_error(returncode: int, output: str) -> str:
    """Classify an error as routing, structural, transient, resource_contention, or unknown.

    Parameters
    ----------
    returncode : int
        The subprocess exit code (negative when killed by a signal).
    output : str
        Combined stdout+stderr from `combine_output` — Inspect reports
        prerequisite failures on stdout.

    Returns
    -------
    str
        One of ``resource_contention``, ``routing``, ``structural``,
        ``transient`` or ``unknown``.
    """
    combined = output.lower()

    # Credential exhaustion first: its 400/403 wrappers would otherwise match
    # a routing or misconfiguration pattern. Structural, tagged by the caller.
    if is_credential_exhaustion(output):
        return "structural"

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

    Each errored sample's error message is classified with
    `is_provider_credential_error`. Under ``--no-fail-on-error`` a credit or
    usage cap reached mid-eval does not fail the process: Inspect exits 0 and
    the remaining samples error one by one, so the process-level
    ``[credentials]`` tag in `classify_error` never sees it. Only the count is
    kept — the message itself is never stored, so nothing a scorer wrote into
    an error can reach the status file.

    Parameters
    ----------
    log_path : str
        Path to a ``.eval`` file.

    Returns
    -------
    dict
        ``samples``, ``sample_errors``, ``credential_sample_errors`` (how
        many of those errors are a provider credit/usage-cap refusal),
        ``sample_retries``, and
        ``termination`` = ``{no_assistant_msg, limit_hit, error_terminated}``.
        Example: ``{"samples": 30, "sample_errors": 12,
        "credential_sample_errors": 11, "sample_retries": 2,
        "termination": {"no_assistant_msg": 0, "limit_hit": 3,
        "error_terminated": 12}}``.
    """
    from inspect_ai.log import read_eval_log_sample_summaries

    summaries = read_eval_log_sample_summaries(log_path)
    errors = sum(1 for s in summaries if s.error is not None)
    credential_errors = sum(
        1 for s in summaries if s.error is not None and is_provider_credential_error(str(s.error))
    )
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
        "credential_sample_errors": credential_errors,
        "sample_retries": retries,
        "termination": {
            "no_assistant_msg": no_assistant,
            "limit_hit": limit_hit,
            "error_terminated": errors,
        },
    }


def served_models(log_path: str) -> list[str]:
    """The model names the provider's replies reported, across a log's samples.

    A request names a model such as ``openai/gpt-5`` or an OpenRouter model at
    one provider, but what answers can be a dated version of it, or a
    different build once a provider swaps it. Inspect keeps the name each
    reply reported as the sample output's ``model``; only that field is read.

    Parameters
    ----------
    log_path : str
        Path to a ``.eval`` file.

    Returns
    -------
    list[str]
        Distinct names, sorted, e.g. ``["gpt-5-2025-08-07"]``; empty when no
        sample recorded one.
    """
    from inspect_ai.log import read_eval_log_samples

    names = set()
    for sample in read_eval_log_samples(log_path, all_samples_required=False):
        model = getattr(sample.output, "model", None)
        if model:
            names.add(str(model))
    return sorted(names)


# How often the live counter re-reads each running eval's log, in seconds.
# Inspect saves a running .eval every ten finished samples or fewer, so a
# faster poll would mostly re-read an unchanged file.
LIVE_COUNT_INTERVAL = 20


def live_samples_completed(log_dir: str, since: float) -> int | None:
    """Finished samples so far in the eval still writing into a log directory.

    Inspect saves a running ``.eval`` log every few finished samples (at most
    ten), replacing the file in one step, so its sample summaries can be read
    mid-run without a lock and without ever seeing a half-written file. Only
    the ``completed`` flags are read; nothing about scores is kept.

    Parameters
    ----------
    log_dir : str
        The pair's log directory.
    since : float
        When this attempt started (seconds since the epoch). An older log in
        the same directory belongs to an earlier run and is ignored.

    Returns
    -------
    int | None
        The count at the log's last save, or None while this attempt has no
        log yet or its log can't be read.
    """
    path = find_newest_log(log_dir)
    if path is None or os.path.getmtime(path) < since:
        return None
    try:
        from inspect_ai.log import read_eval_log_sample_summaries

        return sum(1 for s in read_eval_log_sample_summaries(path) if s.completed)
    except Exception:  # mid-replace or unreadable: try again next round
        return None


def live_samples_planned(log_dir: str, since: float) -> int | None:
    """How many samples the eval still writing into a log directory will run.

    Inspect writes the plan into the log's header when the eval starts: the
    sample ids left after ``--limit`` or ``--sample-id``, run ``epochs`` times
    each. Only the header is read.

    Parameters
    ----------
    log_dir : str
        The pair's log directory.
    since : float
        When this attempt started (seconds since the epoch); an older log is ignored.

    Returns
    -------
    int | None
        e.g. 300 for three pinned ids at 100 epochs; None while this attempt
        has no log yet or its header can't be read.
    """
    path = find_newest_log(log_dir)
    if path is None or os.path.getmtime(path) < since:
        return None
    try:
        from inspect_ai.log import read_eval_log

        spec = read_eval_log(path, header_only=True).eval
        return len(spec.dataset.sample_ids or []) * (spec.config.epochs or 1) or None
    except Exception:  # mid-replace or unreadable: try again next round
        return None


def watch_live_counts(status: Status, stop: threading.Event, interval: float = LIVE_COUNT_INTERVAL) -> None:
    """Keep each running pair's ``samples_completed`` current until stopped.

    Runs on its own thread beside ``execute``, which records a pair's counts
    only once its eval process exits. The final count from the finished log
    always replaces the live one, since a pair is touched here only while its
    state is ``running``. Each attempt's planned size goes in
    ``samples_planned`` as soon as its log header names it, so the panel can
    show a total before ``samples_total`` is known at the end.

    Parameters
    ----------
    status : Status
        The run's status file.
    stop : threading.Event
        Set when the run is over.
    interval : float
        Seconds between rounds.
    """
    # Per pair: (attempt number, wall-clock start) of the attempt being watched
    started: dict[str, tuple[int, float]] = {}
    # Per pair: (attempt number, planned samples or None until its header is read)
    planned: dict[str, tuple[int, int | None]] = {}
    while not stop.wait(interval):
        with status.lock:
            running = {
                pid: (e["attempts"], e["log_dir"])
                for pid, e in status.data["pairs"].items()
                if e.get("state") == "running" and e.get("log_dir")
            }
        # A new attempt restarts its count from its own fresh log
        for pid, (attempt, _) in running.items():
            if started.get(pid, (None,))[0] != attempt:
                started[pid] = (attempt, time.time())

        # Logs are read outside the lock so a slow read never holds up an event;
        # an attempt's plan is read until found, then kept
        counts = {pid: live_samples_completed(log_dir, started[pid][1]) for pid, (_, log_dir) in running.items()}
        for pid, (attempt, log_dir) in running.items():
            if planned.get(pid, (None, None))[0] != attempt or planned[pid][1] is None:
                planned[pid] = (attempt, live_samples_planned(log_dir, started[pid][1]))
        with status.lock:
            changed = False
            for pid, done in counts.items():
                e = status.pair(pid)
                if e.get("state") != "running" or e["attempts"] != running[pid][0]:
                    continue
                if done is not None:
                    changed |= e.get("samples_completed") != done
                    e["samples_completed"] = done
                size = planned[pid][1]
                if size is not None and e.get("samples_planned") != size:
                    e["samples_planned"] = size
                    changed = True
            if changed:
                status.save()


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


# How long a stopped eval gets to shut down by itself. Inspect treats SIGINT as
# a cancel and removes its sandbox containers on the way out, which takes tens
# of seconds for a batch of them; SIGTERM it does not handle, so it dies at once
# and leaves every container running.
STOP_GRACE_SECONDS = 90
TERM_GRACE_SECONDS = 10


def _kill_group(proc: subprocess.Popen) -> None:
    """Stop a child and everything in its process group, gently first.

    SIGINT, so Inspect can cancel and clean up its sandboxes; then SIGTERM;
    then SIGKILL, each after a grace period.
    """
    for sig, grace in ((signal.SIGINT, STOP_GRACE_SECONDS), (signal.SIGTERM, TERM_GRACE_SECONDS)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.communicate(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.communicate()


# ---------------------------------------------------------------------------
# Leftover sandboxes
# ---------------------------------------------------------------------------

# Labels Docker Compose puts on every container: the project Inspect named it
# (``inspect-<task>-i<random>``) and the folder its compose file sits in, which
# for a Prism run is a condition folder inside the run's experiment directory
_PROJECT_LABEL = "com.docker.compose.project"
_WORKDIR_LABEL = "com.docker.compose.project.working_dir"
# The compose files the project was started from, comma-separated. For a task
# with only a Dockerfile, Inspect writes the compose file into its own data
# folder, so the working directory above is that folder, not the task's.
_CONFIG_LABEL = "com.docker.compose.project.config_files"
DOCKER_TIMEOUT = 120
# One `docker ps` line per container: project, working directory, compose files
_PS_FORMAT = "{{.Label \"%s\"}}\t{{.Label \"%s\"}}\t{{.Label \"%s\"}}" % (_PROJECT_LABEL, _WORKDIR_LABEL, _CONFIG_LABEL)


def _docker(args: list[str], timeout: int = DOCKER_TIMEOUT) -> str | None:
    """Run a docker command.

    Parameters
    ----------
    args : list[str]
        The arguments after ``docker``, e.g. ``["ps", "-a"]``.
    timeout : int
        Seconds before giving up on a slow daemon.

    Returns
    -------
    str | None
        Its stdout; None if docker is missing, timed out or failed.
    """
    try:
        out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _inside(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or somewhere beneath it, symlinks resolved.

    Parameters
    ----------
    path, root : str
        Directories; ``root`` already resolved with ``os.path.realpath``.

    Returns
    -------
    bool
        e.g. True for ``/exp/c_1`` and for ``/exp`` itself; False for ``/exp_copy``.
    """
    resolved = os.path.realpath(path)
    return resolved == root or resolved.startswith(root + os.sep)


def _build_contexts(compose_file: str) -> list[str]:
    """The ``build: context:`` directories a compose file names, made absolute.

    Parameters
    ----------
    compose_file : str
        Path to a compose file.

    Returns
    -------
    list[str]
        e.g. ``["/exp/c_1"]`` for Inspect's generated file for a Dockerfile in
        ``/exp/c_1``; empty when the file is gone or names no build.
    """
    try:
        with open(compose_file) as f:
            text = f.read()
    except OSError:
        return []
    base = os.path.dirname(compose_file)
    contexts = re.findall(r"^\s*context:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    return [os.path.join(base, c.strip("\"'")) for c in contexts]


def _started_inside(exp_dir: str, workdir: str, config_files: str) -> bool:
    """Whether a compose project was started for a task inside ``exp_dir``.

    Either its working directory is inside (a task with its own compose file),
    or one of its compose files builds from a folder inside (a task with only a
    Dockerfile, whose compose file Inspect writes to its own data folder). A
    sandbox with no Dockerfile or compose file at all names no folder of the
    task, so it cannot be told apart from another run's and is never matched.

    Parameters
    ----------
    exp_dir : str
        The run's experiment directory.
    workdir : str
        The project's working-directory label.
    config_files : str
        The project's comma-separated compose-files label.

    Returns
    -------
    bool
    """
    root = os.path.realpath(exp_dir)
    if workdir and _inside(workdir, root):
        return True
    return any(_inside(ctx, root)
               for f in config_files.split(",") if f
               for ctx in _build_contexts(f))


def _our_containers(exp_dir: str, docker: Any, running_only: bool = False) -> list[tuple[str, str]] | None:
    """The Inspect sandbox containers started for tasks inside ``exp_dir``.

    Parameters
    ----------
    exp_dir : str
        The run's experiment directory.
    docker : Callable
        Runs a docker command.
    running_only : bool
        Leave out stopped containers.

    Returns
    -------
    list[tuple[str, str]] | None
        ``(container name, compose project)`` pairs, e.g.
        ``[("inspect-sandbagging-i6tmri7-default-1", "inspect-sandbagging-i6tmri7")]``;
        None when docker could not be asked.
    """
    all_flag = [] if running_only else ["-a"]
    out = docker(["ps", *all_flag, "--filter", f"label={_PROJECT_LABEL}", "--format", "{{.Names}}\t" + _PS_FORMAT])
    if out is None:
        return None
    ours = []
    for line in out.splitlines():
        name, project, workdir, config_files = (line.split("\t") + ["", "", "", ""])[:4]
        if project.startswith("inspect-") and _started_inside(exp_dir, workdir, config_files):
            ours.append((name, project))
    return ours


def leftover_sandboxes(exp_dir: str, docker: Any = None) -> list[str] | None:
    """Inspect's sandbox projects still present that were started from inside ``exp_dir``.

    Parameters
    ----------
    exp_dir : str
        The run's experiment directory.
    docker : Callable
        Runs a docker command; None for ``_docker``. Tests pass a stand-in.

    Returns
    -------
    list[str] | None
        Project names, sorted, e.g. ``["inspect-sandbagging-i6tmri7"]``; None
        when docker could not be asked. Containers started anywhere else, by
        other runs or other folders, are never listed.
    """
    ours = _our_containers(exp_dir, docker or _docker)
    return None if ours is None else sorted({project for _, project in ours})


def _other_live_run(run_dir: str) -> str | None:
    """Another unfinished run beside this one whose worker is alive, if any.

    Its containers sit under the same experiment directory, so removing
    leftovers then could remove its live sandboxes.

    Parameters
    ----------
    run_dir : str
        This run's directory; its siblings are the other runs.

    Returns
    -------
    str | None
        The other run's id, e.g. ``"iter_02_20261108T211422Z"``; None if none is going.
    """
    parent = os.path.dirname(os.path.abspath(run_dir))
    for other in sorted(glob.glob(os.path.join(parent, "*", "status.json"))):
        other_dir = os.path.dirname(other)
        if os.path.samefile(other_dir, run_dir):
            continue
        try:
            with open(other) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("state") not in TERMINAL_STATES and _worker_alive(data):
            return other_dir
    return None


def remove_leftover_sandboxes(exp_dir: str, run_dir: str, docker: Any = None) -> dict[str, Any]:
    """Remove the sandbox containers a finished or stopped run left behind, and their built images.

    Only once every eval of the run has ended: Inspect normally removes its own
    containers and images, so any still there from this run's experiment directory are
    leftovers of an eval that was killed. Skipped while another run beside this
    one is still going.

    Parameters
    ----------
    exp_dir : str
        The run's experiment directory.
    run_dir : str
        This run's directory.
    docker : Callable
        Runs a docker command; None for ``_docker``. Tests pass a stand-in.

    Returns
    -------
    dict
        ``{"removed": [...], "failed": [...], "skipped": reason | None}``.
    """
    docker = docker or _docker
    result: dict[str, Any] = {"removed": [], "failed": [], "skipped": None}
    other = _other_live_run(run_dir)
    if other:
        result["skipped"] = f"another run in this experiment is still going ({other})"
        return result
    projects = leftover_sandboxes(exp_dir, docker)
    if projects is None:
        result["skipped"] = "docker could not be asked (not installed or not running)"
        return result
    for project in projects:
        # `--rmi local` also removes the image Compose built for the project
        # (named after it), as Inspect's own clean-up would have; images
        # pulled by name for an `image:` line are shared and stay
        done = docker(["compose", "-p", project, "down", "--volumes", "--remove-orphans", "--rmi", "local"]) is not None
        result["removed" if done else "failed"].append(project)
    return result


# ---------------------------------------------------------------------------
# Automatic sizing: how many samples to keep in flight
# ---------------------------------------------------------------------------

_MEM_UNITS = {"b": 1 / 2**20, "kib": 1 / 1024, "kb": 1 / 1000, "mib": 1, "mb": 1, "gib": 1024, "gb": 1000}


def parse_mem_mb(text: str) -> float | None:
    """The used half of a ``docker stats`` memory reading, in MB.

    Parameters
    ----------
    text : str
        E.g. ``"12.3MiB / 15.6GiB"``.

    Returns
    -------
    float | None
        ``12.3`` for the example; None if it cannot be read.
    """
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text)
    if not m or m.group(2).lower() not in _MEM_UNITS:
        return None
    return float(m.group(1)) * _MEM_UNITS[m.group(2).lower()]


def experiment_sandbox_memory(exp_dir: str, docker: Any = None) -> dict[str, float] | None:
    """Memory used now by each sandbox container started from inside ``exp_dir``.

    Parameters
    ----------
    exp_dir : str
        The run's experiment directory.
    docker : Callable
        Runs a docker command; None for ``_docker``. Tests pass a stand-in.

    Returns
    -------
    dict[str, float] | None
        Container name → MB, e.g. ``{"inspect-sandbagging-i6tmri7-default-1": 41.2}``;
        None when docker could not be asked. Other runs' containers are left out.
    """
    docker = docker or _docker
    found = _our_containers(exp_dir, docker, running_only=True)
    if found is None:
        return None
    ours = [name for name, _ in found]
    if not ours:
        return {}
    stats = docker(["stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}", *ours])
    if stats is None:
        return None
    memory = {}
    for line in stats.splitlines():
        name, _, usage = line.partition("\t")
        mb = parse_mem_mb(usage)
        if mb is not None:
            memory[name] = mb
    return memory


class SandboxSampler:
    """Sample this experiment's sandbox memory in the background while preflight runs.

    Parameters
    ----------
    exp_dir : str
        The run's experiment directory.
    interval : float
        Seconds between samples.
    docker : Callable
        Runs a docker command; None for ``_docker``.
    """

    def __init__(self, exp_dir: str, interval: float = MEASURE_INTERVAL, docker: Any = None):
        self.exp_dir, self.interval, self.docker = exp_dir, interval, docker
        self.peaks: dict[str, float] = {}
        self.min_free_mb: int | None = None
        self.docker_ok = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> None:
        """Take one reading: each container's memory peak and the machine's lowest free memory."""
        memory = experiment_sandbox_memory(self.exp_dir, self.docker)
        if memory is None:
            self.docker_ok = False
        for name, mb in (memory or {}).items():
            self.peaks[name] = max(mb, self.peaks.get(name, 0.0))
        free = _host_resources()["free_mem_mb"]
        if free is not None:
            self.min_free_mb = free if self.min_free_mb is None else min(free, self.min_free_mb)

    def _run(self) -> None:
        """The background thread: sample every ``interval`` seconds until stopped."""
        while True:
            self._sample()
            if self._stop.wait(self.interval):
                return

    def start(self) -> "SandboxSampler":
        """Begin sampling in the background.

        Returns
        -------
        SandboxSampler
            This sampler, so it can be made and started in one line.
        """
        self._thread.start()
        return self

    def stop(self) -> dict[str, Any]:
        """Stop sampling and summarise.

        Returns
        -------
        dict
            ``{"containers_seen": 2, "peak_container_mb": 41.2, "min_free_mb": 9304, "docker": True}``.
        """
        self._stop.set()
        self._thread.join()
        return {
            "containers_seen": len(self.peaks),
            "peak_container_mb": round(max(self.peaks.values()), 1) if self.peaks else None,
            "min_free_mb": self.min_free_mb,
            "docker": self.docker_ok,
        }


def machine_capacity(measured: dict[str, Any] | None, nproc: int | None, sandboxed: bool) -> tuple[int, str]:
    """How many samples this machine can keep in flight, from preflight's measurements.

    Parameters
    ----------
    measured : dict | None
        `SandboxSampler.stop`'s summary; None when preflight did not run.
    nproc : int | None
        The machine's core count.
    sandboxed : bool
        Whether the eval runs one sandbox per sample.

    Returns
    -------
    tuple[int, str]
        The capacity and how it was reached, e.g.
        ``(30, "cores: 3 x 10 (memory allows 46; heaviest sandbox 100 MB)")``.
    """
    if not sandboxed:
        return AUTO_MAX_IN_FLIGHT, "no sandboxes: the hard cap"
    if not measured or not measured.get("containers_seen") or not measured.get("min_free_mb"):
        return AUTO_MIN_ALONE, "sandboxes not measured: the old default"
    per_sample = max(measured["peak_container_mb"] or 0.0, AUTO_MIN_CONTAINER_MB)
    by_memory = int(measured["min_free_mb"] * AUTO_MEMORY_SHARE // per_sample)
    by_cores = AUTO_PER_CORE * (nproc or 1)
    limits = {"memory": by_memory, "cores": by_cores, "cap": AUTO_MAX_IN_FLIGHT}
    binding = min(limits, key=limits.get)
    detail = f"memory allows {by_memory} at {per_sample:.0f} MB each; {AUTO_PER_CORE} per core allows {by_cores}; cap {AUTO_MAX_IN_FLIGHT}"
    return max(1, limits[binding]), f"{binding} ({detail})"


def target_in_flight(capacity: int, live_runs: int) -> int:
    """This run's share of the machine: an even split with the other live runs.

    A run never waits for room: arriving beside a run that holds everything,
    it takes its share at once (the memory headroom covers the overlap) and
    the older run drops to its share at its next condition start.

    Parameters
    ----------
    capacity : int
        From `machine_capacity`.
    live_runs : int
        Live runs on the machine, this one included.

    Returns
    -------
    int
        E.g. 30 alone, 15 beside one other run, never below the floors.
    """
    if live_runs <= 1:
        return max(capacity, AUTO_MIN_ALONE)
    return max(capacity // live_runs, AUTO_MIN_SHARED)


def split_in_flight(in_flight: int, unfinished: int) -> tuple[int, int]:
    """Spread samples in flight over conditions running at once.

    Parameters
    ----------
    in_flight : int
        Samples to keep in flight.
    unfinished : int
        Conditions not yet finished (running or waiting).

    Returns
    -------
    tuple[int, int]
        ``(conditions at once, connections each)``: 30 over eleven → ``(3, 10)``;
        10 → ``(2, 5)``; 30 with one condition left → ``(1, 30)``.
    """
    width = max(1, min(max(unfinished, 1), max(2, round(in_flight / AUTO_PER_CONDITION))))
    return width, max(1, in_flight // width)


def with_connections(command: str, connections: int) -> str:
    """The command with its ``--max-connections`` value replaced.

    Parameters
    ----------
    command : str
        A rendered ``inspect eval`` command carrying ``--max-connections N``.
    connections : int
        The new value.

    Returns
    -------
    str
        The command, changed only in that flag.
    """
    return re.sub(r"--max-connections \d+", f"--max-connections {connections}", command)


def _run_registry() -> str:
    """The folder where each live run on this machine leaves an entry.

    Returns
    -------
    str
        ``$PRISM_RUN_REGISTRY`` when set, else ``~/.cache/prism/live_runs``.
    """
    return os.environ.get(RUN_REGISTRY_ENV) or os.path.expanduser("~/.cache/prism/live_runs")


def register_live_run(run_dir: str) -> str:
    """Record this worker as a live run on the machine, for other runs' even split.

    Parameters
    ----------
    run_dir : str
        This run's directory.

    Returns
    -------
    str
        The registry entry's path, removed by `unregister_live_run`.
    """
    os.makedirs(_run_registry(), exist_ok=True)
    path = os.path.join(_run_registry(), f"{os.getpid()}.json")
    with open(path, "w") as f:
        json.dump({"pid": os.getpid(), "pid_start": _pid_start_time(os.getpid()), "run_dir": run_dir}, f)
    return path


def unregister_live_run(path: str) -> None:
    """Remove this run's registry entry; one already gone is fine.

    Parameters
    ----------
    path : str
        The entry `register_live_run` returned.
    """
    try:
        os.remove(path)
    except OSError:
        pass


def count_live_runs() -> int:
    """Live runs on this machine, this one included; dead entries are pruned.

    Returns
    -------
    int
        E.g. 2 while another Prism run is executing evals; at least 1.
    """
    live = 0
    for entry in glob.glob(os.path.join(_run_registry(), "*.json")):
        try:
            with open(entry) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if _pid_alive(data.get("pid"), data.get("pid_start")):
            live += 1
        else:
            unregister_live_run(entry)  # a killed run never cleaned up
    return max(live, 1)


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
                    return -1, time.monotonic() - start, "", f"{TIMED_OUT} {timeout}s"

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
    size_launch: Any = None,
) -> ExecutionReport:
    """Run all commands with error handling, retries, and graceful degradation.

    Commands start as slots free up, not in fixed groups: a condition that
    finishes early hands its slot straight to the next one instead of idling
    until its slowest neighbour is done.

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
        ``on_event(kind, payload)`` called with ``("batch_start", [id])`` as
        each command starts, ``("launch", {...})`` with its sizing when
        ``size_launch`` is set, ``("result", EvalResult)`` and
        ``("reduction", ConcurrencyReduction)``, so a status file can be kept
        current while the run goes.
    size_launch : callable | None
        ``size_launch(unfinished) -> (width, connections)``, asked before every
        start: how many commands may run at once now, and the
        ``--max-connections`` the next one gets (None keeps its command as
        built). None runs ``config.max_parallel`` at once with commands as built.

    Returns
    -------
    ExecutionReport
        Per-command results in the original order plus run-level fields.
    """
    report = ExecutionReport(concurrency_used=config.max_parallel)
    total_start = time.monotonic()

    def emit(kind: str, payload: Any) -> None:
        if on_event is not None:
            on_event(kind, payload)

    if not commands:
        report.total_wall_clock_seconds = 0.0
        return report

    # --- State ---------------------------------------------------------------
    # The queue holds (not_before, command); a retry waits out its backoff
    # there without holding up commands that are ready
    queue: list[tuple[float, CommandSpec]] = [(0.0, c) for c in commands]
    running: dict[Any, tuple[CommandSpec, int, int]] = {}  # future -> (command, launch number, width then)
    completed_results: dict[str, EvalResult] = {}
    retry_counts: dict[str, int] = {c.id: 0 for c in commands}
    ceiling: int | None = None if size_launch else config.max_parallel  # lowered on resource contention
    recent: list[str | None] = []  # error class of each finished attempt, None for success
    launches = 0
    last_reduction = 0  # launch count when the ceiling was last lowered
    peak = 0

    def width_and_connections() -> tuple[int, int | None]:
        if size_launch is None:
            return max(1, ceiling or 1), None
        width, connections = size_launch(len(queue) + len(running))
        return max(1, min(width, ceiling) if ceiling else width), connections

    with ThreadPoolExecutor(max_workers=EXECUTE_POOL) as pool:
        while (queue or running) and not _SHUTDOWN.is_set():

            # --- Start every due command the current width allows ------------
            while queue and not _SHUTDOWN.is_set():
                width, connections = width_and_connections()
                if len(running) >= width:
                    break
                now = time.monotonic()
                due = next((i for i, (not_before, _) in enumerate(queue) if not_before <= now), None)
                if due is None:
                    break
                _, cmd = queue.pop(due)
                launches += 1
                command = with_connections(cmd.command, connections) if connections else cmd.command
                emit("batch_start", [cmd.id])
                if size_launch is not None:
                    emit("launch", {"id": cmd.id, "width": width, "connections": connections})
                future = pool.submit(
                    run_single, command, config.command_timeout, cmd.cwd, capture_dir,
                    f"{cmd.id}.attempt{retry_counts[cmd.id]}",
                )
                running[future] = (cmd, launches, width)
                peak = max(peak, len(running))

            # --- Wait for a finish, or for a retry to fall due ----------------
            if not running:
                if queue:
                    _SHUTDOWN.wait(max(0.0, min(nb for nb, _ in queue) - time.monotonic()))
                continue
            done, _ = wait(list(running), timeout=EXECUTE_POLL, return_when=FIRST_COMPLETED)

            for future in done:
                cmd, launch_no, width_then = running.pop(future)
                try:
                    returncode, duration, stdout, stderr = future.result()
                except Exception as e:
                    returncode, duration, stdout, stderr = -1, 0.0, "", str(e)

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
                    batch=launch_no,
                    model_usage=log_info.get("model_usage", {}),
                )
                if log_info.get("error"):
                    # A log's error message is the eval's own text and can quote
                    # scorer output; it is scrubbed like any captured stream.
                    result.errors.append(scrub_scores(log_info["error"]))

                # Success
                if returncode == 0 and log_info.get("status") == "success":
                    recent.append(None)
                    completed_results[cmd.id] = result
                    emit("result", result)
                    continue

                # Failure: classify and say why
                # Both streams: Inspect reports prerequisite failures on stdout.
                diagnostic = combine_output(stdout, stderr)
                error_class = classify_error(returncode, diagnostic)
                # Outliving the time limit is its own class: the command was
                # killed with nothing to show, which reads as a stall, not a
                # crash. Its output went with it, so the patterns cannot tell.
                if stderr.startswith(TIMED_OUT):
                    error_class = "stalled"
                result.error_class = error_class
                recent.append(error_class)
                if diagnostic:
                    # The tag rides on the already-scrubbed tail, so the
                    # blackout holds for credential failures as for any other.
                    tag = f"[{error_class}]"
                    if is_credential_exhaustion(diagnostic):
                        tag += f" {CREDENTIALS_TAG}"
                    result.errors.append(f"{tag} {diagnostic[:2000]}")
                else:
                    # A non-zero exit with no output at all leaves the
                    # orchestrator nothing to diagnose from, so say so
                    # explicitly rather than reporting an empty error list.
                    result.errors.append(
                        f"[{error_class}] Command exited {returncode} with no output "
                        "on stdout or stderr and produced no log."
                    )
                result.errors.append(f"exit_code={returncode}")

                # The attempts that finished around this one, for spotting a
                # failure shared by the whole run rather than one condition
                window = recent[-max(2, width_then):]
                failures = [c for c in window if c is not None]
                widespread = len(window) > 1 and len(failures) > len(window) / 2
                uniform = widespread and all(c == failures[0] for c in failures)

                # Resource contention halves how many run at once, once per
                # generation of starts (simultaneous failures count once)
                if error_class == "resource_contention" and width_then > 1 and launch_no > last_reduction:
                    reduction = ConcurrencyReduction(
                        batch=launch_no,
                        previous_level=width_then,
                        new_level=max(1, width_then // 2),
                        reason=f"Resource contention detected at launch {launch_no}",
                    )
                    report.concurrency_reductions.append(reduction)
                    emit("reduction", reduction)
                    ceiling = reduction.new_level
                    last_reduction = launches

                stall_stops = error_class == "stalled" and not config.retry_stalled
                if not stall_stops and should_retry(error_class, retry_counts[cmd.id], config.max_retries):
                    # A failed attempt may have left a partial .eval behind.
                    # Move it out before retrying: a second log in the pair's
                    # directory would double the Analyst's pool.
                    _stash_failed_attempt_logs(cmd, retry_counts[cmd.id], capture_dir)
                    retry_counts[cmd.id] += 1
                    result.status = "retrying"
                    emit("result", result)
                    # A transient error hitting most of the run gets a longer wait
                    backoff = compute_backoff(retry_counts[cmd.id] - 1, config.retry_backoff_seconds)
                    if uniform and failures[0] == "transient":
                        backoff *= 2
                    # Retries go to the front, so they run as soon as they are due
                    queue.insert(0, (time.monotonic() + backoff, cmd))
                else:
                    # Exhausted retries or structural failure
                    result.process_retries = retry_counts[cmd.id]
                    completed_results[cmd.id] = result
                    emit("result", result)

                # The same unretryable error across the run: systemic, abort
                # what has not started. A uniform routing failure means the
                # pinned provider is down for every command.
                if width_then > 1 and uniform and failures[0] in ("structural", "routing") and queue:
                    report.errors.append(
                        f"Launch {launch_no}: all failures are {failures[0]} — aborting remaining commands"
                    )
                    for _, remaining_cmd in queue:
                        if remaining_cmd.id in completed_results:
                            continue
                        aborted = EvalResult(
                            id=remaining_cmd.id,
                            status="aborted",
                            errors=[f"Aborted due to systemic {failures[0]} failure"],
                        )
                        completed_results[remaining_cmd.id] = aborted
                        emit("result", aborted)
                    queue = []

    # Handle shutdown: anything queued, and anything stopped mid-run
    if _SHUTDOWN.is_set():
        report.status = "interrupted"
        unfinished = [c for _, c in queue] + [c for c, _, _ in running.values()]
        for remaining_cmd in unfinished:
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
    report.concurrency_used = peak
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
            # A key that names no requested model (a typo, a stale entry) would
            # otherwise be ignored, and the model it meant would run unpinned.
            if isinstance(models, list) and m not in models:
                problems.append(f"model_routing key {m!r} is not one of the request's models")
            if not isinstance(r, dict):
                problems.append(f"model_routing[{m!r}] must be a mapping")
                continue
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
        for k in ("sample_limit", "epochs", "max_parallel", "max_connections", "command_timeout_seconds", "preflight_timeout_seconds"):
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
    # The working directory every command of this pair runs in: the task
    # file's own directory (see `task_location`). None only in a plan written
    # before this field existed, which then runs from experiment_dir.
    cwd: str | None = None
    # Samples the probe and the retest each run: 1 and 3, or, when the request
    # pins sample ids, one per id and three per id
    probe_samples: int = 1
    retest_samples: int = 3


def task_location(exp_dir: str, task: str) -> tuple[str, str]:
    """Where a condition's commands run, and the task path to pass from there.

    Inspect imports the task module relative to the task file, but resolves
    the sandbox compose file / Dockerfile and string ``Sample.files`` paths
    from the *process* working directory (`inspect_reference.md` gotcha 11).
    Run from a shared parent, every condition whose task sits in its own
    subdirectory would get the parent's sandbox and the same file bytes — the
    manipulation silently never lands. Running each condition from its task's
    directory makes all three resolve beside the task, as they do in a
    single-condition run; relative ``-T`` file arguments resolve there too.

    Parameters
    ----------
    exp_dir : str
        The request's absolute ``experiment_dir``.
    task : str
        The condition's task path, relative to ``exp_dir`` (validated).

    Returns
    -------
    tuple[str, str]
        ``(cwd, task_arg)``. Example: ``("/exp/cond_a", "task.py")`` for
        ``task="cond_a/task.py"``; a task given as a directory runs from that
        directory with ``"."``.
    """
    full = os.path.normpath(os.path.join(exp_dir, task))
    if os.path.isdir(full):
        return full, "."
    return os.path.dirname(full), os.path.basename(full)


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
    the ``openai`` pin on the ``uv run`` prefix. Every command of a pair runs
    from its task file's directory (``PairPlan.cwd``, see `task_location`), so
    the task argument is the file's name; log directories stay absolute.

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
        # Each condition runs from its task's directory; the task argument is
        # relative to that directory (Inspect rejects absolute task paths).
        cwd, task = task_location(exp_dir, task)
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
                n_ids = pinned_id_count(sample_id)
            else:
                probe = render(preflight_log_dir, limit=1, sample_id=None, epochs=None)
                retest = render(preflight_log_dir, limit=3, sample_id=None, epochs=None)
                n_ids = 1

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
                    cwd=cwd,
                    probe_samples=n_ids,
                    retest_samples=3 * n_ids,
                )
            )
    return pairs


def pinned_id_count(sample_id: Any) -> int:
    """How many sample ids a request's ``sample_id`` override pins.

    Parameters
    ----------
    sample_id : Any
        The override: one id, a comma-separated string of them, or a list.

    Returns
    -------
    int
        e.g. 3 for ``"e1,e2,e3"`` or ``["e1", "e2", "e3"]``, 1 for ``"episode"`` or 7; at least 1.
    """
    if isinstance(sample_id, list):
        return max(1, len(sample_id))
    if isinstance(sample_id, str):
        return max(1, len([i for i in sample_id.split(",") if i.strip()]))
    return 1


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
        # Held around every change as well as every save once the live sample
        # counter runs beside the main thread: a save serialises `data`, and a
        # key added mid-dump would abort it. Re-entrant so a holder can save.
        self.lock = threading.RLock()

    @classmethod
    def load(cls, run_dir: str) -> "Status":
        path = os.path.join(run_dir, "status.json")
        with open(path) as f:
            return cls(path, json.load(f))

    def save(self) -> None:
        with self.lock:
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
        "pid_start": None,
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
                # Of those, provider credit/usage-cap refusals (`[credentials]`).
                "credential_sample_errors": None,
                "sample_retries": None,
                "termination": None,
                "model_usage": {},
                # Token usage of every attempt of this pair — preflight probe,
                # retest, each full-run attempt including retried ones — so
                # `scripts/spend.py` counts all tokens spent, not only the
                # final attempt's (`model_usage`).
                "usage_attempts": [],
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
    entry["credential_sample_errors"] = shape["credential_sample_errors"]
    entry["sample_retries"] = shape["sample_retries"]
    entry["termination"] = shape["termination"]
    try:
        entry["served_models"] = served_models(log_path)
    except Exception as e:
        entry["errors"].append(f"served model names unreadable: {type(e).__name__}: {e}")


def _final_pair_state(result: EvalResult, entry: dict[str, Any]) -> str:
    """Map an engine result to the contract's matrix status vocabulary."""
    if result.status in ("aborted", "interrupted", "not_run", "retrying"):
        return result.status
    if result.status == "success":
        if entry.get("sample_errors"):
            return "sample-level-errors"
        return "success"
    return "failed"


def _usage_recorder(status: Status, pair: str) -> Any:
    """An `execute` event hook that keeps each attempt's token usage.

    Parameters
    ----------
    status : Status
        The run's status file.
    pair : str
        The pair id whose ``usage_attempts`` list receives the usage.

    Returns
    -------
    Callable[[str, Any], None]
        The ``on_event`` callback.
    """
    def record(kind: str, payload: Any) -> None:
        if kind == "result" and payload.model_usage:
            # Preflight probes run side by side, so this fires from worker threads.
            with status.lock:
                status.pair(pair).setdefault("usage_attempts", []).append(payload.model_usage)

    return record


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
        Retry/timeout parameters (each probe runs as its own one-command batch).
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
        # A probe is a sample or two: one still going after the short limit
        # is stalled, and trying it again would only stall again.
        command_timeout=min(config.preflight_timeout, config.command_timeout),
        retry_stalled=False,
    )
    exp_dir = status.data["experiment_dir"]

    def run_probe(p: PairPlan, kind: str) -> EvalResult | None:
        """Run one pair's probe (``kind="preflight"``) or retest; None once shutting down."""
        if _SHUTDOWN.is_set():
            return None
        with status.lock:
            status.pair(p.id)["state"] = "preflight"
            status.save()
        shutil.rmtree(p.preflight_log_dir, ignore_errors=True)
        command = p.preflight_command if kind == "preflight" else p.retest_command
        cmd = CommandSpec(id=f"{kind}_{p.id}", command=command, log_dir=p.preflight_log_dir, cwd=p.cwd or exp_dir)
        # Every probe/retest attempt spends tokens too; keep them for spend.py.
        return execute([cmd], probe_config, capture_dir=capture, on_event=_usage_recorder(status, p.id)).results[0]

    # --- Probes and retests, side by side ----------------------------------
    # Run probes side by side up to the samples the batch keeps in flight
    # (max_parallel × max_connections), so preflight never holds more
    # sandboxes than the main run will. One at a time, a round's eleven probes
    # once took 75 minutes. A probe is one sample, or one per pinned id; a
    # retest three times that; each holds at most max_connections at once.
    # Retests run after every probe has finished.
    conc = status.data["concurrency"]
    in_flight = max(1, conc["max_parallel"] * conc["max_connections"])

    def width(samples_each: int) -> int:
        """How many probes (or retests) of this many samples fit in flight at once."""
        return max(1, in_flight // max(1, min(samples_each, conc["max_connections"])))

    probe_width = width(max((p.probe_samples for p in pairs), default=1))
    retest_width = width(max((p.retest_samples for p in pairs), default=3))
    # The probes are real samples in real sandboxes: measure them for sizing
    sampler = SandboxSampler(exp_dir).start()
    with ThreadPoolExecutor(max_workers=min(probe_width, max(1, len(pairs)))) as pool:
        probes = dict(zip([p.id for p in pairs], pool.map(lambda p: run_probe(p, "preflight"), pairs)))

    # Read each successful probe's log now: a retest overwrites it.
    shapes: dict[str, dict[str, Any] | Exception | None] = {}
    for p in pairs:
        r = probes[p.id]
        if r is not None and r.status == "success" and r.log_path:
            try:
                shapes[p.id] = sample_shape(r.log_path)
            except Exception as e:
                shapes[p.id] = e

    # A sample error on the single probe earns the --limit 3 retest.
    to_retest = [p for p in pairs if isinstance(shapes.get(p.id), dict) and shapes[p.id]["sample_errors"] > 0]
    with ThreadPoolExecutor(max_workers=max(1, min(retest_width, len(to_retest)))) as pool:
        retests = dict(zip([p.id for p in to_retest], pool.map(lambda p: run_probe(p, "retest"), to_retest)))
    with status.lock:
        conc["measured"] = sampler.stop()

    # --- Outcomes, recorded in the pairs' original order --------------------
    surviving: list[PairPlan] = []
    durations: list[float] = []

    def cut_off(result: EvalResult | None) -> bool:
        """Whether a stop came before this probe or retest ran, or interrupted it."""
        return result is None or result.status == "interrupted"

    for p in pairs:
        r = probes[p.id]
        if cut_off(r) or (p.id in retests and cut_off(retests[p.id])):
            # A stop says nothing about the eval: record no verdict, so the
            # pair is neither excluded as broken nor left mid-preflight
            status.pair(p.id)["state"] = "interrupted"
            status.save()
            continue
        entry: dict[str, Any] = {
            "outcome": None,
            "process_retries": r.process_retries,
            "duration_seconds": r.duration_seconds,
            "retest": None,
            "evidence": None,
        }
        # A stalled probe's duration is just the time limit, not its speed
        if r.duration_seconds and r.error_class != "stalled":
            durations.append(float(r.duration_seconds))

        if r.status == "success":
            shape = shapes.get(p.id)
            if isinstance(shape, Exception):
                entry["evidence"] = {"note": f"probe log unreadable: {shape}"}
            if p.id in retests:
                rr = retests[p.id]
                try:
                    rshape = sample_shape(rr.log_path) if rr.log_path else {"samples": 0, "sample_errors": 0}
                except Exception:
                    rshape = {"samples": 0, "sample_errors": 0}
                n, errs = rshape["samples"], rshape["sample_errors"]
                creds = rshape.get("credential_sample_errors", 0)
                entry["retest"] = {
                    "samples": n,
                    "sample_errors": errs,
                    "credential_sample_errors": creds,
                    "process_retries": rr.process_retries,
                }
                if rr.error_class == "stalled":
                    # The probe answered but its retest did not: the provider
                    # stopped answering in between, which no sample count shows
                    entry["outcome"] = "excluded-stalled"
                    entry["evidence"] = {
                        "error_class": "stalled",
                        "command": p.retest_command,
                        "limit_seconds": probe_config.command_timeout,
                    }
                elif n and errs >= n:
                    entry["outcome"] = "excluded-all-samples-deterministic"
                    # A cap hit during preflight looks "deterministic" too;
                    # the tag stops it being read as a task bug.
                    tag = f" ({creds} {CREDENTIALS_TAG})" if creds else ""
                    entry["evidence"] = {"retest_errors": f"{errs} of {n} samples errored in the --limit 3 retest{tag}"}
                elif errs == 0:
                    entry["outcome"] = "sample-transient"
                else:
                    entry["outcome"] = "sample-deterministic"
            else:
                entry["outcome"] = "passed-after-retries" if r.process_retries else "passed"
        else:
            cls = r.error_class or "structural"
            entry["outcome"] = {"routing": "excluded-routing", "stalled": "excluded-stalled"}.get(cls, "excluded-structural")
            entry["evidence"] = {
                "error_class": cls,
                "exit_code": next((e.split("=", 1)[1] for e in r.errors if e.startswith("exit_code=")), None),
                "command": p.preflight_command,
                "output_tail": next((e for e in r.errors if e.startswith("[")), "") ,
                # An eval can exit 0 and still leave a failed log; its own
                # (already scrubbed) error is then the only account of why.
                "log_status": r.status,
                "log_error": next((e for e in r.errors if not e.startswith(("[", "exit_code="))), None),
            }
            if cls == "stalled":
                entry["evidence"]["limit_seconds"] = probe_config.command_timeout

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
    status.data["pid_start"] = _pid_start_time(os.getpid())
    with open(os.path.join(run_dir, "plan.json")) as f:
        plan = json.load(f)
    pairs = [PairPlan(**p) for p in plan["pairs"]]
    config = ExecutionConfig(**plan["execution"])
    exp_dir = status.data["experiment_dir"]
    start = time.monotonic()

    try:
        # The launcher checked for Inspect, but the worker can still lack it: a
        # runner started under `uv run --with inspect_ai` hands the worker a
        # temporary environment that uv deletes as soon as the launcher exits.
        # Without this, every eval would run and then "fail" its log check.
        if importlib.util.find_spec("inspect_ai") is None:
            raise RuntimeError(
                "inspect_ai is not importable in the worker: launch the runner with "
                "`uv run python scripts/execute_evals.py` from the eval's project, "
                "not from a temporary `uv run --with` environment"
            )

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

        commands = [CommandSpec(id=p.id, command=p.command, log_dir=p.log_dir, cwd=p.cwd or exp_dir) for p in to_run]
        for p in to_run:
            Path(p.log_dir).mkdir(parents=True, exist_ok=True)

        def on_event(kind: str, payload: Any) -> None:
            with status.lock:
                _record_event(kind, payload)

        def _record_event(kind: str, payload: Any) -> None:
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
                if r.model_usage:
                    e.setdefault("usage_attempts", []).append(r.model_usage)
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
                        # Sorted, since parallel pairs finish in any order
                        status.data["zero_sample_pairs"] = sorted(status.data["zero_sample_pairs"] + [r.id])
            elif kind == "reduction":
                status.data["concurrency"]["reductions"].append(asdict(payload))
            elif kind == "launch":
                status.data["concurrency"]["auto"]["launches"].append({"pair": payload["id"], **sizing, "conditions_at_once": payload["width"], "connections": payload["connections"]})
            status.save()

        # --- Automatic sizing ----------------------------------------------
        # Only when neither knob was set by the request or the Executor: then
        # every condition start re-asks the machine's capacity and how many
        # runs share it, so an older run shrinks when a newer one arrives
        conc = status.data["concurrency"]
        size_launch = None
        sizing: dict[str, int] = {}
        if conc.get("max_parallel_source") == "runner default" and conc.get("max_connections_source") == "runner default":
            capacity, basis = machine_capacity(conc.get("measured"), (conc.get("resources") or {}).get("nproc"), bool(conc.get("sandbox_detected")))
            conc["auto"] = {"capacity": capacity, "basis": basis, "launches": []}
            registry_entry = register_live_run(run_dir)

            def size_launch(unfinished: int) -> tuple[int, int]:
                live = count_live_runs()
                n = target_in_flight(capacity, live)
                sizing.update(live_runs=live, in_flight=n)
                return split_in_flight(n, unfinished)

        # Samples finished so far, for the lead's status view and the panel's
        # run tab, which would otherwise see a condition's count only at its end
        stop_counting = threading.Event()
        counter = threading.Thread(target=watch_live_counts, args=(status, stop_counting), daemon=True)
        counter.start()
        try:
            report = execute(commands, config, capture_dir=os.path.join(_quarantine_dir(run_dir), "eval_output"), on_event=on_event, size_launch=size_launch)
        finally:
            stop_counting.set()
            counter.join()
            if size_launch is not None:
                unregister_live_run(registry_entry)
        status.data["errors"].extend(report.errors)
        status.data["state"] = "interrupted" if report.status == "interrupted" else "completed"
        # Anything still pending (e.g. excluded before launch) keeps its state.
    except Exception as e:  # never leave a run without a terminal state
        status.data["errors"].append(f"runner exception: {type(e).__name__}: {e}")
        status.data["state"] = "failed"
    finally:
        # Every eval has ended by now, so containers still up from this
        # experiment are leftovers of one that was killed
        try:
            status.data["sandbox_cleanup"] = remove_leftover_sandboxes(exp_dir, run_dir)
        except Exception as e:
            status.data["errors"].append(f"leftover sandbox cleanup failed: {type(e).__name__}: {e}")
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

    # The worker reads every eval log with Inspect to confirm it succeeded;
    # without it each eval "fails" after running, and preflight excludes all.
    if importlib.util.find_spec("inspect_ai") is None:
        return "", ["inspect_ai is not importable here: launch the runner with `uv run python scripts/execute_evals.py` from the eval's project, not with `--no-project`"]

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
        preflight_timeout=int(overrides.get("preflight_timeout_seconds", DEFAULT_PREFLIGHT_TIMEOUT)),
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
    # Record the pid (and its start time, the guard against pid reuse) in a
    # file the worker never writes: touching status.json here would race the
    # worker's own first save. Format: "<pid> <start-time>".
    with open(os.path.join(run_dir, "worker.pid"), "w") as f:
        f.write(f"{proc.pid} {_pid_start_time(proc.pid) or ''}".strip())
    return proc.pid


def _worker_pid_record(data: dict[str, Any]) -> tuple[int | None, str | None]:
    """The worker's pid and start time: from the status file once the worker has
    saved, else from worker.pid.

    Parameters
    ----------
    data : dict
        Contents of ``status.json``.

    Returns
    -------
    tuple[int | None, str | None]
        ``(pid, start_time)``; the start time is None when unrecorded.
    """
    if data.get("pid"):
        return int(data["pid"]), data.get("pid_start")
    try:
        with open(os.path.join(data.get("run_dir", ""), "worker.pid")) as f:
            parts = f.read().split()
    except OSError:
        return None, None
    try:
        pid = int(parts[0]) if parts else None
    except ValueError:
        return None, None
    return pid or None, (parts[1] if len(parts) > 1 else None)


def _worker_pid(data: dict[str, Any]) -> int | None:
    """The worker's pid (see `_worker_pid_record`)."""
    return _worker_pid_record(data)[0]


def _worker_alive(data: dict[str, Any]) -> bool:
    """Whether the recorded worker process (not a pid-reusing stranger) runs."""
    return _pid_alive(*_worker_pid_record(data))


def _worker_dead(data: dict[str, Any]) -> bool:
    """True when the run is not terminal and its worker process is gone."""
    if data.get("state") in TERMINAL_STATES:
        return False
    return _worker_pid(data) is not None and not _worker_alive(data)


def _pid_start_time(pid: int | None) -> str | None:
    """The process's start time in clock ticks since boot, or None.

    Field 22 of ``/proc/<pid>/stat``, counted after the last ")" because the
    command name in field 2 may contain spaces. Mirrors
    `launch_subagent._pid_start_time`.

    Parameters
    ----------
    pid : int | None
        A process id.

    Returns
    -------
    str | None
        e.g. ``"1234567"``; None where ``/proc`` is unavailable.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def _pid_alive(pid: int | None, start_time: str | None = None) -> bool:
    """True if `pid` exists, is not a zombie, and is the process recorded.

    `os.kill(pid, 0)` succeeds on a zombie (a dead child nobody has reaped),
    so the process state is read from /proc where available. When a start
    time was recorded and the live process's differs, the pid was reused by
    an unrelated process (after a host restart, say) and the worker is dead.

    Parameters
    ----------
    pid : int | None
        A process id.
    start_time : str | None
        The start time recorded with it (`_pid_start_time`), if any.

    Returns
    -------
    bool
        Whether the recorded process is still running.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if start_time is not None:
        current = _pid_start_time(pid)
        if current is not None and current != start_time:
            return False
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
            concurrency: max_parallel=2 (runner default) max_connections=5 (runner default)
            preflight: done — 2 passed, 0 excluded
            pairs:
              c_0e4d / openai/gpt-5   running   samples -/-   attempts 1
            zero-sample pairs: none
    """
    pid = _worker_pid(data)
    alive = _worker_alive(data)
    state = data.get("state")
    if _worker_dead(data):
        state = f"{state} — WORKER DIED (pid {pid} not alive, no terminal state written; check worker.log)"
    lines = [f"run: {data.get('run_id')}  state: {state}  pid: {pid} ({'alive' if alive else 'not running'})"]
    lines.append(f"started: {data.get('started_at')}  updated: {data.get('updated_at')}  finished: {data.get('finished_at')}")
    c = data.get("concurrency", {})
    last = ((c.get("auto") or {}).get("launches") or [None])[-1]
    if last:
        # Automatic sizing: the latest condition start says where it stands
        lines.append(
            f"concurrency: automatic, capacity {c['auto']['capacity']}; latest start {last['in_flight']} in flight "
            f"({last['conditions_at_once']} at once × {last['connections']}, live runs {last['live_runs']})"
        )
    else:
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
        if e.get("credential_sample_errors"):
            extra += f"  {CREDENTIALS_TAG} {e['credential_sample_errors']} sample error(s)"
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


def _cleanup_notes(cleanup: dict[str, Any]) -> list[str]:
    """Report notes for `remove_leftover_sandboxes`'s result.

    Parameters
    ----------
    cleanup : dict
        ``{"removed": [...], "failed": [...], "skipped": reason | None}``.

    Returns
    -------
    list[str]
        One line each for what was removed, what could not be, and why the
        check was skipped; empty when nothing was left behind.
    """
    notes = []
    if cleanup.get("removed"):
        notes.append(f"Removed {len(cleanup['removed'])} sandbox container set(s) left running by killed evals: {', '.join(cleanup['removed'])}.")
    if cleanup.get("failed"):
        notes.append(f"Could not remove {len(cleanup['failed'])} leftover sandbox container set(s): {', '.join(cleanup['failed'])}; they are still running.")
    if cleanup.get("skipped"):
        notes.append(f"Leftover sandbox check skipped: {cleanup['skipped']}.")
    return notes


def _merge_cleanup(earlier: dict[str, Any] | None, again: dict[str, Any]) -> dict[str, Any]:
    """Combine the worker's clean-up record with a second pass made after it.

    Parameters
    ----------
    earlier : dict | None
        The worker's ``sandbox_cleanup`` record; None when it never wrote one
        (killed before its ``finally``).
    again : dict
        The second pass's record, in the same shape.

    Returns
    -------
    dict
        ``{"removed", "failed", "skipped"}``: everything either pass removed;
        failures and the skip reason from the latest pass that ran, e.g. a
        worker that removed two projects followed by a pass that found none
        gives ``{"removed": ["inspect-a", "inspect-b"], "failed": [], "skipped": None}``.
    """
    if not earlier:
        return again
    ran_again = again["skipped"] is None
    return {
        "removed": sorted(set(earlier.get("removed") or []) | set(again["removed"])),
        "failed": again["failed"] if ran_again else list(earlier.get("failed") or []),
        # Skipped only if neither pass got to look
        "skipped": again["skipped"] if earlier.get("skipped") and not ran_again else None,
    }


def stop_run(run_dir: str, timeout: float = 300.0, docker: Any = None) -> dict[str, Any]:
    """Stop a run cleanly: its evals get to remove their own containers first.

    Signals the worker, which asks each running eval to cancel (SIGINT, the
    signal Inspect cleans up on), waits for the run to reach a terminal state,
    then removes any of this experiment's containers still left. Killing the
    processes directly instead leaves every sandbox running.

    Parameters
    ----------
    run_dir : str
        The run directory.
    timeout : float
        Seconds to wait for the worker to finish stopping.
    docker : Callable
        Runs a docker command; None for ``_docker``. Tests pass a stand-in.

    Returns
    -------
    dict
        The run's status data, with ``sandbox_cleanup`` filled in.
    """
    data = Status.load(run_dir).data
    if data.get("state") not in TERMINAL_STATES and _worker_alive(data):
        try:
            os.kill(int(_worker_pid(data)), signal.SIGTERM)
        except ProcessLookupError:
            pass
        data = wait_for(run_dir, timeout, interval=5.0)

    # The worker cleans up as it exits; this catches a worker that was killed
    # or never got that far. Containers already removed are simply not found,
    # so the worker's own record of what it removed is kept alongside.
    if data.get("state") in TERMINAL_STATES or not _worker_alive(data):
        status = Status.load(run_dir)
        again = remove_leftover_sandboxes(status.data["experiment_dir"], run_dir, docker)
        status.data["sandbox_cleanup"] = _merge_cleanup(status.data.get("sandbox_cleanup"), again)
        if status.data.get("state") not in TERMINAL_STATES:
            status.data["state"] = "interrupted"
            status.data["finished_at"] = _now()
        status.save()
        data = status.data
    return data


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
    structural = [e for e in failed if e.get("error_class") in ("structural", "unknown", "resource_contention", "transient", "stalled", None)]
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
            "Sample-level attrition: "
            + "; ".join(
                f"{e['condition']} / {e['model']} {e['sample_errors']} of {e['samples_completed']}"
                + (f" ({e['credential_sample_errors']} {CREDENTIALS_TAG})" if e.get("credential_sample_errors") else "")
                for e in sample_err
            )
            + "."
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
                if (entry.get("retest") or {}).get("credential_sample_errors"):
                    out.append(f"  - **{CREDENTIALS_TAG} Credentials exhausted**: {_CREDENTIALS_NOTE}")
                out.append(f"  - **Evidence**: {ev.get('retest_errors')}; sample error traces are in the quarantined preflight logs under the run directory's `private/preflight_logs/`.")
            elif outcome == "excluded-stalled":
                minutes = round((ev.get("limit_seconds") or DEFAULT_PREFLIGHT_TIMEOUT) / 60)
                out.append("  - **Reason**: `provider-not-responding (stalled)`")
                out.append(
                    f"  - **Evidence**: the preflight probe was still running after its {minutes}-minute limit "
                    "and was stopped, without retries. A single sample normally takes a few minutes at most, "
                    "so the model's provider is most likely not responding (a sandbox that never starts looks "
                    "the same). Try again later, re-pin the provider, or drop the model; "
                    "`preflight_timeout_seconds` in `overrides` lengthens the limit if this model is genuinely slow."
                )
                out.append(f"  - **Command**: `{ev.get('command')}`")
            else:
                reason = "pinned-provider-unavailable (routing)" if outcome == "excluded-routing" else "structural"
                out.append(f"  - **Reason**: `{reason}`")
                if CREDENTIALS_TAG in (ev.get("output_tail") or ""):
                    out.append(f"  - **{CREDENTIALS_TAG} Credentials exhausted**: {_CREDENTIALS_NOTE}")
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
        if CREDENTIALS_TAG in tail:
            out.append(f"  - **{CREDENTIALS_TAG} Credentials exhausted**: {_CREDENTIALS_NOTE}")
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
        # A cap reached mid-eval under --no-fail-on-error errors the remaining
        # samples instead of failing the process; those samples are not data.
        creds = e.get("credential_sample_errors")
        if creds:
            out.append(
                f"  - **{CREDENTIALS_TAG} Credentials exhausted** ({creds} of the {e.get('sample_errors')} "
                f"errored samples): {_CREDENTIALS_NOTE} These samples are not valid data; re-run the pair "
                "once credit is restored."
            )
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

    # --- Models as Served --------------------------------------------------
    out.append("### Models as Served")
    out.append("")
    out.append("| Condition | Model | Served as |")
    out.append("|---|---|---|")
    any_rows = False
    for e in ran:
        if "served_models" not in e:
            continue
        any_rows = True
        served = ", ".join(f"`{m}`" for m in e["served_models"]) or "not reported by the provider"
        out.append(f"| {e['condition']} | {e['model']} | {served} |")
    if not any_rows:
        out.append("| — | — | — |")
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
    auto = c.get("auto")
    launches = (auto or {}).get("launches") or []
    if auto:
        widest = max((l["conditions_at_once"] for l in launches), default=c.get("max_parallel"))
        out.append(f"- **Chosen `max_parallel`**: {widest} (automatic; at most this many conditions at once)")
    else:
        out.append(f"- **Chosen `max_parallel`**: {c.get('max_parallel')}")
    out.append(f"- **Orchestrator override provided**: {override}")
    out.append(f"- **Resources observed**: nproc={res.get('nproc')}, free_mem_mb={res.get('free_mem_mb')}")
    sd = c.get("sandbox_detected")
    out.append(f"- **Sandbox detected**: {'yes' if sd else 'no' if sd is False else 'unknown'} (evidence: {c.get('sandbox_evidence') or 'none recorded'})")
    if auto:
        m = c.get("measured") or {}
        measured = (f"heaviest of {m.get('containers_seen')} preflight sandboxes {m.get('peak_container_mb')} MB, lowest free memory {m.get('min_free_mb')} MB"
                    if m.get("containers_seen") else "no preflight sandboxes measured")
        starts = "; ".join(f"{l['pair']}: {l['in_flight']} in flight, {l['conditions_at_once']} at once × {l['connections']} (live runs {l['live_runs']})" for l in launches) or "none"
        out.append(f"- **Automatic sizing**: capacity {auto['capacity']}, set by {auto['basis']}; {measured}. Condition starts: {starts}")
    if c.get("reductions"):
        out.append("- **Reductions applied during execution**: " + "; ".join(f"launch {r['batch']}: {r['previous_level']}→{r['new_level']} ({r['reason']})" for r in c["reductions"]))
    else:
        out.append("- **Reductions applied during execution**: none")
    if auto:
        out.append("- **Rationale**: no override was requested, so the runner sized each condition start from preflight's sandbox measurements and the number of live runs on the machine, split evenly between them.")
    elif c.get("max_parallel_source") == "cli override":
        out.append(f"- **Rationale**: Executor raised the runner ceiling via CLI override — {c.get('rationale')}")
    elif "request overrides" in (c.get("max_parallel_source"), c.get("max_connections_source")):
        out.append(f"- **Rationale**: the request's overrides were applied as given, so automatic sizing was off; any key it left unset kept the runner's starting value (max_parallel={DEFAULT_MAX_PARALLEL}, max_connections={DEFAULT_MAX_CONNECTIONS}).")
    else:
        out.append(f"- **Rationale**: the runner's starting values (max_parallel={DEFAULT_MAX_PARALLEL}, max_connections={DEFAULT_MAX_CONNECTIONS}); no override was requested.")
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
    notes.extend(_cleanup_notes(data.get("sandbox_cleanup") or {}))
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

    p_stop = sub.add_parser("stop", help="stop a run cleanly, removing the sandbox containers it leaves")
    p_stop.add_argument("run_dir")
    p_stop.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the evals to stop (default 300)")

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

    if args.cmd == "stop":
        data = stop_run(args.run_dir, args.timeout)
        cleanup = data.get("sandbox_cleanup") or {}
        print(f"state: {data.get('state')}")
        print(f"leftover sandboxes removed: {', '.join(cleanup.get('removed') or []) or 'none'}")
        if cleanup.get("failed"):
            print(f"could not remove: {', '.join(cleanup['failed'])}")
        if cleanup.get("skipped"):
            print(f"cleanup skipped: {cleanup['skipped']}")
        return 0 if data.get("state") in TERMINAL_STATES else 3

    if args.cmd == "_worker":
        return run_worker(args.run_dir)

    return 2


if __name__ == "__main__":
    sys.exit(main())
