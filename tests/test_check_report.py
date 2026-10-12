"""Tests for `scripts/check_report.py` (deficiencies D09 and D33).

Corpus anchors: R04's Gemini reports substituted their own headings in both
modes (non-conformant); R06's Grok iter-3 diff review used bold pseudo-
headings; R02's Gemini merged four sections and proposed candidates with no
signature (D33); R03's gpt-5.1 candidate critiques are conformant, while its
diff review and prediction are not.
"""

from __future__ import annotations

import re

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


CONTRACTS = {
    "critic": ".claude/docs/contracts/critic_interface_contract.md",
    "mapper": ".claude/docs/contracts/mapper_interface_contract.md",
    "designer": ".claude/docs/contracts/designer_interface_contract.md",
    "explorer": ".claude/docs/contracts/explorer_interface_contract.md",
    "executor": ".claude/docs/contracts/executor_interface_contract.md",
    "analyst": ".claude/docs/contracts/analyst_interface_contract.md",
}
OPTIONAL_MARK = re.compile(r"\s*\*\(only if applicable\)\*\s*$")


def _report_format_headings(contract: str) -> list[tuple[str, bool]]:
    """The `###` headings of a contract's `## … Report Format` section.

    Parameters
    ----------
    contract : str
        Scaffold-relative contract path.

    Returns
    -------
    list[tuple[str, bool]]
        ``(heading text, marked optional)`` in document order, e.g.
        ``[("Summary", False), ..., ("Variant Dependencies", True), ...]``.
    """
    lines = (REPO_ROOT / contract).read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## ") and "Report Format" in ln)
    found: list[tuple[str, bool]] = []
    for ln in lines[start + 1 :]:
        if ln.startswith("## "):
            break
        if ln.startswith("### "):
            text = ln[4:].strip()
            found.append((OPTIONAL_MARK.sub("", text), bool(OPTIONAL_MARK.search(text))))
    return found


def test_every_role_is_covered() -> None:
    """Any role can run on a non-Claude model, so every role needs a check."""
    assert set(cr.ROLE_HEADINGS) == set(CONTRACTS)


@pytest.mark.parametrize("role", sorted(CONTRACTS))
def test_heading_tables_equal_their_contracts(role: str) -> None:
    """Each table restates its contract's Report Format exactly, in order.

    A strict equality rather than a containment check: a heading added to or
    reordered in a contract without the table following is drift, and drift
    is exactly what makes a conformant report fail (or a defective one pass).
    For the Critic the section lists the three modes' headings in mode order.
    """
    contract = _report_format_headings(CONTRACTS[role])
    modes = cr.ROLE_HEADINGS[role]
    # The Analyst's forensic mode is checked against its addendum below.
    tables = [v for k, v in modes.items() if not (role == "analyst" and k == "forensic")]
    expected = [h for table in tables for h in table]
    assert [h for h, _ in contract] == expected, role
    marked_optional = {h for h, optional in contract if optional}
    assert marked_optional == cr.OPTIONAL_HEADINGS & set(expected), role


def test_forensic_analyst_table_follows_the_addendum() -> None:
    """The two forensic sections sit exactly where the addendum places them."""
    text = (REPO_ROOT / ".claude/docs/contracts/analyst_forensic_addendum.md").read_text()
    forensic = cr.ROLE_HEADINGS["analyst"]["forensic"]
    for added in cr.ANALYST_FORENSIC_ADDITIONS:
        m = re.search(
            rf"^### {re.escape(added)}\n\nNew top-level report section, placed immediately "
            r"after `### (?P<after>[^`]+)` and before `### (?P<before>[^`]+)`",
            text,
            re.M,
        )
        assert m, f"addendum no longer states where `### {added}` goes"
        i = forensic.index(added)
        assert forensic[i - 1] == m.group("after"), added
        # "before" names the next *base* section; the other addition may sit between.
        later = forensic[i + 1 :]
        assert m.group("before") in later, added
        assert all(h in cr.ANALYST_FORENSIC_ADDITIONS for h in later[: later.index(m.group("before"))])
    assert [h for h in forensic if h not in cr.ANALYST_FORENSIC_ADDITIONS] == list(cr.ANALYST_BASE_HEADINGS)


def test_forensic_analyst_system_prompt_skeleton_matches_the_table() -> None:
    """The forensic system prompt's literal report skeleton equals the checker's list, in order.

    The forensic overlay tells the Analyst which headings to emit and where the
    two addendum sections go; a drift there (a renamed base heading, the
    additions moved) produces reports the checker then rejects, so the prompt's
    own statement is parsed and compared, not just grepped for.
    """
    from subagents.transcript_analyst.system_prompt import FORENSIC_SECTION

    para = next(p for p in FORENSIC_SECTION.split("\n\n") if p.startswith("**The report skeleton"))
    base_list = re.search(r"verbatim and in order \((.*?)\), with the addendum", para, re.S)
    added_list = re.search(r"the addendum's two top-level sections \((.*?)\) inserted", para, re.S)
    place = re.search(r"inserted between `### ([^`]+)` and `### ([^`]+)`", para)
    assert base_list and added_list and place, "the skeleton sentence no longer has its parsed shape"
    base = re.findall(r"`### ([^`]+)`", base_list.group(1))
    added = re.findall(r"`### ([^`]+)`", added_list.group(1))
    after, before = place.groups()
    assert base.index(before) == base.index(after) + 1, "the additions must sit between adjacent base sections"
    i = base.index(after) + 1
    assert base[:i] + added + base[i:] == list(cr.ROLE_HEADINGS["analyst"]["forensic"])


def test_executor_subheadings_appear_in_contract() -> None:
    text = (REPO_ROOT / CONTRACTS["executor"]).read_text()
    for prefix in cr.SUBSECTION_PREFIXES["executor"]["Error Summary"]:
        assert f"#### {prefix}N condition-model pair(s))" in text, prefix


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
    """A heading line at line start inside a fence is content, not structure.

    The fenced block carries a stray section and a duplicate of a real one;
    the same text unfenced must produce both defects, so the pass is for the
    right reason (the fence handling) rather than because nothing looked like
    a heading.
    """
    body = "### Findings\n### Blind Spots\n"
    fenced = _critique(f"Nothing to add.\n\n```markdown\n{body}```")
    assert cr.check_report("critic", fenced, "candidate_critique")[0] == []
    unfenced, _ = cr.check_report("critic", _critique(f"Nothing to add.\n\n{body}"), "candidate_critique")
    assert any("`### Findings` is not a section" in d for d in unfenced), unfenced
    assert any("duplicate section `### Blind Spots`" in d for d in unfenced), unfenced


def test_duplicate_section_is_a_defect() -> None:
    text = _critique("Nothing to add.") + "\n### Blind Spots\n\nAgain.\n"
    defects, _ = cr.check_report("critic", text, "candidate_critique")
    assert any(d.startswith("line ") and "duplicate section `### Blind Spots` (first at line" in d for d in defects), defects


def test_executor_subheadings_out_of_order_are_a_defect() -> None:
    report = _executor_report()
    swapped = report.replace("#### Structural (", "#### TMP (").replace("#### Sample-level (", "#### Structural (").replace("#### TMP (", "#### Sample-level (")
    defects, _ = cr.check_report("executor", swapped)
    assert any("sub-headings out of order" in d for d in defects), defects
    assert cr.check_report("executor", report)[0] == []


@pytest.mark.parametrize(
    "proposed, expect",
    [
        # Ids mentioned, no block recognised, not marked empty.
        ("C3 looks relevant here but I have not written it up as a candidate.", "mentions candidate ids"),
        # Prose with no ids and no empty marker.
        ("Some thoughts about the environment, but nothing structured.", "has content but no recognisable candidate"),
    ],
)
def test_unrecognised_proposals_warn(proposed: str, expect: str) -> None:
    """A Proposed section the parser cannot read is flagged for a hand check, not passed silently."""
    defects, warnings = cr.check_report("critic", _critique(proposed), "candidate_critique")
    assert defects == []
    assert any(expect in w for w in warnings), warnings


def test_preamble_is_a_warning_not_a_defect() -> None:
    text = "Here is my report.\n\n" + _critique("Empty — nothing to add.")
    defects, warnings = cr.check_report("critic", text, "candidate_critique")
    assert defects == []
    assert any("precede the first heading" in w for w in warnings)


def test_cli_strips_preamble_before_emitting_the_report() -> None:
    """The emitted report starts at its first `###`; check_report then sees no preamble."""
    from subagents.cli import strip_preamble

    report = _critique("Empty — nothing to add.")
    emitted = strip_preamble("Now I have everything I need. Here is the report:\n\n" + report)
    assert emitted == report and emitted.startswith("### ")
    _, warnings = cr.check_report("critic", emitted, "candidate_critique")
    assert not any("precede the first heading" in w for w in warnings)
    # No `###` line at all: left untouched for check_report to flag, never emptied.
    assert strip_preamble("**Summary** only bold\n") == "**Summary** only bold\n"
    # A `####` sub-heading or a `##` title is not the first section heading.
    assert strip_preamble("## Title\n#### x\n### Summary\nok") == "### Summary\nok"


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
        cr.check_report("executor", "### Summary\n", "forensic")  # analyst-only mode
    with pytest.raises(ValueError):
        cr.check_report("analyst", "### Summary\n", "no_such_mode")
    with pytest.raises(ValueError):
        cr.check_report("no_such_role", "### Summary\n")


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


# --- Executor and Analyst (all roles may run on a non-Claude model) ------------

R06_ART = "case-6-quiz-agent-shutdown/investigations/shutdown_bypass/artefacts/"
R01_ART = R01 + "artefacts/"
R02_ART = R02 + "artefacts/"
R04_ART = "case-4-spec-tests-conflict/investigations/test_conflict_special_casing/artefacts/"


def _executor_report(drop: str | None = None) -> str:
    """A minimal conformant Executor report, optionally missing a sub-heading.

    Parameters
    ----------
    drop : str | None
        Prefix of an Error Summary sub-heading to leave out.

    Returns
    -------
    str
        The report text.
    """
    parts = []
    for h in cr.ROLE_HEADINGS["executor"][None]:
        parts.append(f"### {h}\n\nNone.\n")
        if h == "Error Summary":
            for p in cr.SUBSECTION_PREFIXES["executor"]["Error Summary"]:
                if p != drop:
                    parts.append(f"#### {p}0 condition-model pair(s))\n\nNone.\n")
    return "\n".join(parts)


def test_executor_synthetic_conformant_and_missing_subheading() -> None:
    assert cr.check_report("executor", _executor_report()) == ([], [])
    defects, _ = cr.check_report("executor", _executor_report(drop="Sample-level ("))
    assert any("missing its `#### Sample-level (" in d for d in defects), defects


@pytest.mark.parametrize(
    "relative",
    [
        R06_ART + "executor/resample_report.md",
        R06_ART + "executor/iter1_report.md",
        R05 + "executor/iter1_report.md",
        R05 + "executor/phase4_report.md",
        R04_ART + "executor/report_00_resample.md",
        R02 + "iterations/01_round1/artefacts/executor/report.md",
        R01_ART + "executor/resample_report_1.md",
    ],
)
def test_conformant_executor_corpus_reports_pass(relative: str) -> None:
    defects, _ = cr.check_report("executor", case_path(relative).read_text())
    assert defects == [], defects


@pytest.mark.parametrize(
    "relative, expect",
    [
        # R06: the Executor asked for permission instead of reporting (wrapper denied).
        (R06_ART + "executor/iter2_widen_actions_report.md", "no markdown headings found"),
        # R01: an ad-hoc score-blind runner's report, not the contract's shape.
        (R01_ART + "executor/resample.report.md", "Execution Report (score-blind)"),
    ],
)
def test_non_conformant_executor_corpus_reports_fail(relative: str, expect: str) -> None:
    defects, _ = cr.check_report("executor", case_path(relative).read_text())
    assert any(expect in d for d in defects), defects


@pytest.mark.parametrize(
    "relative",
    [
        R01_ART + "analyst/report_1c.md",
        R02_ART + "analyst/report_1c.md",
        R03 + "artefacts/analyst/report_1c.md",
        R04_ART + "analyst/report_1c.md",
        R05 + "analyst/iter1_A_report.md",
        R06_ART + "analyst/report_iter3_I.md",
        R06_ART + "analyst/report_iter4.md",
    ],
)
def test_conformant_forensic_analyst_corpus_reports_pass(relative: str) -> None:
    text = case_path(relative).read_text()
    defects, _ = cr.check_report("analyst", text, "forensic")
    assert defects == [], defects
    # Checked as a base report, the two forensic sections are foreign — and
    # the warning names the likely cause.
    defects, warnings = cr.check_report("analyst", text)
    assert any("Eval-awareness split" in d and "not a section" in d for d in defects)
    assert any("--forensic" in w for w in warnings)


def test_non_conformant_analyst_corpus_report_fails() -> None:
    """R06's Grok Analyst asked permission to continue instead of reporting."""
    text = case_path(R06_ART + "analyst/report_iter1_A_FAILED_grok_asked_permission.md").read_text()
    defects, _ = cr.check_report("analyst", text, "forensic")
    assert any("no markdown headings found" in d for d in defects), defects


def test_forensic_sections_out_of_place_are_defects() -> None:
    base = "".join(f"### {h}\n\nx\n\n" for h in cr.ANALYST_BASE_HEADINGS)
    assert cr.check_report("analyst", base) == ([], [])
    # Forensic sections appended at the end instead of after Quantified results.
    late = base + "### Behaviours not captured by the grader\n\nx\n\n### Eval-awareness split\n\nx\n"
    defects, _ = cr.check_report("analyst", late, "forensic")
    assert any(d.startswith("sections out of order") for d in defects), defects
    # Forensic mode with the sections omitted names both as missing.
    defects, _ = cr.check_report("analyst", base, "forensic")
    assert "missing section `### Behaviours not captured by the grader`" in defects
    assert "missing section `### Eval-awareness split`" in defects


def test_cli_forensic_flag(tmp_path) -> None:
    report = tmp_path / "a.md"
    report.write_text("".join(f"### {h}\n\nx\n\n" for h in cr.ROLE_HEADINGS["analyst"]["forensic"]))
    assert cr.main(["analyst", str(report), "--forensic"]) == 0
    assert cr.main(["analyst", str(report)]) == 1
    assert cr.main(["mapper", str(report), "--forensic"]) == 2
    assert cr.main(["analyst", str(report), "--forensic", "--mode", "prediction"]) == 2
