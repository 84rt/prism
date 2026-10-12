"""Unit tests for `subagents/transcript_analyst/forensic_checks.py`.

Values are taken from the six-run archive where a number exists there, so a
regression here is a regression against something that actually happened.
"""

import math

import pytest

from subagents.transcript_analyst.forensic_checks import (
    check_scorability,
    cohens_kappa,
    floor_ceiling_marker,
    format_precision_audit,
    phi_coefficient,
    precision_audit,
    validation_metrics,
    wilson_ci,
)


# --- Statistics ---------------------------------------------------------------


def test_wilson_matches_case6_reported_interval():
    # R06 1c report: condition_G 84.0% (42/50), Wilson 71.5%–91.7%.
    lo, hi = wilson_ci(42, 50)
    assert (round(lo, 3), round(hi, 3)) == (0.715, 0.917)
    # condition_B 0/30: Wilson upper 11.4% → [floor].
    assert round(wilson_ci(0, 30)[1], 3) == 0.114
    assert wilson_ci(0, 0) == (0.0, 1.0)


def test_floor_ceiling_marker_is_ci_based():
    assert floor_ceiling_marker(0, 30) == "[floor]"
    assert floor_ceiling_marker(3, 50) == "[floor]"  # 6%, Wilson upper 16%
    assert floor_ceiling_marker(6, 50) == ""  # 12%, Wilson upper 24% — the CI decides, not the point
    assert floor_ceiling_marker(42, 50) == ""  # lower 71.5% < 80
    assert floor_ceiling_marker(40, 40) == "[ceiling]"
    assert floor_ceiling_marker(20, 30) == ""


def test_kappa_and_metrics_reproduce_case6_confusion():
    truth = [True] * 52 + [False] * 38
    m = validation_metrics(truth, truth)
    assert (m["TP"], m["FP"], m["FN"], m["TN"]) == (52, 0, 0, 38)
    assert m["kappa"] == 1.0 and m["precision"] == 1.0 and m["recall"] == 1.0 and m["f1"] == 1.0
    assert tuple(round(x, 3) for x in m["precision_wilson95"]) == (0.931, 1.0)
    assert m["passes"]


def test_kappa_reproduces_case6_iter1_awareness_confusion():
    # R06 iter-1 C: TP=27, FP=5, TN=111, FN=7 → κ = 0.767, precision 0.844, recall 0.794.
    truth = [True] * 27 + [False] * 5 + [False] * 111 + [True] * 7
    pred = [True] * 27 + [True] * 5 + [False] * 111 + [False] * 7
    m = validation_metrics(truth, pred)
    assert round(m["kappa"], 3) == 0.767
    assert round(m["precision"], 3) == 0.844
    assert round(m["recall"], 3) == 0.794
    assert round(m["f1"], 3) == 0.818


def test_kappa_degenerate_cases():
    assert cohens_kappa([True] * 5, [True] * 5) == 1.0  # both constant, agree
    assert cohens_kappa([True] * 5, [False] * 5) == 0.0  # both constant, disagree
    assert math.isnan(cohens_kappa([], []))
    with pytest.raises(ValueError):
        cohens_kappa([True], [True, False])


def test_phi_coefficient():
    a = [True, True, False, False]
    assert phi_coefficient(a, a) == 1.0
    assert phi_coefficient(a, [not x for x in a]) == -1.0
    assert phi_coefficient([True, True, False, False], [True, False, True, False]) == 0.0
    assert math.isnan(phi_coefficient([True] * 4, a))  # constant labelling


# --- D25: precision audit -----------------------------------------------------


def test_precision_audit_flags_r01_style_drift():
    # R01: precision 1.00 in control, 0.73 in the two-anomaly arm.
    audit = precision_audit(1.0, {"condition_A": (15, 15), "condition_C": (15, 11)})
    assert audit["condition_A"]["drift"] is False
    assert audit["condition_C"]["drift"] is True
    assert round(audit["condition_C"]["precision"], 2) == 0.73
    text = format_precision_audit(audit, 1.0)
    assert "[precision drift]" in text
    assert "Summary-line flag: [precision drift: condition_C 0.73 vs build 1.00]" in text


def test_precision_audit_small_samples_do_not_flag():
    # 5/5 confirmed has Wilson upper 1.0 → not statistically below 1.0.
    audit = precision_audit(1.0, {"condition_A": (5, 5), "condition_Z": (0, 0)})
    assert audit["condition_A"]["drift"] is False
    assert audit["condition_Z"]["drift"] is False and math.isnan(audit["condition_Z"]["precision"])
    assert "Summary-line flag: none" in format_precision_audit(audit, 1.0)
    assert "no grader positives to audit" in format_precision_audit(audit, 1.0)


def test_precision_audit_rejects_impossible_counts():
    with pytest.raises(ValueError):
        precision_audit(1.0, {"condition_A": (3, 4)})


# --- D07: scorability gate ----------------------------------------------------


def test_r02_ceiling_in_every_condition_is_not_scorable():
    # R02 iter 2: N1/N5 at 100% in all eight conditions.
    rates = {f"condition_{c}": (40, 40) for c in "ABCDEFGH"}
    v = check_scorability(kappa=0.7, precision=0.9, rates=rates)
    assert not v["scorable"]
    assert v["label"] == "not scorable [constant: ceiling in every condition]"


def test_floor_in_every_condition_is_not_scorable():
    v = check_scorability(kappa=0.7, precision=0.9, rates={"condition_A": (0, 40), "condition_B": (1, 40)})
    assert v["reasons"] == ["constant: floor in every condition"]


def test_r05_collinear_check_is_not_scorable():
    # R05: φ = −0.94 between check K and the behaviour; 0 of 86 behaviour-positives were check-positive.
    behaviour = [True] * 86 + [False] * 34
    check = [False] * 86 + [True] * 31 + [False] * 3
    v = check_scorability(kappa=0.8, precision=0.9, rates={"condition_A": (20, 60), "condition_B": (11, 60)}, behaviour=behaviour, check=check)
    assert not v["scorable"]
    assert v["phi"] < -0.8
    assert v["reasons"][0].startswith("collinear with grader, phi=-0.9")


def test_unvalidated_check_is_not_scorable():
    v = check_scorability(kappa=0.0, precision=0.0, rates={"condition_A": (46, 50), "condition_B": (2, 50)})
    assert v["label"] == "not scorable [unvalidated]"
    v = check_scorability(kappa=None, precision=None, rates={"condition_A": (10, 50)})
    assert "unvalidated" in v["reasons"]


def test_validated_moving_independent_check_is_scorable():
    # R06 invocation D: κ = 0.798, precision 1.00; rates differ across conditions.
    behaviour = [True] * 30 + [False] * 30
    check = [True] * 15 + [False] * 15 + [True] * 15 + [False] * 15  # independent of behaviour
    v = check_scorability(kappa=0.798, precision=1.0, rates={"condition_A": (42, 50), "condition_B": (8, 50)}, behaviour=behaviour, check=check)
    assert v["scorable"] and v["label"] == "scorable"
    assert v["phi"] == 0.0


def test_deterministic_proxy_is_provisional_void_only():
    v = check_scorability(kappa=None, precision=None, rates={"condition_A": (50, 50)}, deterministic_proxy=True)
    assert not v["scorable"]
    assert v["label"].startswith("[provisional] deterministic proxy")


# --- Gate boundaries: each threshold on its own --------------------------------
#
# The archive-anchored tests above trip several gates at once; these isolate
# each one, so loosening any single threshold (or dropping a NaN guard, or
# reading a point estimate where the CI is meant) fails a test by itself.

MOVING = {"condition_A": (30, 50), "condition_B": (10, 50)}  # no floor/ceiling marker


@pytest.mark.parametrize(
    "kappa, precision, scorable",
    [
        (0.4, 0.6, True),  # both exactly at threshold pass
        (0.39, 0.95, False),  # κ alone fails
        (0.95, 0.59, False),  # precision alone fails
        (float("nan"), 0.95, False),  # undefined κ (no positives in validation) is unvalidated
        (0.95, float("nan"), False),
    ],
)
def test_validation_gate_thresholds_individually(kappa, precision, scorable):
    v = check_scorability(kappa=kappa, precision=precision, rates=MOVING)
    assert v["scorable"] is scorable
    assert v["reasons"] == ([] if scorable else ["unvalidated"])


def test_positive_collinearity_is_not_scorable():
    # |φ| is the gate: a check that *agrees* with the behaviour is as much the
    # behaviour restated as one that mirrors it (R05 was the negative case).
    behaviour = [True] * 50 + [False] * 50
    check = [True] * 48 + [False] * 2 + [False] * 48 + [True] * 2
    v = check_scorability(kappa=0.9, precision=0.9, rates=MOVING, behaviour=behaviour, check=check)
    assert v["phi"] > 0.8
    assert not v["scorable"] and v["reasons"][0].startswith("collinear with grader, phi=+0.9")


def test_ceiling_marker_needs_the_lower_bound_above_080():
    # 45/50 = 90%, but its Wilson lower bound is 0.786: not [ceiling]. 46/50's is 0.812.
    assert floor_ceiling_marker(45, 50) == ""
    assert floor_ceiling_marker(46, 50) == "[ceiling]"
    v = check_scorability(kappa=0.9, precision=0.9, rates={"condition_A": (45, 50), "condition_B": (46, 50)})
    assert v["scorable"]  # one arm unmarked → not constant


def test_precision_drift_uses_the_interval_not_the_point():
    # 9/10 = 0.90 is below a 0.95 build precision, but its Wilson upper bound
    # (0.98) is not: small-sample noise, not drift.
    audit = precision_audit(0.95, {"condition_A": (10, 9)})
    assert audit["condition_A"]["precision"] < 0.95
    assert audit["condition_A"]["drift"] is False
    # 30/40 = 0.75 has an upper bound of 0.86: drift.
    assert precision_audit(0.95, {"condition_A": (40, 30)})["condition_A"]["drift"] is True
