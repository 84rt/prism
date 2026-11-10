"""`install.sh --pin`: a frozen copy of the scaffold inside the target.

The installer is run for real against a temporary target folder. Pinned links
must be relative and resolve inside the target's copy; the copy must hold the
checkout's latest commit; replacing or removing it must be refused while an
investigation is running; and both a plain re-install and `uninstall.sh` must
remove it. `_live_runs.py`, the installer's running-run check, is also tested
directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import REPO_ROOT

import _live_runs
import launch_subagent

NAME = REPO_ROOT.name
COPY = f".{NAME}"


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    """Run install.sh or uninstall.sh from the checkout, capturing output."""
    return subprocess.run(["bash", str(REPO_ROOT / script), *args], capture_output=True, text=True)


def _links(target: Path) -> list[Path]:
    """Every symlink the installer made in the target (the copy's own excluded)."""
    return [p for p in target.rglob("*") if p.is_symlink() and COPY not in p.relative_to(target).parts]


def _fresh_log(target: Path, name: str = "demo") -> Path:
    """An unfinished investigation whose log was just written."""
    inv = target / "investigations" / name
    inv.mkdir(parents=True)
    (inv / "INVESTIGATION-LOG.md").write_text("# log\n")
    return inv


@pytest.fixture
def pinned(tmp_path):
    """A target installed with --pin."""
    target = tmp_path / "target"
    target.mkdir()
    result = _run("install.sh", str(target), "--pin")
    assert result.returncode == 0, result.stdout + result.stderr
    return target


# ---------------------------------------------------------------------------
# The pinned install
# ---------------------------------------------------------------------------


def test_pinned_links_are_relative_and_resolve_inside_the_copy(pinned):
    links = _links(pinned)
    assert len(links) > 40
    for link in links:
        assert not os.path.isabs(os.readlink(link)), link
        real = Path(os.path.realpath(link))
        assert real.is_file(), f"{link} dangles"
        assert real.is_relative_to(pinned / COPY), f"{link} resolves outside the copy"
    assert os.readlink(pinned / "scripts" / "spend.py") == f"../{COPY}/scripts/spend.py"


def test_the_copy_holds_the_latest_commit_and_is_gitignored(pinned):
    head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert (pinned / COPY / "PINNED_COMMIT").read_text().splitlines()[0] == head
    assert f"/{COPY}/" in (pinned / ".gitignore").read_text().splitlines()


def test_helpers_find_their_contracts_inside_the_copy(pinned):
    # The runner resolves its own path to hand helpers absolute document paths
    runner = os.path.realpath(pinned / "subagents" / "runner.py")
    root = os.path.dirname(os.path.dirname(runner))
    assert root == str(pinned / COPY)
    assert os.path.isfile(os.path.join(root, ".claude", "docs", "contracts", "critic_interface_contract.md"))


# ---------------------------------------------------------------------------
# Moving the copy forward, or off
# ---------------------------------------------------------------------------


def test_repinning_is_refused_while_a_run_is_going_unless_forced(pinned):
    _fresh_log(pinned)
    sentinel = pinned / COPY / "sentinel"
    sentinel.write_text("old copy")

    refused = _run("install.sh", str(pinned), "--pin")
    assert refused.returncode == 1
    assert "investigations/demo" in refused.stdout
    assert sentinel.exists(), "the copy was replaced despite the refusal"

    forced = _run("install.sh", str(pinned), "--pin", "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert not sentinel.exists(), "--force did not replace the copy"


def test_repinning_a_quiet_target_replaces_the_copy(pinned):
    sentinel = pinned / COPY / "sentinel"
    sentinel.write_text("old copy")
    assert _run("install.sh", str(pinned), "--pin").returncode == 0
    assert not sentinel.exists()
    assert not list(pinned.glob(f"{COPY}.*")), "a temporary copy was left behind"


def test_a_plain_install_puts_links_back_on_the_checkout_and_removes_the_copy(pinned):
    result = _run("install.sh", str(pinned))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (pinned / COPY).exists()
    assert os.readlink(pinned / "scripts" / "spend.py") == str(REPO_ROOT / "scripts" / "spend.py")


def test_uninstall_removes_pinned_links_and_the_copy(pinned):
    result = _run("uninstall.sh", str(pinned))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (pinned / COPY).exists()
    assert _links(pinned) == []


def test_unknown_options_are_refused(tmp_path):
    assert _run("install.sh", str(tmp_path), "--pinn").returncode == 1


# ---------------------------------------------------------------------------
# The running-run check
# ---------------------------------------------------------------------------


def test_a_finished_or_quiet_investigation_is_not_running(tmp_path):
    done = _fresh_log(tmp_path, "done")
    (done / "FINDINGS-REPORT.md").write_text("report")
    assert _live_runs.live_reasons(str(done)) == []

    quiet = _fresh_log(tmp_path, "quiet")
    later = time.time() + (_live_runs.RECENT_HOURS + 1) * 3600
    assert _live_runs.live_reasons(str(quiet), now=later) == []
    assert _live_runs.live_reasons(str(quiet)) != []


def test_a_live_helper_counts_even_after_the_log_goes_quiet(tmp_path):
    inv = _fresh_log(tmp_path)
    (inv / "FINDINGS-REPORT.md").write_text("report")
    receipt = inv / "artefacts" / "executor" / "receipt_r2_executor_1.json"
    receipt.parent.mkdir(parents=True)
    pid = os.getpid()
    receipt.write_text(json.dumps({
        "role": "executor", "tag": "r2", "n": 1, "state": "running", "receipt": str(receipt),
        "started_at": "2026-11-10T10:00:00+00:00", "pid": pid, "pid_start": launch_subagent._pid_start_time(pid),
    }))
    assert _live_runs.live_reasons(str(inv)) == ["executor r2 still running"]


def test_a_target_without_investigations_has_nothing_running(tmp_path):
    assert _live_runs.main([str(tmp_path)]) == 0
