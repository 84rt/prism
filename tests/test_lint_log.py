"""Tests for `scripts/lint_log.py`.

Synthetic fixtures cover each defect shape and the template's legitimate
repeats; the corpus tests pin the linter to the exact findings the six-run
analysis recorded for R01 (the duplicated Phase 4 block) and R06 (a doubled
heading and a stranded iteration-1 section), and are skipped when the
container-local corpus is absent.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "lint_log.py"
CORPUS = Path("/workspaces/forensics-cases")
R01_LOG = CORPUS / "case-1-coding-agent/investigations/visible_test_keying/INVESTIGATION-LOG.md"
R06_LOG = CORPUS / "case-6-quiz-agent-shutdown/investigations/shutdown_bypass/INVESTIGATION-LOG.md"

needs_corpus = pytest.mark.skipif(not CORPUS.is_dir(), reason="forensics-cases corpus not present")


def _load():
    spec = importlib.util.spec_from_file_location("lint_log", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves string annotations through sys.modules[cls.__module__],
    # so a by-path import must be registered before it executes.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ll = _load()


def kinds(text: str) -> list[tuple[str, int]]:
    return [(f.kind, f.line) for f in ll.lint(text)]


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

CLEAN = """# Investigation Log: x

## Phase 1: Initial Scoping
### Critic — Candidate Critique (iteration 0)
- fine: iteration 0 lives under Phase 1 by convention

## Iteration 1
### Pre-registered Predictions
| a | b |
### Ledger Delta
- Reordered: C1 1 -> 2
- Added: X1
- Unchanged: C3

## Iteration 2
### Pre-registered Predictions
| c | d |
### Ledger Delta
- Reordered: C2 1 -> 2
- Added: X2
- Unchanged: C4
### Analyst Findings — iteration 2
- rates
"""


def test_template_repeats_across_iterations_are_not_findings():
    assert kinds(CLEAN) == []


def test_duplicate_heading_under_same_parent_is_flagged():
    text = CLEAN + "### Pre-registered Predictions\n| e | f |\n"
    found = kinds(text)
    assert ("duplicate-heading", text.count("\n") - 1) in found


def test_duplicate_block_is_flagged_even_with_reworded_heading():
    body = "- line one\n- line two\n- line three\n"
    text = "# L\n## Iteration 1\n### Results\n" + body + "### Results (again)\n" + body
    found = kinds(text)
    assert [k for k, _ in found] == ["duplicate-block"]
    assert found[0][1] == 7


def test_short_identical_bodies_are_ignored():
    text = "# L\n## Iteration 1\n### Decision\n- continue\n## Iteration 2\n### Decision\n- continue\n"
    assert kinds(text) == []


def test_stranded_block_under_other_iteration_and_under_phase4():
    text = (
        "# L\n## Iteration 1\n### Analyst Findings — iteration 1\n- ok\n"
        "## Iteration 2\n### Manipulation checks specified for iteration 1\n- late\n"
        "## Phase 4: Predict to Test\n### Executor Results — iteration 2\n- late again\n"
    )
    assert kinds(text) == [("stranded-block", 6), ("stranded-block", 9)]


def test_stranded_check_uses_nearest_level2_ancestor_for_deep_headings():
    text = "# L\n## Iteration 2\n### Executor Results\n#### Notes on iteration 1 rerun\n- x\n"
    assert kinds(text) == [("stranded-block", 4)]


def test_headings_inside_code_fences_are_ignored():
    text = "# L\n## Iteration 1\n### Results\n```markdown\n### Results\n### Results\n```\n- ok\n"
    assert kinds(text) == []


# ---------------------------------------------------------------------------
# Corpus fixtures: the exact defects the analysis recorded (D23)
# ---------------------------------------------------------------------------


def _run(paths: list[Path]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, paths)], capture_output=True, text=True)


@needs_corpus
def test_r01_duplicated_phase4_block():
    findings = ll.lint(R01_LOG.read_text())
    dup_headings = sorted(f.line for f in findings if f.kind == "duplicate-heading")
    dup_blocks = sorted(f.line for f in findings if f.kind == "duplicate-block")
    # The Phase 4 results -> scoring -> ledger-delta sequence appears twice,
    # the second copy starting at L1403 (analysis: R01 log L1323–1401 then L1403–1481).
    assert dup_headings == [1403, 1431, 1465]
    assert dup_blocks == [1403, 1431, 1465]
    assert not [f for f in findings if f.kind == "stranded-block"]


@needs_corpus
def test_r06_doubled_heading_and_stranded_iteration1_block():
    findings = ll.lint(R06_LOG.read_text())
    by_kind = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f.line)
    assert by_kind.get("duplicate-heading") == [157, 158]  # analysis: L154–158
    assert by_kind.get("stranded-block") == [1422]  # analysis: L1422–1434, under Phase 4
    assert "duplicate-block" not in by_kind


@needs_corpus
def test_cli_exit_codes():
    assert _run([R01_LOG]).returncode == 1
    clean = Path(__file__).with_name("_clean_tmp.md")
    clean.write_text(CLEAN)
    try:
        assert _run([clean]).returncode == 0
    finally:
        clean.unlink()
