"""Detached launcher for Prism sub-agents.

Every sub-agent invocation goes through this script. It persists the request
JSON under ``investigations/<name>/requests/`` (not the session scratchpad,
which is wiped on restart), starts the sub-agent in its own session so it
survives the caller's teardown, names every output per invocation so repeated
runs of one role never collide, and records a receipt the caller can poll.

Usage
-----
    uv run python scripts/launch_subagent.py launch <role> <request.json> \\
        --investigation investigations/<name> --tag <label> [--artefacts-dir <dir>] [--cwd <dir>]
    uv run python scripts/launch_subagent.py status <receipt.json>
    uv run python scripts/launch_subagent.py wait <receipt.json> [--timeout S]
    uv run python scripts/launch_subagent.py list <investigation-dir> [--unfinished]

``status`` and ``wait`` exit 0 when the receipt is terminal, 3 while the
sub-agent runs, and 4 when its process died without recording an exit (also
terminal: stop polling). Sub-agents run on the pinned
``CLAUDE_AGENT_SDK_REQUIREMENT``.

Roles: ``explorer``, ``executor``, ``analyst``, ``mapper``, ``designer``,
``critic``. ``--tag`` is the iteration or phase label (``iter_00``,
``iter_01``, ``phase4``) — keep it neutral, since it is written into every
output filename below; ``<n>`` is assigned automatically so the outputs are::

    <artefacts-dir>/<role>/report_<tag>_<role>_<n>.md     the sub-agent's stdout
    <artefacts-dir>/<role>/stderr_<tag>_<role>_<n>.log    its stderr
    <artefacts-dir>/<role>/cost_<tag>_<role>_<n>.json     SDK usage/cost sidecar
    <artefacts-dir>/<role>/receipt_<tag>_<role>_<n>.json  pid, paths, exit code
    <investigation>/requests/<tag>_<role>_<n>.json        the request, as sent

``--artefacts-dir`` defaults to ``<investigation>/artefacts``; pass the
iteration's ``artefacts/`` for iteration-scoped roles.

A run that fails because a provider refused on credit or a usage cap carries
``failure_class: credentials_exhausted`` and ``failure_route`` in its receipt
(from the runner's cost sidecar, or the marker in its stderr). ``list
--unfinished`` prints every invocation not yet cleanly finished with a
ready-to-run relaunch command — the mechanical half of a ``RESUME.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Unresolved path on purpose: in an installed target this file is a symlink
# into the scaffold repo, and the target root is where `subagents/` lives.
_SCRIPT = os.path.abspath(__file__)
TARGET_ROOT = os.path.dirname(os.path.dirname(_SCRIPT))

ROLES: dict[str, str] = {
    "explorer": "environment_explorer",
    "executor": "experiment_executor",
    "analyst": "transcript_analyst",
    "mapper": "environment_mapper",
    "designer": "variant_designer",
    "critic": "redteam_critic",
}

# Environment variable through which `subagents/runner.py` learns where to
# write its cost sidecar for this invocation.
COST_FILE_ENV = "PRISM_COST_FILE"

# The marker `subagents/runner.py` puts in a credential-exhaustion error, which
# the CLI prints to stderr as "Agent error: <agent>: credentials_exhausted
# (route=openrouter, model=…): …". Used when no cost sidecar recorded the class.
CREDENTIALS_MARKER = re.compile(r"credentials_exhausted \(route=(?P<route>[A-Za-z_-]+)")

# The Agent SDK version every sub-agent runs on: the one the test suite runs
# against. Pinned because the runner's isolation options (`tools`, `skills`,
# `setting_sources`, `settings`) and the CLI behaviour behind them — no
# CLAUDE.md, no auto-memory, no user plugins or hooks — were verified on this
# release; an unpinned install could silently change what a sub-agent sees.
# Move it deliberately, re-running the tests (`tests/test_runner_isolation.py`).
CLAUDE_AGENT_SDK_REQUIREMENT = "claude-agent-sdk==0.2.165"

# Exit codes of `status` / `wait`: 0 = terminal receipt (finished or failed to
# start), 3 = still running, 4 = the process died without recording an exit
# (supervisor killed, host restart) — terminal, so a caller polling `wait`
# must stop and treat the invocation as failed rather than loop.
EXIT_TERMINAL, EXIT_RUNNING, EXIT_DEAD = 0, 3, 4

# Request keys that select *how* a sub-agent runs rather than *what* it is asked
# to do. A relaunch that changed only these (a route substitution, a re-ask
# carrying `conformance_defects` + `previous_report`) supersedes the failed
# original.
INFRA_REQUEST_KEYS = ("subagent_", "conformance_defects", "previous_report")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pid_start_time(pid: int | None) -> str | None:
    """The process's start time in clock ticks since boot, or None.

    Field 22 of ``/proc/<pid>/stat``. Recorded beside each pid so a later
    liveness check can tell the same process from an unrelated one that was
    given the recycled pid (after a host restart, say). The fields are counted
    after the last ")", because the command name in field 2 may contain spaces.

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
    return fields[19] if len(fields) > 19 else None  # field 22 = index 19 after ")"


def _pid_alive(pid: int | None, start_time: str | None = None) -> bool:
    """True if `pid` exists, is not a zombie, and is the process recorded.

    Parameters
    ----------
    pid : int | None
        A process id from a receipt.
    start_time : str | None
        The start time recorded with it (`_pid_start_time`); when given and
        the live process's differs, the pid has been reused and the recorded
        process is dead. Mirrors `execute_evals._pid_alive`.

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


def _receipt_alive(receipt: dict[str, Any]) -> bool:
    """Whether the sub-agent or its supervisor named in a receipt is running."""
    return _pid_alive(receipt.get("pid"), receipt.get("pid_start")) or _pid_alive(
        receipt.get("supervisor_pid"), receipt.get("supervisor_start")
    )


def _write_json_atomic(path: str, obj: Any) -> None:
    """Write JSON so a concurrent reader sees the old file or the new, never half.

    Parameters
    ----------
    path : str
        Destination path.
    obj : Any
        JSON-serialisable content.
    """
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# How long a receipt may sit in `launching` with no pid recorded before it is
# treated as dead: the supervisor writes its pid on its first save, which
# follows the sub-agent's Popen.
LAUNCH_GRACE_SECONDS = 60.0


def reserve_index(role_dir: str, tag: str, role: str) -> int:
    """Claim the smallest free ``n`` by creating its receipt file exclusively.

    ``O_CREAT | O_EXCL`` makes the check and the claim one atomic step, so two
    launches of the same role and tag at the same moment can never both take
    ``n`` and overwrite each other's request, report and receipt. The empty
    file is replaced by the real receipt immediately afterwards.

    Parameters
    ----------
    role_dir : str
        ``<artefacts-dir>/<role>``.
    tag : str
        The invocation tag.
    role : str
        The role name.

    Returns
    -------
    int
        The reserved index.
    """
    n = 1
    while True:
        path = os.path.join(role_dir, f"receipt_{tag}_{role}_{n}.json")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return n
        except FileExistsError:
            n += 1


def build_command(role: str, request_path: str, cwd: str | None) -> list[str]:
    """The sub-agent CLI invocation from `subagent_invocation.md §General Pattern`."""
    main_py = os.path.join(TARGET_ROOT, "subagents", ROLES[role], "main.py")
    cmd = [
        "uv", "run", "--with", CLAUDE_AGENT_SDK_REQUIREMENT, "--with", "python-dotenv",
        "python", main_py, request_path,
    ]
    if cwd:
        cmd += ["--cwd", os.path.abspath(cwd)]
    return cmd


def launch(
    role: str,
    request_path: str,
    investigation: str,
    tag: str,
    artefacts_dir: str | None = None,
    cwd: str | None = None,
    entry: list[str] | None = None,
) -> dict[str, Any]:
    """Persist the request, start the sub-agent detached, and return the receipt.

    Parameters
    ----------
    role : str
        One of `ROLES`.
    request_path : str
        The request JSON to send (copied into ``requests/``; the copy is what
        the sub-agent reads).
    investigation : str
        ``investigations/<name>`` directory (created if missing).
    tag : str
        Iteration or phase label used in every output name.
    artefacts_dir : str | None
        Where the role's outputs go; defaults to ``<investigation>/artefacts``.
    cwd : str | None
        ``--cwd`` for the sub-agent (defaults to the target root).
    entry : list[str] | None
        Test hook: replaces the sub-agent command line.

    Returns
    -------
    dict
        The receipt, also written to ``receipt_<tag>_<role>_<n>.json``.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
    if not os.path.isfile(request_path):
        raise ValueError(f"request file not found: {request_path}")
    with open(request_path) as f:
        json.load(f)  # fail early on malformed JSON

    investigation = os.path.abspath(investigation)
    artefacts_dir = os.path.abspath(artefacts_dir or os.path.join(investigation, "artefacts"))
    requests_dir = os.path.join(investigation, "requests")
    role_dir = os.path.join(artefacts_dir, role)
    os.makedirs(requests_dir, exist_ok=True)
    os.makedirs(role_dir, exist_ok=True)

    n = reserve_index(role_dir, tag, role)
    stem = f"{tag}_{role}_{n}"
    persisted_request = os.path.join(requests_dir, f"{stem}.json")
    if os.path.abspath(request_path) != persisted_request:
        shutil.copy(request_path, persisted_request)

    receipt_path = os.path.join(role_dir, f"receipt_{stem}.json")
    receipt: dict[str, Any] = {
        "role": role,
        "tag": tag,
        "n": n,
        "request": persisted_request,
        "report": os.path.join(role_dir, f"report_{stem}.md"),
        "stderr": os.path.join(role_dir, f"stderr_{stem}.log"),
        "cost": os.path.join(role_dir, f"cost_{stem}.json"),
        "receipt": receipt_path,
        "command": entry or build_command(role, persisted_request, cwd),
        "cwd": os.path.abspath(cwd) if cwd else TARGET_ROOT,
        "state": "launching",
        "pid": None,
        "pid_start": None,
        "supervisor_pid": None,
        "supervisor_start": None,
        "exit_code": None,
        "failure_class": None,
        "failure_route": None,
        "started_at": _now(),
        "finished_at": None,
    }
    _write_json_atomic(receipt_path, receipt)

    # The supervisor (this script, `_supervise`) runs the sub-agent as its child
    # so the exit code can be recorded — a fully detached grandchild could not
    # be reaped later. `start_new_session` gives it its own session and process
    # group: the caller's teardown never reaches it.
    sup = subprocess.Popen(
        [sys.executable, _SCRIPT, "_supervise", receipt_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    # The supervisor records its own pid in the receipt; writing it here would
    # race the supervisor's first save and could clobber the child's pid.
    receipt["supervisor_pid"] = sup.pid
    return receipt


def supervise(receipt_path: str) -> int:
    """Run the sub-agent described by a receipt and record its outcome."""
    with open(receipt_path) as f:
        receipt = json.load(f)
    env = dict(os.environ)
    env[COST_FILE_ENV] = receipt["cost"]
    receipt["supervisor_pid"] = os.getpid()
    receipt["supervisor_start"] = _pid_start_time(os.getpid())

    def save() -> None:
        _write_json_atomic(receipt_path, receipt)

    # A launch that cannot even start (bad cwd, missing interpreter) must
    # still leave a terminal receipt, or a caller polling `wait` is stranded.
    try:
        with open(receipt["report"], "wb") as out, open(receipt["stderr"], "ab") as err:
            proc = subprocess.Popen(
                receipt["command"],
                cwd=receipt["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
            )
            receipt["pid"] = proc.pid
            receipt["pid_start"] = _pid_start_time(proc.pid)
            receipt["state"] = "running"
            save()
            code = proc.wait()
    except Exception as exc:
        receipt["state"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["finished_at"] = _now()
        save()
        try:
            with open(receipt["stderr"], "a") as err:
                err.write(f"[launcher] could not start sub-agent: {receipt['error']}\n")
        except OSError:
            pass
        return 2
    receipt["exit_code"] = code
    receipt["state"] = "finished"
    receipt["finished_at"] = _now()
    if code != 0:
        receipt["failure_class"], receipt["failure_route"] = failure_from_outputs(receipt)
    save()
    return code


def failure_from_outputs(receipt: dict[str, Any]) -> tuple[str | None, str | None]:
    """Recover a failed run's failure class and route from its outputs.

    Parameters
    ----------
    receipt : dict
        The run's receipt (paths to its cost sidecar and stderr log).

    Returns
    -------
    tuple[str | None, str | None]
        ``(failure_class, route)`` — e.g. ``("credentials_exhausted",
        "direct")`` — or ``(None, None)`` when no class was recognised.
    """
    # The sidecar is structured, so prefer it; the stderr marker covers a run
    # whose sidecar could not be written.
    try:
        cost = load_receipt(receipt["cost"])
        if cost.get("failure_class"):
            return cost["failure_class"], cost.get("route")
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    try:
        with open(receipt["stderr"], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))  # the error is at the end
            tail = f.read().decode("utf-8", "replace")
    except (OSError, KeyError):
        return None, None
    m = CREDENTIALS_MARKER.search(tail)
    if m:
        return "credentials_exhausted", m.group("route")
    return None, None


def load_receipt(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def describe(receipt: dict[str, Any]) -> str:
    """One-screen status for a receipt (score-free: sizes and states only)."""
    alive = _pid_alive(receipt.get("pid"), receipt.get("pid_start")) or (
        receipt.get("state") == "launching"
        and _pid_alive(receipt.get("supervisor_pid"), receipt.get("supervisor_start"))
    )
    state = receipt.get("state")
    if state == "failed":
        state = f"failed to start ({receipt.get('error')})"
    elif state != "finished" and not alive:
        state = f"{state} — PROCESS DIED (no exit code recorded; check {receipt.get('stderr')})"
    size = os.path.getsize(receipt["report"]) if os.path.exists(receipt["report"]) else 0
    if receipt.get("failure_class"):
        state = f"{state} — {receipt['failure_class']} (route={receipt.get('failure_route')})"
    lines = [
        f"{receipt['role']} {receipt['tag']} #{receipt['n']}: {state}"
        + (f" exit={receipt['exit_code']}" if receipt.get("exit_code") is not None else ""),
        f"  pid: {receipt.get('pid')} ({'alive' if alive else 'not running'})  started: {receipt.get('started_at')}  finished: {receipt.get('finished_at')}",
        f"  report: {receipt['report']} ({size} bytes)",
        f"  stderr: {receipt['stderr']}",
        f"  request: {receipt['request']}",
    ]
    if os.path.exists(receipt["cost"]):
        try:
            cost = load_receipt(receipt["cost"])
            lines.append(
                f"  cost: ${cost.get('total_cost_usd')}  turns: {cost.get('num_turns')}  "
                f"stop: {cost.get('subtype')}"
            )
        except (OSError, json.JSONDecodeError):
            pass
    return "\n".join(lines)


def wait_for(receipt_path: str, timeout: float, interval: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        r = load_receipt(receipt_path)
        if r.get("state") in ("finished", "failed"):
            return r
        # Dead without a terminal receipt (supervisor killed, host restart):
        # report it as terminal rather than stranding the caller. A receipt
        # still in `launching` with no pid gets a grace period first.
        if not _receipt_alive(r):
            if r.get("state") == "launching" and not r.get("supervisor_pid"):
                try:
                    started = datetime.fromisoformat(r["started_at"])
                    age = (datetime.now(timezone.utc) - started).total_seconds()
                except (KeyError, ValueError):
                    age = LAUNCH_GRACE_SECONDS
                if age < LAUNCH_GRACE_SECONDS:
                    if time.monotonic() >= deadline:
                        return r
                    time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
                    continue
            return r
        if time.monotonic() >= deadline:
            return r
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def list_receipts(investigation: str) -> list[dict[str, Any]]:
    """Every receipt under an investigation (any depth), oldest first."""
    found: list[dict[str, Any]] = []
    for root, _dirs, files in os.walk(os.path.abspath(investigation)):
        for fn in files:
            if fn.startswith("receipt_") and fn.endswith(".json"):
                try:
                    found.append(load_receipt(os.path.join(root, fn)))
                except (OSError, json.JSONDecodeError):
                    continue
    return sorted(found, key=lambda r: (r.get("started_at") or "", r.get("role", ""), r.get("tag", ""), int(r.get("n") or 0)))


def _task_of(request_path: str) -> dict[str, Any] | None:
    """The request with its infrastructure keys removed (None if unreadable).

    Parameters
    ----------
    request_path : str
        A persisted request JSON.

    Returns
    -------
    dict | None
        The request minus `INFRA_REQUEST_KEYS`, for "same work?" comparison.
    """
    try:
        with open(request_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return {k: v for k, v in data.items() if not k.startswith(INFRA_REQUEST_KEYS)}


def relaunch_command(receipt: dict[str, Any]) -> list[str]:
    """The `launch` command that re-runs a receipt's persisted request.

    Parameters
    ----------
    receipt : dict
        A receipt written by `launch`.

    Returns
    -------
    list[str]
        argv for this script, e.g. ``["uv", "run", "python",
        "scripts/launch_subagent.py", "launch", "analyst",
        "<inv>/requests/iter_02_analyst_1.json", "--investigation", "<inv>",
        "--tag", "iter_02", "--artefacts-dir", "<inv>/iter_02/artefacts"]``.
    """
    request = receipt["request"]
    # Layout fixed by `launch`: <investigation>/requests/<stem>.json and
    # <artefacts-dir>/<role>/report_<stem>.md.
    investigation = os.path.dirname(os.path.dirname(request))
    artefacts_dir = os.path.dirname(os.path.dirname(receipt["report"]))
    cmd = [
        "uv", "run", "python", "scripts/launch_subagent.py", "launch", receipt["role"], request,
        "--investigation", investigation, "--tag", receipt["tag"], "--artefacts-dir", artefacts_dir,
    ]
    if receipt.get("cwd") and os.path.abspath(receipt["cwd"]) != TARGET_ROOT:
        cmd += ["--cwd", receipt["cwd"]]
    return cmd


def unfinished(investigation: str) -> list[dict[str, Any]]:
    """Invocations not yet cleanly finished, each with what to do about it.

    An invocation is finished cleanly when it exited 0. A failed or dead one
    is *superseded* — and so not listed — when a later invocation of the same
    role and tag asked for the same work (request equal up to
    `INFRA_REQUEST_KEYS`) and exited 0: that is the retry or route
    substitution that already recovered it.

    Parameters
    ----------
    investigation : str
        The ``investigations/<name>`` directory.

    Returns
    -------
    list[dict]
        One entry per unfinished invocation, oldest first: ``receipt``,
        ``alive`` (still running — let it finish, launch nothing over it),
        and ``relaunch`` (argv, None while alive).
    """
    receipts = list_receipts(investigation)
    clean = [
        r for r in receipts if r.get("state") == "finished" and r.get("exit_code") == 0
    ]
    out: list[dict[str, Any]] = []
    for r in receipts:
        if r in clean:
            continue
        task = _task_of(r["request"])
        # An unreadable request cannot be shown to be the same work, so it is
        # never superseded (None == None must not count as a match).
        superseded = task is not None and any(
            c["role"] == r["role"]
            and c["tag"] == r["tag"]
            and (c.get("started_at") or "") >= (r.get("started_at") or "")
            and c is not r
            and _task_of(c["request"]) == task
            for c in clean
        )
        if superseded:
            continue
        alive = r.get("state") not in ("finished", "failed") and _receipt_alive(r)
        out.append({"receipt": r, "alive": alive, "relaunch": None if alive else relaunch_command(r)})
    return out


def exit_code_for(receipt: dict[str, Any]) -> int:
    """The `status`/`wait` exit code for a receipt (see `EXIT_TERMINAL`).

    Parameters
    ----------
    receipt : dict
        The receipt as last read.

    Returns
    -------
    int
        0 when the receipt is terminal, 3 while the process runs, 4 when it
        died without recording an exit (a dead invocation is terminal: stop
        polling and handle it as a failure).
    """
    if receipt.get("state") in ("finished", "failed"):
        return EXIT_TERMINAL
    if receipt.get("state") == "launching" and not receipt.get("supervisor_pid"):
        # Inside the launch grace period nothing has recorded a pid yet.
        try:
            started = datetime.fromisoformat(receipt["started_at"])
            age = (datetime.now(timezone.utc) - started).total_seconds()
        except (KeyError, ValueError):
            age = LAUNCH_GRACE_SECONDS
        if age < LAUNCH_GRACE_SECONDS:
            return EXIT_RUNNING
    return EXIT_RUNNING if _receipt_alive(receipt) else EXIT_DEAD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch Prism sub-agents detached.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("launch")
    p.add_argument("role", choices=sorted(ROLES))
    p.add_argument("request")
    p.add_argument("--investigation", required=True, help="investigations/<name> directory")
    p.add_argument("--tag", required=True, help="neutral iteration/phase label, e.g. iter_00, iter_01, phase4")
    p.add_argument("--artefacts-dir", default=None, help="defaults to <investigation>/artefacts")
    p.add_argument("--cwd", default=None, help="--cwd for the sub-agent")

    p = sub.add_parser("status")
    p.add_argument("receipt")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("wait")
    p.add_argument("receipt")
    p.add_argument("--timeout", type=float, default=540.0)
    p.add_argument("--interval", type=float, default=15.0)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list")
    p.add_argument("investigation")
    p.add_argument(
        "--unfinished",
        action="store_true",
        help="only invocations not cleanly finished, each with a relaunch command",
    )

    p = sub.add_parser("_supervise", help=argparse.SUPPRESS)
    p.add_argument("receipt")

    args = parser.parse_args(argv)

    if args.cmd == "launch":
        try:
            receipt = launch(args.role, args.request, args.investigation, args.tag, args.artefacts_dir, args.cwd)
        except ValueError as e:
            print(json.dumps({"status": "error", "errors": [str(e)]}))
            return 1
        # The lead waits in the background (see subagent_invocation.md
        # §General Pattern), so the hint carries the long, cache-sparing
        # timeout; `next` stays a plain command so it can be run as given
        next_cmd = f"uv run python scripts/launch_subagent.py wait {receipt['receipt']} --timeout 3300"
        print(json.dumps({**receipt, "status": "launched", "next": next_cmd, "next_run_in_background": True}, indent=2))
        return 0
    if args.cmd == "status":
        r = load_receipt(args.receipt)
        print(json.dumps(r, indent=2) if args.json else describe(r))
        return exit_code_for(r)
    if args.cmd == "wait":
        r = wait_for(args.receipt, args.timeout, args.interval)
        print(json.dumps(r, indent=2) if args.json else describe(r))
        return exit_code_for(r)
    if args.cmd == "list" and args.unfinished:
        entries = unfinished(args.investigation)
        if not entries:
            print("no unfinished invocations")
        for e in entries:
            print(describe(e["receipt"]))
            if e["alive"]:
                print("  -> still running: let it finish; do not relaunch over it")
            else:
                print("  -> relaunch (from the target root): " + shlex.join(e["relaunch"]))
        return 0
    if args.cmd == "list":
        rs = list_receipts(args.investigation)
        if not rs:
            print("no receipts found")
        for r in rs:
            print(describe(r))
        return 0
    if args.cmd == "_supervise":
        return supervise(args.receipt)
    return 2


if __name__ == "__main__":
    sys.exit(main())
