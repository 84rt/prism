"""Shared test configuration for the Prism scaffold tests.

The scaffold has no package install; tests import `subagents.*` from the repo
root, so it is put on `sys.path` here. Regression tests over the persisted
forensics corpus are skipped (not failed) when the corpus is absent — it is
container-local and never committed.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Never write bytecode into the read-only corpus when a test imports a persisted
# grader module from a case directory.
sys.dont_write_bytecode = True

CASES_ROOT = "/workspaces/forensics-cases"


def requires_cases(path: str = CASES_ROOT):
    """Skip marker for tests that need the on-disk forensics corpus.

    Parameters
    ----------
    path : str
        A path that must exist for the test to run.

    Returns
    -------
    pytest.MarkDecorator
    """
    return pytest.mark.skipif(not os.path.exists(path), reason=f"forensics corpus not present at {path}")
