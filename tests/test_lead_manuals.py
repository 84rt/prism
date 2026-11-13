"""Rules in the lead agents' manuals that past runs showed were needed.

Prose rules can be dropped by a later trim without anything failing, so the
ones bought with an expensive run are pinned here. Each check looks for the
rule's bold lead-in and for where it sits relative to the launch step.
"""

from __future__ import annotations

import pytest

from conftest import REPO_ROOT

MANUALS = REPO_ROOT / ".claude" / "docs" / "investigator"

# (manual, the size check's lead-in, a phrase from the launch instruction it must precede)
SIZE_CHECKS = [
    ("diagnostician_responsibilities.md", "**Size the round before launching.**", "Launch with `--tag <iteration>"),
    ("orchestrator_responsibilities.md", "**Size the batch before launching.**", "**Launch the Experiment Executor sub-agent**"),
]


@pytest.mark.parametrize("manual, lead_in, launch", SIZE_CHECKS)
def test_each_lead_sizes_a_round_before_launching_it(manual, lead_in, launch):
    """A round's episodes, hours and dollars are checked before it launches.

    Without this, a sandbagging rerun's second round grew from a projected
    seven arms to eleven (2,200 episodes, about seventeen hours) and launched
    without anyone comparing that with the agreed time.

    Parameters
    ----------
    manual : str
        The lead's manual, relative to the investigator docs folder.
    lead_in : str
        The bold opening of the size-check rule.
    launch : str
        A phrase from the instruction that launches the Executor.

    Returns
    -------
    None
    """
    text = (MANUALS / manual).read_text()
    assert lead_in in text, f"{manual} lost its pre-launch size check"
    rule = text[text.index(lead_in):].split("\n\n", 1)[0]
    for quantity in ("episodes", "hours", "dollars"):
        assert quantity in rule, f"{manual}'s size check no longer projects {quantity}"
    assert text.index(lead_in) < text.index(launch), f"{manual}'s size check comes after the launch"


# Every place that states when to split a forensic round across Analysts
SPLIT_RULES = [
    ".claude/docs/contracts/analyst_forensic_addendum.md",
    ".claude/docs/investigator/subagent_invocation.md",
    ".claude/docs/investigator/diagnostician_analyst_guide.md",
]


@pytest.mark.parametrize("doc", SPLIT_RULES)
def test_a_round_splits_across_analysts_only_above_twelve_conditions(doc):
    """One Analyst takes a round of up to about twelve conditions.

    Each split invocation re-runs the grader, its audit and a full re-grade of
    every transcript: in the sandbagging rerun that was about a third of each
    Analyst's cost, for rounds one Analyst would have finished well within
    its turn cap.

    Parameters
    ----------
    doc : str
        A document stating the split threshold, relative to the repository root.

    Returns
    -------
    None
    """
    text = (REPO_ROOT / doc).read_text()
    assert "120 scanner×condition passes" in text, f"{doc} lost the raised pass threshold"
    assert "twelve conditions" in text or "> 12 conditions" in text, f"{doc} lost the raised condition threshold"
    assert "24 scanner" not in text and "eight conditions" not in text, f"{doc} still states the old threshold"


@pytest.mark.parametrize("skill", ["diagnose", "orchestrate"])
def test_each_lead_leaves_concurrency_to_automatic_sizing(skill):
    """Neither lead sets samples in flight by hand without an agreed reason.

    A forensics run set ``max_parallel`` × ``max_connections`` itself for every
    batch, which switched automatic sizing off and ran thirty at once beside
    another run on a ten-core machine.

    Parameters
    ----------
    skill : str
        The lead skill's folder under ``.claude/skills``.

    Returns
    -------
    None
    """
    text = (REPO_ROOT / ".claude" / "skills" / skill / "SKILL.md").read_text()
    line = next(l for l in text.splitlines() if l.startswith("- **Parallelism"))
    assert "automatically" in line, f"{skill} no longer defers to automatic sizing"
