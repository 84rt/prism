"""Shared fixtures for the scaffold's offline tests.

The regression corpus is the six Model Forensics runs preserved under
``/workspaces/forensics-cases/`` (container-local; see
`docs/superpowers/plans/2026-09-17-model-forensics-fixes.md`). Tests that
need it skip cleanly when it is absent, so the suite still runs on a fresh
checkout — the inline fixtures cover the logic, the corpus covers the
formats real runs actually produced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES_ROOT = Path("/workspaces/forensics-cases")

# The scripts are standalone (run via `uv run --no-project`), so they are
# imported by path rather than as a package.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))


def case_path(relative: str) -> Path:
    """Resolve a path inside the forensics corpus, skipping if it is missing.

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
