"""Tests for the Critic's prompt builder (deficiencies D09, D10, D11).

The propagation memory's rule: verify an interface change by capturing the
built prompt, not by reading the diff. These tests build the prompt for each
mode and assert what the Critic actually receives — the contract's absolute
path, the verbatim-heading checklist, the no-re-proposal reminder, and the
re-ask section when `conformance_defects` is sent.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("claude_agent_sdk")

from subagents.redteam_critic import agent  # noqa: E402
from conftest import REPO_ROOT  # noqa: E402

REQUESTS = {
    "candidate_critique": {
        "mode": "candidate_critique",
        "transcript_paths": ["/abs/t1.txt"],
        "mapper_report_path": "/abs/mapper.md",
        "candidates": [
            {
                "id": "C1",
                "slug": "x",
                "statement": "s",
                "discriminating_signature": "d",
                "status": "live",
            }
        ],
        "iteration": 0,
    },
    "diff_review": {
        "mode": "diff_review",
        "diffs": [{"name": "v1", "diff": "--- a\n+++ b\n"}],
        "environment_path": "/abs/env",
        "mapper_report_path": "/abs/mapper.md",
    },
    "prediction": {
        "mode": "prediction",
        "investigation_log_path": "/abs/log.md",
        "leading_explanation": {"statement": "s", "discriminating_signature": "d"},
        "untested_surfaces": ["surface A"],
    },
}


def test_contract_path_is_absolute_resolved_from_scaffold_root() -> None:
    """D10: the injected path is absolute, exists, and lives under the checkout."""
    assert os.path.isabs(agent.CONTRACT_PATH)
    assert os.path.isfile(agent.CONTRACT_PATH)
    assert agent.CONTRACT_PATH == str(
        REPO_ROOT / ".claude" / "docs" / "contracts" / "critic_interface_contract.md"
    )


@pytest.mark.parametrize("mode", list(REQUESTS))
def test_prompt_carries_contract_path_and_heading_checklist(mode: str) -> None:
    """D09/D10: every mode's prompt names the contract and lists its headings."""
    prompt = agent._build_prompt(REQUESTS[mode])
    assert prompt.startswith(f"## Active Mode\n\n`{mode}`")
    assert "## Contract" in prompt
    assert f"Read `{agent.CONTRACT_PATH}` with a direct Read" in prompt
    assert "## Report Headings" in prompt
    for heading in agent.REPORT_HEADINGS[mode]:
        assert f"- `### {heading}`" in prompt
    # No other mode's headings leak into the checklist.
    for other, headings in agent.REPORT_HEADINGS.items():
        if other != mode:
            for heading in set(headings) - set(agent.REPORT_HEADINGS[mode]):
                assert f"- `### {heading}`" not in prompt
    assert "## Re-ask" not in prompt


def test_candidate_critique_carries_no_reproposal_reminder() -> None:
    """D11: the reminder sits after the ledger, before any evidence summary."""
    prompt = agent._build_prompt(REQUESTS["candidate_critique"])
    assert prompt.index("## Candidate Explanations") < prompt.index("## Before Proposing")
    assert "merges proposals by signature on receipt" in prompt
    # The transcript-first ordering is preserved.
    assert prompt.index("## Transcripts To Read First") < prompt.index("## Candidate Explanations")


@pytest.mark.parametrize("mode", ["diff_review", "prediction"])
def test_reminder_is_candidate_critique_only(mode: str) -> None:
    assert "## Before Proposing" not in agent._build_prompt(REQUESTS[mode])


def test_conformance_defects_render_as_reask() -> None:
    """The one-re-ask field is read by the builder and rendered verbatim."""
    data = dict(REQUESTS["prediction"], conformance_defects=["missing section `### Prediction`", "x"])
    prompt = agent._build_prompt(data)
    assert "## Re-ask: Conformance Defects In Your Previous Report" in prompt
    assert "- missing section `### Prediction`" in prompt
    assert prompt.index("## Report Headings") < prompt.index("## Re-ask")


def test_missing_contract_is_an_agent_error(monkeypatch) -> None:
    """A Critic without its contract must not launch (gate failure, exit 2)."""
    monkeypatch.setattr(agent, "CONTRACT_PATH", "/nonexistent/critic_interface_contract.md")
    with pytest.raises(RuntimeError, match="contract not found"):
        agent._build_prompt(REQUESTS["diff_review"])


def test_diff_review_prompt_still_withholds_candidates() -> None:
    """Blinding invariant: nothing new leaks the withheld fields into diff_review."""
    data = dict(REQUESTS["diff_review"], candidates=REQUESTS["candidate_critique"]["candidates"])
    prompt = agent._build_prompt(data)
    assert "Candidate Explanations" not in prompt
    assert '"slug": "x"' not in prompt
