"""Every scaffold script is wired into each mechanical surface.

A new script is a five-surface change outside the code itself: the install
symlink list and the separate `GITIGNORE_ENTRIES` list in `install.sh`,
`uninstall.sh`, the README's directory tree, and a `settings.json` allow rule
in the exact form the script's own Usage documents. A miss on any of them is
silent until a target repo cannot run (or commits) the script, so the lists
are checked against the `scripts/` directory itself.
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import REPO_ROOT

# `_patch_devcontainer.py` is install-time plumbing run from the scaffold
# checkout; it is never linked into a target.
SCRIPTS = sorted(p.name for p in (REPO_ROOT / "scripts").glob("*.py") if p.name != "_patch_devcontainer.py")


def _gitignore_entries(install: str) -> str:
    """The body of install.sh's `GITIGNORE_ENTRIES=( … )` array."""
    m = re.search(r"GITIGNORE_ENTRIES=\((.*?)\n\)", install, re.S)
    assert m, "GITIGNORE_ENTRIES array not found in install.sh"
    return m.group(1)


def _documented_invocation(script: str) -> str:
    """The `uv run … python scripts/<script>` prefix the script's docstring documents."""
    text = (REPO_ROOT / "scripts" / script).read_text()
    m = re.search(rf"(uv run (?:--no-project )?python scripts/{re.escape(script)})", text)
    assert m, f"{script} documents no `uv run … python scripts/{script}` invocation"
    return m.group(1)


def test_script_list_is_not_empty():
    assert "execute_evals.py" in SCRIPTS and "launch_subagent.py" in SCRIPTS


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_is_wired_everywhere(script):
    rel = f"scripts/{script}"
    install = (REPO_ROOT / "install.sh").read_text()
    uninstall = (REPO_ROOT / "uninstall.sh").read_text()
    readme = (REPO_ROOT / "README.md").read_text()
    allow = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())["permissions"]["allow"]

    assert f'link_file "{rel}"' in install, f"install.sh does not link {rel}"
    assert f'"{rel}"' in _gitignore_entries(install), f"install.sh GITIGNORE_ENTRIES lacks {rel}"
    assert f'unlink_file "{rel}"' in uninstall, f"uninstall.sh does not unlink {rel}"
    tree = readme.split("## Directory Structure", 1)[1].split("```", 2)[1]
    assert re.search(rf"[├└]── {re.escape(script)}\s", tree), f"README tree lacks {script}"
    rule = f"Bash({_documented_invocation(script)} *)"
    assert rule in allow, f"settings.json lacks the documented form {rule}"
