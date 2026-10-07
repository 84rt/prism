"""Tests for `scripts/ledger_to_json.py` (deficiency D38).

Two corpus fixtures anchor the behaviour: R01's ledger (case-1), which the
run's own serialiser handled correctly and whose output this script must
reproduce; and R05's ledger (case-5), whose prose-written C10 reached both
the Designer and the Critic as empty strings — the exhibit at
`artefacts/designer/iter2_request.json` L104–109. The script must refuse
that ledger, naming C10.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

import ledger_to_json as l2j
from conftest import case_path

CASE1_LEDGER = "case-1-coding-agent/investigations/visible_test_keying/CANDIDATES.md"
CASE5_LEDGER = (
    "case-5-tool-agent-compliance/investigations/agentharm_disinfo_compliance/"
    "CANDIDATES.md"
)
CASE5_EXHIBIT = (
    "case-5-tool-agent-compliance/investigations/agentharm_disinfo_compliance/"
    "artefacts/designer/iter2_request.json"
)
CASE6_LEDGER = "case-6-quiz-agent-shutdown/investigations/shutdown_bypass/CANDIDATES.md"


# --- Inline fixtures --------------------------------------------------------

TEMPLATE_LEDGER = textwrap.dedent(
    """\
    # Candidate Explanations: demo
    *Current as of iteration 1.*

    ## Explanandum
    The model did X rather than Y.

    ## Live candidates (ranked)

    ### C1 — `task_confusion` — rank 1
    - *Statement*: The model read the failing test as the specification
      and satisfied it directly.
    - *Origin*: CoT read iter 0
    - *Discriminating signature*: Removing the visible test file should
      collapse the behaviour; strengthening the docstring should not.
    - *Evidence for*: none yet
    - *Recipe membership*: standalone

    ### X1 — `critic_idea` — rank 2
    - *Statement*: A Critic-originated account.
    - *Origin*: Critic iter 0
    - *Discriminating signature* (**authored by the Diagnostician**): Some edit
      should reduce the rate.

    ## Live but untested

    ### C3 — `untested_one` — `live (untested)`
    - *Statement*: Untested but admissible.
    - *Origin*: CoT read iter 0
    - *Discriminating signature*:
      - *Against C1*: widening the policy should not move it.
      - *Against X1*: narrowing it should.

    ## Inert candidates

    ### C4 — `inert_one` — INERT from iteration 1
    - *Statement*: Admissible but predicting nothing testable.
    - *Discriminating signature*: none available.

    ## Eliminated candidates

    ### C2 — `dead_one` — eliminated iteration 1
    - *Statement*, *Origin*, *Discriminating signature*: <as above>
    - *Eliminated by*: a severe miss.
    """
)


def test_template_ledger_parses_live_sections_only() -> None:
    """Only the two live sections are emitted; fields survive wrapping."""
    result = l2j.parse_ledger(TEMPLATE_LEDGER)
    assert not l2j.find_defects(result.candidates)
    ids = [c.id for c in result.candidates]
    assert ids == ["C1", "X1", "C3"], ids

    c1 = result.candidates[0].to_json()
    assert set(c1) == {"id", "slug", "statement", "discriminating_signature", "status"}
    assert c1["slug"] == "task_confusion"
    assert c1["status"] == "live"
    # Wrapped lines are joined with single spaces, no trailing whitespace.
    assert c1["statement"] == (
        "The model read the failing test as the specification and satisfied it directly."
    )
    assert c1["discriminating_signature"].startswith("Removing the visible test file")


def test_parenthetical_qualifier_before_colon_is_tolerated() -> None:
    """`- *Discriminating signature* (authored by ...): text` still parses."""
    result = l2j.parse_ledger(TEMPLATE_LEDGER)
    x1 = next(c for c in result.candidates if c.id == "X1")
    assert x1.fields["discriminating_signature"] == "Some edit should reduce the rate."


def test_indented_sub_bullets_are_field_content_not_new_fields() -> None:
    """A signature written as nested `- *Against C1*:` bullets is kept."""
    result = l2j.parse_ledger(TEMPLATE_LEDGER)
    c3 = next(c for c in result.candidates if c.id == "C3")
    sig = c3.fields["discriminating_signature"]
    assert "Against C1" in sig and "Against X1" in sig


def test_prose_candidate_is_a_loud_failure(tmp_path: Path, capsys) -> None:
    """A live candidate written as prose fails with both fields named."""
    ledger = TEMPLATE_LEDGER.replace(
        "### C3 — `untested_one` — `live (untested)`\n"
        "- *Statement*: Untested but admissible.\n"
        "- *Origin*: CoT read iter 0\n"
        "- *Discriminating signature*:\n"
        "  - *Against C1*: widening the policy should not move it.\n"
        "  - *Against X1*: narrowing it should.\n",
        "### C3 — `untested_one` — `live (untested)`\n"
        "The must-exclude candidate. Cannot be retired: its positive control\n"
        "failed to induce the signature.\n",
    )
    src = tmp_path / "CANDIDATES.md"
    out = tmp_path / "out.json"
    src.write_text(ledger)

    assert l2j.main([str(src), "-o", str(out)]) == 1
    assert not out.exists(), "nothing may be written on a defect"
    err = capsys.readouterr().err
    assert "C3 `untested_one`" in err
    assert "missing *Statement*" in err
    assert "missing *Discriminating signature*" in err


def test_not_live_trailer_under_live_section_is_skipped_with_warning() -> None:
    """`### X1 — `slug` — MERGED INTO X5` under Live is not emitted."""
    ledger = TEMPLATE_LEDGER.replace(
        "### X1 — `critic_idea` — rank 2", "### X1 — `critic_idea` — MERGED INTO C1 at iter 1"
    )
    result = l2j.parse_ledger(ledger)
    assert [c.id for c in result.candidates] == ["C1", "C3"]
    assert any("X1" in w and "MERGED" in w for w in result.warnings)


def test_duplicate_id_is_a_defect() -> None:
    """Two live entries with the same id cannot be serialised."""
    ledger = TEMPLATE_LEDGER.replace("### X1 — `critic_idea`", "### C1 — `critic_idea`")
    result = l2j.parse_ledger(ledger)
    defects = l2j.find_defects(result.candidates)
    assert any("duplicate id" in d for d in defects)


def test_missing_live_section_is_a_structure_error(tmp_path: Path) -> None:
    """A file without a live section exits 2, not 0 with an empty array."""
    src = tmp_path / "notes.md"
    src.write_text("# Notes\n\n## Something else\n\n### C1 — `x` — rank 1\n")
    assert l2j.main([str(src)]) == 2


# --- Corpus fixtures -----------------------------------------------------------


def test_case1_matches_r01_exemplar_output(tmp_path: Path) -> None:
    """R01's own serialiser was correct on its ledger; ours must agree."""
    ledger = case_path(CASE1_LEDGER)
    exemplar = case_path("case-1-coding-agent/scripts/ledger_to_json.py")

    import importlib.util

    spec = importlib.util.spec_from_file_location("r01_ledger_to_json", exemplar)
    r01 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r01)  # type: ignore[union-attr]
    expected = r01.parse(ledger.read_text())

    out = tmp_path / "c1.json"
    assert l2j.main([str(ledger), "-o", str(out)]) == 0
    assert json.loads(out.read_text()) == expected
    assert len(expected) == 18


def test_case5_refuses_on_prose_c10(capsys) -> None:
    """R05's C10 shipped empty; the sanctioned serialiser must refuse it."""
    ledger = case_path(CASE5_LEDGER)
    exhibit = json.loads(case_path(CASE5_EXHIBIT).read_text())

    # The exhibit: what the hand serialisation actually sent.
    shipped = next(c for c in exhibit["candidates"] if c["id"] == "C10")
    assert shipped["statement"] == "" and shipped["discriminating_signature"] == ""

    assert l2j.main([str(ledger)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "", "nothing may be written on a defect"
    assert "C10 `eval_aware_strategic`" in captured.err
    assert "missing *Statement*" in captured.err
    assert "missing *Discriminating signature*" in captured.err


def test_case6_nested_signature_bullets_serialise() -> None:
    """R06's X13 writes its signature as nested bullets; it must not be lost."""
    result = l2j.parse_ledger(case_path(CASE6_LEDGER).read_text())
    assert not l2j.find_defects(result.candidates)
    x13 = next(c for c in result.candidates if c.id == "X13")
    assert "Against C1" in x13.fields["discriminating_signature"]
    assert "Against X11" in x13.fields["discriminating_signature"]


@pytest.mark.parametrize("cli_flag", [[], ["-o"]])
def test_cli_writes_valid_json(tmp_path: Path, capsys, cli_flag: list[str]) -> None:
    """Stdout and -o both carry the array; the summary goes to stderr."""
    src = tmp_path / "CANDIDATES.md"
    src.write_text(TEMPLATE_LEDGER)
    out = tmp_path / "out.json"
    argv = [str(src)] + (cli_flag + [str(out)] if cli_flag else [])
    assert l2j.main(argv) == 0
    captured = capsys.readouterr()
    payload = json.loads(out.read_text() if cli_flag else captured.out)
    assert [c["id"] for c in payload] == ["C1", "X1", "C3"]
    assert "3 live candidate(s)" in captured.err


# ---------------------------------------------------------------------------
# Tiers (3d step 9): `--tested-only` for the Designer, whole ledger for the Critic
# ---------------------------------------------------------------------------

TIERED_LEDGER = textwrap.dedent(
    """\
    # Candidate Explanations: demo

    ## Live candidates (ranked)

    ### C1 — `leader` — rank 1
    - *Statement*: s1
    - *Discriminating signature*: g1
    - *Tier*: tested — by rank

    ### C2 — `ninth` — rank 9
    - *Statement*: s2
    - *Discriminating signature*: g2
    - *Tier*: carried — by rank

    ### C3 — `pre_tier_entry` — rank 3
    - *Statement*: s3
    - *Discriminating signature*: g3
    """
)


def test_tier_is_read_but_never_forwarded() -> None:
    result = l2j.parse_ledger(TIERED_LEDGER)
    assert [c.tier for c in result.candidates] == ["tested", "carried", None]
    # The contract shape is unchanged: no tier key reaches a sub-agent.
    expected_keys = {"id", "slug", "statement", "discriminating_signature", "status"}
    assert all(set(c.to_json()) == expected_keys for c in result.candidates)


def test_tested_only_drops_carried_and_keeps_untiered_with_warning() -> None:
    kept, warnings = l2j.select_tested(l2j.parse_ledger(TIERED_LEDGER).candidates)
    assert [c.id for c in kept] == ["C1", "C3"]
    assert len(warnings) == 1 and "C3" in warnings[0]


def test_cli_default_sends_whole_ledger_and_flag_sends_tested(tmp_path: Path, capsys) -> None:
    ledger = tmp_path / "CANDIDATES.md"
    ledger.write_text(TIERED_LEDGER)

    assert l2j.main([str(ledger)]) == 0
    assert [c["id"] for c in json.loads(capsys.readouterr().out)] == ["C1", "C2", "C3"]

    assert l2j.main([str(ledger), "--tested-only"]) == 0
    captured = capsys.readouterr()
    assert [c["id"] for c in json.loads(captured.out)] == ["C1", "C3"]
    assert "1 carried candidate(s) omitted" in captured.err


def test_carried_entries_are_still_defect_checked(tmp_path: Path) -> None:
    """The Critic still receives carried candidates, so a blank one fails even under --tested-only."""
    ledger = tmp_path / "CANDIDATES.md"
    ledger.write_text(TIERED_LEDGER.replace("- *Statement*: s2\n", ""))
    assert l2j.main([str(ledger), "--tested-only"]) == 1


def test_unrecognised_tier_value_warns_and_is_untiered() -> None:
    result = l2j.parse_ledger(TIERED_LEDGER.replace("tested — by rank", "maybe"))
    assert result.candidates[0].tier is None
    assert any("neither `tested` nor `carried`" in w for w in result.warnings)
