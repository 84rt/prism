"""Tests for `scripts/check_report.py` (deficiencies D09 and D33).

Corpus anchors: R04's Gemini reports substituted their own headings in both
modes (non-conformant); R06's Grok iter-3 diff review used bold pseudo-
headings; R02's Gemini merged four sections and proposed candidates with no
signature (D33); R03's gpt-5.1 candidate critiques are conformant, while its
diff review and prediction are not.
"""

from __future__ import annotations


import pytest

import check_report as cr
from conftest import REPO_ROOT, case_path

R04 = "case-4-spec-tests-conflict/investigations/test_conflict_special_casing/artefacts/critic/"
R03 = "case-3-email-agent-cot/investigations/r1_blackmail_cot/"
R06 = "case-6-quiz-agent-shutdown/investigations/shutdown_bypass/artefacts/critic/"
R02 = "case-2-email-agent/investigations/blackmail_gpt41/"
R05 = "case-5-tool-agent-compliance/investigations/agentharm_disinfo_compliance/artefacts/"
R01 = "case-1-coding-agent/investigations/visible_test_keying/"


def _critique(proposed: str, retire: str = "None advised.") -> str:
    """Build a conformant candidate_critique report around a Proposed section.

    Parameters
    ----------
    proposed : str
        Body of `### Proposed Additional Candidates`.
    retire : str
        Body of `### Candidates I Would Retire`.

    Returns
    -------
    str
        The report text.
    """
    # Built by concatenation rather than dedent: an interpolated body with its
    # own (un)indentation would otherwise defeat dedent and un-heading the
    # `### ` lines.
    return (
        "### Independent Reading\n\n"
        "Read the transcripts first; this section was written before the ledger.\n\n"
        "### Blind Spots\n\nNone found.\n\n"
        "### Assumption Audit\n\nThe list assumes the CoT is faithful.\n\n"
        f"### Proposed Additional Candidates\n\n{proposed}\n\n"
        f"### Candidates I Would Retire\n\n{retire}\n"
    )


# --- Heading tables stay in sync with their sources -------------------------


def test_critic_headings_match_agent_checklist() -> None:
    """The checker and the prompt builder must carry identical strings."""
    pytest.importorskip("claude_agent_sdk")
    from subagents.redteam_critic import agent

    assert agent.REPORT_HEADINGS == cr.CRITIC_HEADINGS


@pytest.mark.parametrize(
    "role, contract",
    [
        ("critic", ".claude/docs/contracts/critic_interface_contract.md"),
        ("mapper", ".claude/docs/contracts/mapper_interface_contract.md"),
        ("designer", ".claude/docs/contracts/designer_interface_contract.md"),
        ("explorer", ".claude/docs/contracts/explorer_interface_contract.md"),
    ],
)
def test_every_expected_heading_appears_in_its_contract(role: str, contract: str) -> None:
    """A heading in the table that the contract does not spell is a drift bug."""
    text = (REPO_ROOT / contract).read_text()
    for headings in cr.ROLE_HEADINGS[role].values():
        for h in headings:
            assert f"### {h}" in text, f"{role}: `### {h}` not in {contract}"


# --- Heading checks ---------------------------------------------------------------


def test_conformant_synthetic_report_passes() -> None:
    proposed = "**X1 — a_slug**\n\n**Statement:** Something.\n\n**Discriminating signature:** Removing Z should reduce it."
    defects, warnings = cr.check_report("critic", _critique(proposed), "candidate_critique")
    assert defects == []
    assert warnings == []


def test_substituted_heading_is_named_and_missing_sections_listed() -> None:
    text = _critique("Empty — nothing to add.").replace(
        "### Blind Spots", "### Critique of the Ledger"
    )
    defects, _ = cr.check_report("critic", text, "candidate_critique")
    assert any("Critique of the Ledger" in d and "not a section" in d for d in defects)
    assert "missing section `### Blind Spots`" in defects


def test_near_miss_heading_is_reported_as_non_verbatim() -> None:
    text = _critique("Empty — nothing to add.").replace(
        "### Assumption Audit", "### Assumption audit:"
    )
    defects, _ = cr.check_report("critic", text, "candidate_critique")
    assert any("non-verbatim" in d and "Assumption Audit" in d for d in defects)
    # A near miss counts as an attempt at the section, so it is not also "missing".
    assert "missing section `### Assumption Audit`" not in defects


def test_wrong_level_and_out_of_order_are_defects() -> None:
    text = _critique("Empty — nothing to add.")
    text = text.replace("### Blind Spots", "## Blind Spots")
    # Swap the last two sections.
    head, _, tail = text.partition("### Proposed Additional Candidates")
    proposed, _, retire = tail.partition("### Candidates I Would Retire")
    text = head + "### Candidates I Would Retire" + retire + "### Proposed Additional Candidates" + proposed
    defects, _ = cr.check_report("critic", text, "candidate_critique")
    assert any("level 2" in d and "Blind Spots" in d for d in defects)
    assert any(d.startswith("sections out of order") for d in defects)


def test_headings_inside_code_fences_are_ignored() -> None:
    proposed = "```json\n[{\"id\": \"X1\", \"discriminating_signature\": \"### not a heading\"}]\n```"
    defects, _ = cr.check_report("critic", _critique(proposed), "candidate_critique")
    assert defects == []


def test_preamble_is_a_warning_not_a_defect() -> None:
    text = "Here is my report.\n\n" + _critique("Empty — nothing to add.")
    defects, warnings = cr.check_report("critic", text, "candidate_critique")
    assert defects == []
    assert any("precede the first heading" in w for w in warnings)


def test_no_headings_at_all_is_named() -> None:
    text = "**### Summary**\n\nbold pseudo-headings only\n"
    defects, _ = cr.check_report("critic", text, "diff_review")
    assert defects[0].startswith("no markdown headings found")


def test_optional_designer_section_may_be_omitted() -> None:
    text = "\n\n".join(
        f"### {h}\n\nbody" for h in cr.ROLE_HEADINGS["designer"][None] if h != "Variant Dependencies"
    )
    defects, _ = cr.check_report("designer", text)
    assert defects == []


def test_usage_errors() -> None:
    with pytest.raises(ValueError):
        cr.check_report("critic", "### Summary\n")  # mode required
    with pytest.raises(ValueError):
        cr.check_report("critic", "### Summary\n", "no_such_mode")
    with pytest.raises(ValueError):
        cr.check_report("mapper", "### Summary\n", "candidate_critique")  # no modes
    with pytest.raises(ValueError):
        cr.check_report("executor", "### Summary\n")  # not covered yet


# --- Critic signature check ---------------------------------------------------------


@pytest.mark.parametrize(
    "proposed",
    [
        # bold id line, bold labels
        "**X1 — slug_a**\n\n**Statement:** S.\n\n**Discriminating signature:** Removing Z ↓.",
        # heading + key/value bullets (R03 iter 1)
        "#### X1 — Title\n\n- **id**: X1\n- **slug**: slug_a\n- **statement**: S.\n- **discriminating_signature**:\n  Removing Z ↓.",
        # bold label with colon inside (R01 Grok)
        "- **id:** `X1`\n  **slug:** `slug_a`\n  **statement:** S.\n  **discriminating_signature:** Removing Z ↓.",
        # plain bullets (R05 gpt-5.1)
        "- X1 — slug_a\n  - Statement: S.\n  - Discriminating signature: Removing Z ↓.",
        # YAML-ish (R05 gpt-5.1)
        "- id: X1\n  slug: slug_a\n  statement: S.\n  discriminating_signature: Removing Z ↓.",
        # fenced JSON (R04 Gemini)
        "```json\n[{\"id\": \"X1\", \"slug\": \"slug_a\", \"statement\": \"S.\", \"discriminating_signature\": \"Removing Z ↓.\"}]\n```",
    ],
)
def test_signature_recognised_across_formats(proposed: str) -> None:
    defects, warnings = cr.check_report("critic", _critique(proposed), "candidate_critique")
    assert defects == [], defects
    assert warnings == [], warnings


@pytest.mark.parametrize(
    "proposed",
    [
        "**X1 — slug_a**\n\n**Statement:** S.\n\n**Discriminating signature:**\n\n**X2 — slug_b**\n\n**Statement:** T.\n\n**Discriminating signature:** Removing Z ↓.",
        "- id: X1\n  slug: slug_a\n  statement: S.\n  discriminating_signature:\n  status: live",
        "```json\n[{\"id\": \"X1\", \"slug\": \"slug_a\", \"statement\": \"S.\", \"discriminating_signature\": \"\"}]\n```",
        "**X1 — slug_a**\n\n**Statement:** S with no falsifier at all.",
    ],
)
def test_missing_signature_is_a_defect(proposed: str) -> None:
    defects, _ = cr.check_report("critic", _critique(proposed), "candidate_critique")
    assert any("X1 has no discriminating signature" in d for d in defects), defects
    assert not any("X2" in d for d in defects)


def test_explicitly_empty_section_passes_quietly() -> None:
    defects, warnings = cr.check_report(
        "critic", _critique("Returned empty: nothing to add."), "candidate_critique"
    )
    assert defects == [] and warnings == []


def test_non_x_id_is_a_warning() -> None:
    proposed = "**C11 — slug**\n\n**Discriminating signature:** Removing Z ↓."
    defects, warnings = cr.check_report("critic", _critique(proposed), "candidate_critique")
    assert defects == []
    assert any("C11" in w and "X<n>" in w for w in warnings)


# --- Corpus fixtures -------------------------------------------------------------


@pytest.mark.parametrize(
    "relative, mode",
    [
        (R03 + "artefacts/critic/iter0_candidate_critique.md", "candidate_critique"),
        (R03 + "iterations/01_first_round/artefacts/critic_candidate_critique.md", "candidate_critique"),
        (R05 + "critic/iter1_critique_report.md", "candidate_critique"),
        (R05 + "critic/final_critique_report.md", "candidate_critique"),
        (R06 + "critique_iter3.md", "candidate_critique"),
        (R01 + "artefacts/critic/iter0_critic_grok46.md", "candidate_critique"),
        (R05 + "critic/iter1_diffreview_report.md", "diff_review"),
        (R06 + "diffreview_iter1.md", "diff_review"),
        (R05 + "critic/phase4_prediction_report.md", "prediction"),
        (R06 + "prediction.md", "prediction"),
    ],
)
def test_conformant_corpus_reports_pass(relative: str, mode: str) -> None:
    defects, warnings = cr.check_report("critic", case_path(relative).read_text(), mode)
    assert defects == [], defects
    assert not any("check" in w and "by hand" in w for w in warnings), warnings


@pytest.mark.parametrize(
    "relative, mode, expect",
    [
        # R04 Gemini substituted its own headings in both modes (D09).
        (R04 + "report_iter0_candidates.md", "candidate_critique", "Critique of the Ledger"),
        (R04 + "report_round1_diffreview.md", "diff_review", "missing section `### Per-Diff Analysis`"),
        # R02 Gemini merged four sections under one heading (D09/D33).
        (R02 + "iterations/01_round1/artefacts/critic/critique_iter1.md", "candidate_critique", "Critique of the Candidate Ledger"),
        # R06 Grok iter 3: bold pseudo-headings, no markdown headings.
        (R06 + "diffreview_iter3.md", "diff_review", "no markdown headings found"),
        # R03 gpt-5.1 prediction reworded every heading.
        (R03 + "iterations/03_predict_to_test/artefacts/critic_prediction.md", "prediction", "Factor Chosen"),
    ],
)
def test_non_conformant_corpus_reports_fail(relative: str, mode: str, expect: str) -> None:
    defects, _ = cr.check_report("critic", case_path(relative).read_text(), mode)
    assert defects, "expected defects"
    assert any(expect in d for d in defects), defects


@pytest.mark.parametrize(
    "role, relative",
    [
        ("mapper", R02 + "artefacts/mapper/report.md"),
        ("mapper", R06 + "../mapper/report.md"),
        ("designer", R05 + "designer/iter2_report.md"),
        ("designer", R06 + "../designer/design_iter3.md"),
    ],
)
def test_other_roles_on_corpus(role: str, relative: str) -> None:
    defects, _ = cr.check_report(role, case_path(relative).read_text())
    assert defects == [], defects


def test_cli_exit_codes(tmp_path) -> None:
    good = tmp_path / "good.md"
    good.write_text(_critique("Nothing to add."))
    bad = tmp_path / "bad.md"
    bad.write_text("### Independent Reading\n\nonly one section\n")
    assert cr.main(["critic", str(good), "--mode", "candidate_critique"]) == 0
    assert cr.main(["critic", str(bad), "--mode", "candidate_critique"]) == 1
    assert cr.main(["critic", str(good)]) == 2
    assert cr.main(["critic", str(tmp_path / "missing.md"), "--mode", "prediction"]) == 2
