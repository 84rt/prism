"""Stopping eval runs without leaving their sandbox containers running (scripts/execute_evals.py).

Docker is never called for real here: a stand-in records the commands and
reports a made-up set of containers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import execute_evals as ee

PROJECT = "com.docker.compose.project"


class FakeDocker:
    """Answers ``docker ps`` with fixed containers and records every command.

    Parameters
    ----------
    containers : list[tuple]
        ``(compose project, working directory)`` pairs, or triples adding the
        comma-separated compose files the project was started from.
    """

    def __init__(self, containers: list[tuple]):
        self.containers = [(*c, "")[:3] for c in containers]
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: int = 0) -> str | None:
        self.calls.append(args)
        if args[0] == "ps":
            # Container name, then the three labels `_PS_FORMAT` asks for
            return "".join(f"{p}-default-1\t{p}\t{w}\t{c}\n" for p, w, c in self.containers)
        return ""

    @property
    def removed(self) -> list[str]:
        return [a[2] for a in self.calls if a[:2] == ["compose", "-p"]]


def make_run(tmp_path: Path, name: str, **fields) -> tuple[Path, Path]:
    """An experiment directory with one run directory under it, holding a status file."""
    exp = tmp_path / "iterations" / "02_round2"
    run = exp / "artefacts" / "executor" / "runs" / name
    run.mkdir(parents=True)
    data = {"run_dir": str(run), "experiment_dir": str(exp), "state": "completed", "pid": None, **fields}
    (run / "status.json").write_text(json.dumps(data))
    return exp, run


def test_only_this_experiments_inspect_containers_are_listed(tmp_path):
    exp, _ = make_run(tmp_path, "r1")
    docker = FakeDocker([
        ("inspect-sandbagging-i6tmri7", str(exp / "c_c873")),
        ("inspect-sandbagging-iother1", str(tmp_path / "elsewhere" / "c_c873")),
        # A sibling folder whose name merely starts the same is not inside
        ("inspect-sandbagging-isibling", str(exp) + "_copy/c_1"),
        ("someone-elses-app", str(exp / "c_c873")),
    ])
    assert ee.leftover_sandboxes(str(exp), docker) == ["inspect-sandbagging-i6tmri7"]
    assert ee.leftover_sandboxes(str(exp), lambda args, timeout=0: None) is None


def test_a_compose_file_in_the_experiment_folder_itself_counts(tmp_path):
    exp, _ = make_run(tmp_path, "r1")
    assert ee.leftover_sandboxes(str(exp), FakeDocker([("inspect-x-iroot", str(exp))])) == ["inspect-x-iroot"]


def test_a_dockerfile_only_task_is_found_through_its_generated_compose_file(tmp_path):
    # Inspect writes such a task's compose file into its own data folder, so
    # the working directory is that folder; the build context names the task
    exp, _ = make_run(tmp_path, "r1")
    data_dir = tmp_path / "inspect_data" / "docker-compose"
    data_dir.mkdir(parents=True)
    ours = data_dir / "inspect-x-iours.yaml"
    ours.write_text(f"services:\n  default:\n    build:\n      context: {exp / 'c_1'}\n")
    theirs = data_dir / "inspect-x-itheirs.yaml"
    theirs.write_text(f"services:\n  default:\n    build:\n      context: \"{tmp_path / 'elsewhere'}\"\n")
    generic = data_dir / "inspect-x-igeneric.yaml"
    generic.write_text("services:\n  default:\n    image: \"aisiuk/inspect-tool-support\"\n")
    docker = FakeDocker([
        ("inspect-x-iours", str(data_dir), str(ours)),
        ("inspect-x-itheirs", str(data_dir), str(theirs)),
        ("inspect-x-igeneric", str(data_dir), str(generic)),
        ("inspect-x-igone", str(data_dir), str(data_dir / "deleted.yaml")),
    ])
    assert ee.leftover_sandboxes(str(exp), docker) == ["inspect-x-iours"]


def test_leftovers_removed_only_when_no_other_run_beside_is_going(tmp_path):
    exp, run = make_run(tmp_path, "r1")
    docker = FakeDocker([("inspect-x-i1", str(exp / "c_1"))])
    result = ee.remove_leftover_sandboxes(str(exp), str(run), docker)
    assert result == {"removed": ["inspect-x-i1"], "failed": [], "skipped": None}
    assert docker.calls[-1] == ["compose", "-p", "inspect-x-i1", "down", "--volumes", "--remove-orphans", "--rmi", "local"]

    # A second run in the same experiment, still going: its containers look
    # exactly like leftovers, so nothing is touched
    make_run(tmp_path, "r2", state="running", pid=os.getpid(), pid_start=ee._pid_start_time(os.getpid()))
    docker = FakeDocker([("inspect-x-i1", str(exp / "c_1"))])
    result = ee.remove_leftover_sandboxes(str(exp), str(run), docker)
    assert result["removed"] == [] and "still going" in result["skipped"]
    assert docker.removed == []


def test_stop_cleans_up_after_a_worker_that_is_already_gone(tmp_path):
    exp, run = make_run(tmp_path, "r1", state="running", pid=999_999_999)
    docker = FakeDocker([("inspect-x-i1", str(exp / "c_1"))])
    data = ee.stop_run(str(run), timeout=1, docker=docker)
    assert data["state"] == "interrupted"
    assert data["sandbox_cleanup"]["removed"] == ["inspect-x-i1"]
    assert json.loads((run / "status.json").read_text())["state"] == "interrupted"


def test_stop_keeps_what_the_worker_already_removed(tmp_path):
    # The worker cleaned up on its way out; the stop's second pass finds
    # nothing, which must not erase the worker's record
    earlier = {"removed": ["inspect-x-i1", "inspect-x-i2"], "failed": [], "skipped": None}
    exp, run = make_run(tmp_path, "r1", state="interrupted", sandbox_cleanup=earlier)
    data = ee.stop_run(str(run), timeout=1, docker=FakeDocker([]))
    assert data["sandbox_cleanup"] == earlier


def test_cleanup_records_merge():
    skipped = {"removed": [], "failed": [], "skipped": "another run"}
    ran = {"removed": ["inspect-b"], "failed": ["inspect-c"], "skipped": None}
    # No worker record: the second pass stands alone
    assert ee._merge_cleanup(None, skipped) == skipped
    # The worker ran; a second pass that was skipped leaves its record standing
    assert ee._merge_cleanup(ran, skipped) == ran
    # The worker was skipped; the second pass ran
    assert ee._merge_cleanup(skipped, ran) == ran
    # Both skipped: still skipped
    assert ee._merge_cleanup(skipped, skipped)["skipped"] == "another run"
    # Both ran: removals add up, failures are the latest pass's
    merged = ee._merge_cleanup({"removed": ["inspect-a"], "failed": ["inspect-b"], "skipped": None}, ran)
    assert merged == {"removed": ["inspect-a", "inspect-b"], "failed": ["inspect-c"], "skipped": None}


def test_report_notes_say_what_was_removed_and_what_is_still_running():
    notes = ee._cleanup_notes({"removed": ["inspect-x-i1"], "failed": ["inspect-x-i2"], "skipped": None})
    assert notes == [
        "Removed 1 sandbox container set(s) left running by killed evals: inspect-x-i1.",
        "Could not remove 1 leftover sandbox container set(s): inspect-x-i2; they are still running.",
    ]
    assert ee._cleanup_notes({"removed": [], "failed": [], "skipped": None}) == []


def test_a_stopped_eval_gets_sigint_first(tmp_path):
    """Inspect cleans up its sandboxes on SIGINT but not on SIGTERM, so SIGINT goes first."""
    mark = tmp_path / "signal.txt"
    script = (
        "import signal, sys, time\n"
        f"def got(n, f):\n    open({str(mark)!r}, 'w').write(signal.Signals(n).name)\n    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, got)\nsignal.signal(signal.SIGTERM, got)\n"
        "print('ready', flush=True)\ntime.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, start_new_session=True)
    assert proc.stdout.readline().strip() == b"ready"
    started = time.monotonic()
    ee._kill_group(proc)
    assert mark.read_text() == "SIGINT"
    assert time.monotonic() - started < 10
