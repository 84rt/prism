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

Roles: ``explorer``, ``executor``, ``analyst``, ``mapper``, ``designer``,
``critic``. ``--tag`` is the iteration or phase label (``00_resample``,
``iter1``, ``phase4``); ``<n>`` is assigned automatically so the outputs are::

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

# Request keys that select *how* a sub-agent runs rather than *what* it is asked
# to do. A relaunch that changed only these (a route substitution, a re-ask
# carrying `conformance_defects` + `previous_report`) supersedes the failed
# original.
INFRA_REQUEST_KEYS = ("subagent_", "conformance_defects", "previous_report")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pid_alive(pid: int | None) -> bool:
    """True if `pid` exists and is not a zombie (see execute_evals._pid_alive)."""
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


# How long a receipt may sit in `launching` with no pid recorded before it is
# treated as dead: the supervisor writes its pid on its first save, which
# follows the sub-agent's Popen.
LAUNCH_GRACE_SECONDS = 60.0


def next_index(role_dir: str, tag: str, role: str) -> int:
    """Smallest ``n`` for which no receipt ``receipt_<tag>_<role>_<n>.json`` exists."""
    n = 1
    while os.path.exists(os.path.join(role_dir, f"receipt_{tag}_{role}_{n}.json")):
        n += 1
    return n


def build_command(role: str, request_path: str, cwd: str | None) -> list[str]:
    """The sub-agent CLI invocation from `subagent_invocation.md §General Pattern`."""
    main_py = os.path.join(TARGET_ROOT, "subagents", ROLES[role], "main.py")
    cmd = [
        "uv", "run", "--with", "claude-agent-sdk", "--with", "python-dotenv",
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

    n = next_index(role_dir, tag, role)
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
        "supervisor_pid": None,
        "exit_code": None,
        "failure_class": None,
        "failure_route": None,
        "started_at": _now(),
        "finished_at": None,
    }
    with open(receipt_path, "w") as f:
        json.dump(receipt, f, indent=2)

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

    def save() -> None:
        with open(receipt_path, "w") as f:
            json.dump(receipt, f, indent=2)

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
    alive = _pid_alive(receipt.get("pid")) or (
        receipt.get("state") == "launching" and _pid_alive(receipt.get("supervisor_pid"))
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
        if not _pid_alive(r.get("pid")) and not _pid_alive(r.get("supervisor_pid")):
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
        "<inv>/requests/iter2_analyst_1.json", "--investigation", "<inv>",
        "--tag", "iter2", "--artefacts-dir", "<inv>/iter2/artefacts"]``.
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
        alive = r.get("state") not in ("finished", "failed") and (
            _pid_alive(r.get("pid")) or _pid_alive(r.get("supervisor_pid"))
        )
        out.append({"receipt": r, "alive": alive, "relaunch": None if alive else relaunch_command(r)})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch Prism sub-agents detached.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("launch")
    p.add_argument("role", choices=sorted(ROLES))
    p.add_argument("request")
    p.add_argument("--investigation", required=True, help="investigations/<name> directory")
    p.add_argument("--tag", required=True, help="iteration/phase label, e.g. 00_resample, iter1")
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
        print(json.dumps({**receipt, "status": "launched", "next": f"uv run python scripts/launch_subagent.py wait {receipt['receipt']} --timeout 540"}, indent=2))
        return 0
    if args.cmd == "status":
        r = load_receipt(args.receipt)
        print(json.dumps(r, indent=2) if args.json else describe(r))
        return 0 if r.get("state") in ("finished", "failed") else 3
    if args.cmd == "wait":
        r = wait_for(args.receipt, args.timeout, args.interval)
        print(json.dumps(r, indent=2) if args.json else describe(r))
        return 0 if r.get("state") in ("finished", "failed") else 3
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
