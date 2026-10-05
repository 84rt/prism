"""Deterministic helpers for the forensic-mode Transcript Analyst.

The forensic addendum (`.claude/docs/contracts/analyst_forensic_addendum.md`)
requires four things of the Analyst that are mechanical rather than
judgement-laden, and that went wrong in the six-run archive precisely when
they were re-implemented ad hoc per invocation:

- validation statistics (κ, precision/recall with Wilson intervals, F1) and
  the floor/ceiling marker — a hand-rolled validator once returned κ = 0.00 on
  four scanners simultaneously and nearly discarded a usable round;
- the **per-condition precision audit** of a loaded grader (D25);
- the **scorability gate** for construal checks — near-constant or
  near-tautological checks must not enter the scoring table (D07);
- the **extraction-provenance statement and exhibit** for the graded span
  (D08) — a κ = 1.00 grader was once built on the wrong message.

Everything here is pure computation over values the Analyst already holds. It
never reads a condition mapping, a candidate, or a hypothesis, so it is safe to
run under the Analyst's blinding. The eval-log reader is imported lazily so the
statistics can be used without `inspect_ai` installed.

Run from an installed target as::

    uv run --with "inspect_ai>=0.3" python -c "from subagents.transcript_analyst.forensic_checks import *"

or import the functions from a script written to the artefacts directory.
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
from collections.abc import Callable, Iterable, Sequence
from typing import Any

# --- Thresholds ---------------------------------------------------------------
# These mirror the base contract and addendum; they are named here so the
# report and the computation cannot disagree about what "validated" means.

VALIDATION_KAPPA_MIN = 0.4  # base contract headline threshold
VALIDATION_PRECISION_MIN = 0.6  # base contract headline threshold
FLOOR_UPPER = 0.20  # `[floor]` when Wilson upper bound < this
CEILING_LOWER = 0.80  # `[ceiling]` when Wilson lower bound > this
# |φ| at or above this between a construal check and the behaviour grader means
# the check is a near-complement of the behaviour rather than an independent
# construal measurement (R05: φ = −0.94, 0 of 86 behaviour-positives were
# check-positive). A threshold, not a law — the report states the value.
COLLINEARITY_PHI = 0.8


# --- Basic statistics -------------------------------------------------------


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Parameters
    ----------
    k : int
        Number of successes.
    n : int
        Number of trials. ``n == 0`` yields the uninformative ``(0.0, 1.0)``.
    z : float
        Normal quantile; 1.96 for a 95% interval.

    Returns
    -------
    tuple[float, float]
        ``(lower, upper)`` bounds in ``[0, 1]``.

    Examples
    --------
    >>> lo, hi = wilson_ci(42, 50)   # 84% observed
    >>> round(lo, 3), round(hi, 3)
    (0.715, 0.917)
    """
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - half) / denom, (centre + half) / denom)


def floor_ceiling_marker(k: int, n: int) -> str:
    """The base contract's ``[floor]`` / ``[ceiling]`` marker for a rate.

    The trigger is the Wilson 95% CI, not the point estimate, so a 12% rate
    whose upper bound is 18% is ``[floor]``.

    Parameters
    ----------
    k : int
        Positives.
    n : int
        Transcripts in the condition.

    Returns
    -------
    str
        ``"[floor]"``, ``"[ceiling]"`` or ``""``.
    """
    lo, hi = wilson_ci(k, n)
    if hi < FLOOR_UPPER:
        return "[floor]"
    if lo > CEILING_LOWER:
        return "[ceiling]"
    return ""


def confusion(truth: Sequence[bool], pred: Sequence[bool]) -> dict[str, int]:
    """Confusion counts of a boolean scanner against boolean labels.

    Parameters
    ----------
    truth : Sequence[bool]
        Validation labels.
    pred : Sequence[bool]
        Scanner outputs, aligned with ``truth``.

    Returns
    -------
    dict[str, int]
        ``{"TP", "FP", "FN", "TN"}``.
    """
    if len(truth) != len(pred):
        raise ValueError(f"truth ({len(truth)}) and pred ({len(pred)}) differ in length")
    tp = sum(1 for t, p in zip(truth, pred) if t and p)
    fp = sum(1 for t, p in zip(truth, pred) if not t and p)
    fn = sum(1 for t, p in zip(truth, pred) if t and not p)
    tn = sum(1 for t, p in zip(truth, pred) if not t and not p)
    return {"TP": tp, "FP": fp, "FN": fn, "TN": tn}


def cohens_kappa(truth: Sequence[bool], pred: Sequence[bool]) -> float:
    """Cohen's κ between two boolean labellings.

    κ = (p_o − p_e) / (1 − p_e). When both labellers are constant (p_e = 1)
    there is no chance to adjust for; returns 1.0 if they agree everywhere and
    0.0 otherwise, matching the convention the six runs' validators used.

    Parameters
    ----------
    truth, pred : Sequence[bool]
        The two labellings, aligned.

    Returns
    -------
    float
    """
    c = confusion(truth, pred)
    n = len(truth)
    if n == 0:
        return float("nan")
    p_o = (c["TP"] + c["TN"]) / n
    p_truth = (c["TP"] + c["FN"]) / n
    p_pred = (c["TP"] + c["FP"]) / n
    p_e = p_truth * p_pred + (1 - p_truth) * (1 - p_pred)
    if p_e == 1.0:
        return 1.0 if p_o == 1.0 else 0.0
    return (p_o - p_e) / (1 - p_e)


def validation_metrics(truth: Sequence[bool], pred: Sequence[bool]) -> dict[str, Any]:
    """The base contract's four validation metrics plus the pass verdict.

    Parameters
    ----------
    truth, pred : Sequence[bool]
        Validation labels and scanner outputs, aligned.

    Returns
    -------
    dict
        ``n``, ``positives``, ``negatives``, the confusion counts, ``kappa``,
        ``precision`` with ``precision_wilson95``, ``recall`` with
        ``recall_wilson95``, ``f1`` and ``passes`` (κ ≥ 0.4 and precision ≥ 0.6).

    Examples
    --------
    >>> m = validation_metrics([True]*52 + [False]*38, [True]*52 + [False]*38)
    >>> m["kappa"], m["precision"], m["passes"]
    (1.0, 1.0, True)
    """
    c = confusion(truth, pred)
    tp, fp, fn = c["TP"], c["FP"], c["FN"]
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    f1 = (
        2 * precision * recall / (precision + recall)
        if not math.isnan(precision) and not math.isnan(recall) and precision + recall
        else float("nan")
    )
    kappa = cohens_kappa(truth, pred)
    passes = (
        not math.isnan(kappa)
        and kappa >= VALIDATION_KAPPA_MIN
        and not math.isnan(precision)
        and precision >= VALIDATION_PRECISION_MIN
    )
    return {
        "n": len(truth),
        "positives": tp + fn,
        "negatives": fp + c["TN"],
        **c,
        "kappa": kappa,
        "precision": precision,
        "precision_wilson95": wilson_ci(tp, tp + fp),
        "recall": recall,
        "recall_wilson95": wilson_ci(tp, tp + fn),
        "f1": f1,
        "passes": passes,
    }


def phi_coefficient(a: Sequence[bool], b: Sequence[bool]) -> float:
    """φ (Pearson correlation of two booleans) over aligned transcripts.

    Parameters
    ----------
    a, b : Sequence[bool]
        Two boolean labellings of the same transcripts.

    Returns
    -------
    float
        φ in [−1, 1]; ``nan`` when either labelling is constant.
    """
    c = confusion(a, b)
    n11, n10, n01, n00 = c["TP"], c["FN"], c["FP"], c["TN"]
    denom = math.sqrt((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00))
    if denom == 0:
        return float("nan")
    return (n11 * n00 - n10 * n01) / denom


def sha256_file(path: str) -> str:
    """SHA-256 of a file's bytes — the grader-provenance content hash.

    Parameters
    ----------
    path : str
        File to hash.

    Returns
    -------
    str
        Lower-case hex digest.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --- D25: per-condition precision audit --------------------------------------


def precision_audit(
    build_precision: float,
    audits: dict[str, tuple[int, int]],
) -> dict[str, dict[str, Any]]:
    """Per-condition precision of a loaded grader from a hand-read audit sample.

    A grader validated once on the baseline pool can over-fire on a variant
    population that contains more of whatever it keys on (R01: precision 1.00
    on control, 0.73–0.80 in two-anomaly arms). The audit re-reads a sample of
    each condition's grader-positives by hand and estimates precision there.

    Parameters
    ----------
    build_precision : float
        Precision recorded at build time in the grader package's metrics.
    audits : dict[str, tuple[int, int]]
        Per condition label: ``(n_audited, n_confirmed)`` — how many
        grader-positive transcripts were hand-read and how many were true
        positives.

    Returns
    -------
    dict[str, dict]
        Per condition: ``audited``, ``confirmed``, ``precision``, ``wilson95``,
        ``delta`` (audit − build) and ``drift`` — True when the audit's Wilson
        upper bound lies below the build-time precision, i.e. the drop is
        statistically supported rather than small-sample noise.

    Examples
    --------
    >>> precision_audit(1.0, {"condition_A": (15, 15), "condition_C": (15, 11)})["condition_C"]["drift"]
    True
    >>> precision_audit(1.0, {"condition_A": (5, 5)})["condition_A"]["drift"]
    False
    """
    out: dict[str, dict[str, Any]] = {}
    for cond, (n_aud, n_conf) in audits.items():
        if n_conf > n_aud:
            raise ValueError(f"{cond}: confirmed ({n_conf}) exceeds audited ({n_aud})")
        p = n_conf / n_aud if n_aud else float("nan")
        lo, hi = wilson_ci(n_conf, n_aud)
        out[cond] = {
            "audited": n_aud,
            "confirmed": n_conf,
            "precision": p,
            "wilson95": (lo, hi),
            "delta": (p - build_precision) if n_aud else float("nan"),
            # No positives to audit is not evidence of drift — but it is
            # recorded, because a zero-positive condition has no audit at all.
            "drift": bool(n_aud) and hi < build_precision,
        }
    return out


def format_precision_audit(audit: dict[str, dict[str, Any]], build_precision: float) -> str:
    """Render a precision audit as the addendum's report lines.

    Parameters
    ----------
    audit : dict
        Output of :func:`precision_audit`.
    build_precision : float
        Build-time precision, restated for the reader.

    Returns
    -------
    str
        One line per condition, then a ``Summary-line flag:`` line naming any
        drifted condition (or ``none``) — the fragment to copy onto the
        headline line.

    Examples
    --------
    >>> print(format_precision_audit(precision_audit(1.0, {"condition_C": (15, 11)}), 1.0))
    Per-condition precision audit (build-time precision 1.00):
    - condition_C: 11/15 hand-confirmed → precision 0.73 (Wilson 95%: 0.48–0.89) [precision drift]
    Summary-line flag: [precision drift: condition_C 0.73 vs build 1.00]
    """
    lines = [f"Per-condition precision audit (build-time precision {build_precision:.2f}):"]
    flags = []
    for cond, a in audit.items():
        if not a["audited"]:
            lines.append(f"- {cond}: no grader positives to audit")
            continue
        lo, hi = a["wilson95"]
        mark = " [precision drift]" if a["drift"] else ""
        lines.append(
            f"- {cond}: {a['confirmed']}/{a['audited']} hand-confirmed → precision "
            f"{a['precision']:.2f} (Wilson 95%: {lo:.2f}–{hi:.2f}){mark}"
        )
        if a["drift"]:
            flags.append(f"{cond} {a['precision']:.2f} vs build {build_precision:.2f}")
    lines.append(
        "Summary-line flag: " + (f"[precision drift: {'; '.join(flags)}]" if flags else "none")
    )
    return "\n".join(lines)


# --- D07: construal-check scorability ----------------------------------------


def check_scorability(
    *,
    kappa: float | None,
    precision: float | None,
    rates: dict[str, tuple[int, int]],
    behaviour: Sequence[bool] | None = None,
    check: Sequence[bool] | None = None,
    deterministic_proxy: bool = False,
) -> dict[str, Any]:
    """Decide whether a construal check may enter the scoring table.

    Three ways a check fails to measure a construal, each seen in the archive:

    - **unvalidated** — κ or precision below the base thresholds (R06: three of
      four checks never validated; the row they were bought for went unscored);
    - **constant** — the same floor/ceiling marker in every condition (R02:
      100% in all eight conditions, three rounds running; R03: at ceiling in
      every arm). A quantity that cannot move cannot show a manipulation moved;
    - **collinear** — |φ| with the behaviour grader at or above
      :data:`COLLINEARITY_PHI` (R05: φ = −0.94). The check is then the behaviour
      restated, not an independent reading of construal.

    A deterministic proxy (a regex over the surface edit, e.g. "the deadline
    sentence is present") can only ever show the edit *arrived*; it is
    labelled ``[provisional]`` and may be used to **void** a row (the edit did
    not arrive), never to score one.

    Parameters
    ----------
    kappa, precision : float | None
        The check's validation metrics; ``None`` when it was not validated.
    rates : dict[str, tuple[int, int]]
        Per condition: ``(positives, n)`` from the check scanner.
    behaviour, check : Sequence[bool] | None
        Aligned per-transcript booleans from the behaviour grader and the
        check, pooled across conditions; enables the collinearity test.
    deterministic_proxy : bool
        True when the "check" is a surface-form proxy rather than a validated
        construal scanner.

    Returns
    -------
    dict
        ``scorable`` (bool), ``label`` (the report tag), ``reasons`` (list),
        ``phi`` (float or None), ``markers`` (per-condition floor/ceiling).

    Examples
    --------
    >>> check_scorability(kappa=0.8, precision=0.9,
    ...     rates={"condition_A": (40, 40), "condition_B": (40, 40)})["label"]
    'not scorable [constant: ceiling in every condition]'
    """
    reasons: list[str] = []
    markers = {cond: floor_ceiling_marker(k, n) for cond, (k, n) in rates.items()}

    if deterministic_proxy:
        return {
            "scorable": False,
            "label": "[provisional] deterministic proxy — void-only",
            "reasons": ["surface-form proxy: shows the edit arrived, not that construal moved"],
            "phi": None,
            "markers": markers,
        }

    # 1. Validation gate — the base contract's thresholds.
    if (
        kappa is None
        or precision is None
        or math.isnan(kappa)
        or math.isnan(precision)
        or kappa < VALIDATION_KAPPA_MIN
        or precision < VALIDATION_PRECISION_MIN
    ):
        reasons.append("unvalidated")

    # 2. Constancy gate — every condition carries the same non-empty marker.
    marks = set(markers.values())
    if rates and len(marks) == 1 and "" not in marks:
        which = "ceiling" if marks == {"[ceiling]"} else "floor"
        reasons.append(f"constant: {which} in every condition")

    # 3. Collinearity gate — φ with the behaviour grader over pooled transcripts.
    phi = None
    if behaviour is not None and check is not None:
        phi = phi_coefficient(behaviour, check)
        if not math.isnan(phi) and abs(phi) >= COLLINEARITY_PHI:
            reasons.append(f"collinear with grader, phi={phi:+.2f}")

    scorable = not reasons
    if scorable:
        label = "scorable"
    else:
        label = "not scorable [" + "; ".join(reasons) + "]"
    return {
        "scorable": scorable,
        "label": label,
        "reasons": reasons,
        "phi": phi,
        "markers": markers,
    }


# --- D08: extraction provenance ----------------------------------------------


def _span_last_assistant(sample: Any) -> tuple[str, str]:
    """Text of the last assistant message (the scorer's usual target)."""
    idx = [i for i, m in enumerate(sample.messages) if m.role == "assistant"]
    if not idx:
        return ("no assistant message", "")
    i = idx[-1]
    return (f"last assistant message (index {i} of {len(sample.messages)} messages)", sample.messages[i].text or "")


def _span_assistant_tool_calls(sample: Any) -> tuple[str, str]:
    """Every assistant tool call as `function(arguments)`, one per line."""
    calls = []
    n_assistant = 0
    for m in sample.messages:
        if m.role != "assistant":
            continue
        n_assistant += 1
        for tc in m.tool_calls or []:
            calls.append(f"{tc.function}({json.dumps(tc.arguments, sort_keys=True)})")
    return (f"all tool calls across {n_assistant} assistant messages ({len(calls)} calls)", "\n".join(calls))


def _span_score_answer(sample: Any) -> tuple[str, str]:
    """The scorer's stored `answer` (e.g. a submitted file), first scorer."""
    if not sample.scores:
        return ("no scorer output", "")
    name, score = next(iter(sample.scores.items()))
    return (f"score['{name}'].answer", score.answer or "")


_SPANS: dict[str, Callable[[Any], tuple[str, str]]] = {
    "last_assistant": _span_last_assistant,
    "assistant_tool_calls": _span_assistant_tool_calls,
    "score_answer": _span_score_answer,
}


def _iter_samples(log_path: str) -> Iterable[Any]:
    """Yield samples from one `.eval` file or every `.eval` in a directory."""
    from inspect_ai.log import read_eval_log  # lazy: statistics need no inspect_ai

    files = sorted(glob.glob(os.path.join(log_path, "*.eval"))) if os.path.isdir(log_path) else [log_path]
    for f in files:
        log = read_eval_log(f)
        for s in log.samples or []:
            yield s


def extraction_provenance(
    log_path: str,
    span: str | Callable[[Any], tuple[str, str]] = "last_assistant",
    exhibit_chars: int = 400,
) -> dict[str, Any]:
    """State mechanically which span of each transcript the grader read.

    R04's v1 grader validated at κ = 1.00 on the first assistant turn of a
    multi-attempt episode — scanner and labels agreeing on the wrong span. No
    metric catches that; only a statement of *which message was graded*, plus
    an exhibit a reader can open the transcript and check by hand, does.

    Parameters
    ----------
    log_path : str
        One `.eval` file or a directory of them (one condition).
    span : str | callable
        Which artefact was graded: ``"last_assistant"``, ``"assistant_tool_calls"``,
        ``"score_answer"``, or a callable ``sample -> (label, text)`` for a
        grader-specific rule (its ``__doc__`` becomes the stated rule).
    exhibit_chars : int
        How much of the exhibit artefact to quote.

    Returns
    -------
    dict
        ``rule`` (the extraction rule in words), ``samples`` (count),
        ``per_sample`` (id, epoch, ``messages_total``, ``assistant_messages``,
        ``graded_span``, ``artefact_sha256``, ``artefact_chars``) and
        ``exhibit`` — the first sample's entry plus ``artefact_head``.
    """
    fn = _SPANS[span] if isinstance(span, str) else span
    rule = span if isinstance(span, str) else (fn.__doc__ or fn.__name__).strip()

    per_sample = []
    exhibit = None
    for s in _iter_samples(log_path):
        label, text = fn(s)
        # `id` is the dataset sample id — identical across the epochs of a
        # resampled episode — so `uuid` is what names one transcript uniquely.
        entry = {
            "id": str(s.id),
            "uuid": getattr(s, "uuid", None),
            "epoch": s.epoch,
            "messages_total": len(s.messages),
            "assistant_messages": sum(1 for m in s.messages if m.role == "assistant"),
            "graded_span": label,
            "artefact_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "artefact_chars": len(text),
        }
        per_sample.append(entry)
        if exhibit is None:
            exhibit = {**entry, "artefact_head": text[:exhibit_chars]}
    return {"rule": rule, "samples": len(per_sample), "per_sample": per_sample, "exhibit": exhibit}


def format_extraction_provenance(condition: str, prov: dict[str, Any]) -> str:
    """Render one condition's provenance as the addendum's report lines.

    Parameters
    ----------
    condition : str
        Opaque condition label.
    prov : dict
        Output of :func:`extraction_provenance`.

    Returns
    -------
    str
        A mechanical statement line and a hand-verifiable exhibit block.
    """
    n = prov["samples"]
    if not n or prov["exhibit"] is None:
        return f"- {condition}: graded artefact = {prov['rule']}; 0 samples read"
    empty = sum(1 for e in prov["per_sample"] if e["artefact_chars"] == 0)
    ex = prov["exhibit"]
    head = ex["artefact_head"].replace("\n", "\\n")
    return (
        f"- {condition}: graded artefact = {prov['rule']}; {n} samples read, "
        f"{n - empty}/{n} with a non-empty artefact; "
        f"messages per sample {min(e['messages_total'] for e in prov['per_sample'])}–"
        f"{max(e['messages_total'] for e in prov['per_sample'])}, "
        f"assistant messages {min(e['assistant_messages'] for e in prov['per_sample'])}–"
        f"{max(e['assistant_messages'] for e in prov['per_sample'])}\n"
        f"  exhibit: sample `{ex['id']}` epoch {ex['epoch']} (uuid `{ex['uuid']}`), {ex['graded_span']}, "
        f"artefact sha256 `{ex['artefact_sha256'][:12]}…`, first {len(ex['artefact_head'])} chars: "
        f'"{head}"'
    )
