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
