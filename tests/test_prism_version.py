"""`scripts/prism_version.py`: the Prism version line for logs and reports.

Covers both ways a folder reaches Prism: a frozen copy (read from its marker
file, including the installer's record of a folder's own patch) and a live
checkout (read with git), run for real through an installed temporary folder.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import REPO_ROOT

import prism_version as pv

COPY = f".{REPO_ROOT.name}"


def _installed(tmp_path: Path, *flags: str) -> Path:
    """A temporary folder with Prism installed, live or pinned."""
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    result = subprocess.run(["bash", str(REPO_ROOT / "install.sh"), str(target), *flags], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return target


def _line(target: Path) -> str:
    """The Prism part of what the folder's own linked copy of the script prints, run from the folder."""
    out = subprocess.run(["python3", str(target / "scripts" / "prism_version.py")], capture_output=True, text=True, check=True, cwd=target)
    prism, sep, packages = out.stdout.strip().rpartition("; packages: ")
    assert sep, out.stdout
    return prism


def _head() -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_a_frozen_copy_names_its_commit(tmp_path):
    line = _line(_installed(tmp_path, "--pin"))
    assert line.startswith(f"Prism {_head()} (")
    assert line.endswith(", frozen copy")


def test_a_frozen_copy_names_the_folders_own_edits(tmp_path):
    target = _installed(tmp_path, "--pin")
    rel = "README.md"
    first = (REPO_ROOT / rel).read_text().splitlines()[0]
    (target / f"{COPY}-local.patch").write_text(f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-{first}\n+# edited\n")
    _installed(tmp_path, "--pin")
    assert _line(target).endswith(f", frozen copy, with this folder's own edits ({COPY}-local.patch)")


def test_a_live_install_says_it_is_not_frozen(tmp_path):
    line = _line(_installed(tmp_path))
    assert line.startswith(f"Prism {_head()} (")
    assert "live checkout, not frozen" in line


def test_an_incomplete_marker_or_missing_checkout_says_so(tmp_path):
    (tmp_path / "PINNED_COMMIT").write_text("abc\n")
    assert "unreadable" in pv.frozen_version(str(tmp_path))
    assert pv.frozen_version(str(tmp_path / "nowhere")) is None
    assert "unknown" in pv.live_version(str(tmp_path / "nowhere"))


def test_packages_name_inspect_and_fingerprint_the_lock(tmp_path):
    lock = '[[package]]\nname = "anyio"\nversion = "4.9.0"\n\n[[package]]\nname = "inspect-ai"\nversion = "0.3.285"\n'
    (tmp_path / "uv.lock").write_text(lock)
    first = pv.packages_version(str(tmp_path))
    assert first.startswith("packages: Inspect 0.3.285, uv.lock ")
    # Any package change moves the fingerprint
    (tmp_path / "uv.lock").write_text(lock.replace("4.9.0", "4.9.1"))
    assert pv.packages_version(str(tmp_path)) != first
    assert "no uv.lock" in pv.packages_version(str(tmp_path / "nowhere"))


def test_long_subjects_are_trimmed_to_one_line():
    line = pv._describe("0123456789abcdef", "2026-11-10T11:15:32Z", "x" * 100)
    assert line.startswith('Prism 0123456 (2026-11-10, "') and line.endswith('...")')
    assert line.count("x") == 57
