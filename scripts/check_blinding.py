"""Pre-Analyst blinding check: does any condition's real name reach the Analyst?

The Analyst's firewall rests on it not knowing which condition is which. Opaque
``condition_X`` labels in the request are not enough on their own: in one
archived run every label pointed at a log directory named after its condition
(``logs/leverage_prohibition``), so the Analyst knew the whole mapping and
reported by real name. A condition's name reaches the Analyst through more
channels than the directory, and every one of them has to be clean:

1. **path** — the transcript-source directory itself (and, when it is a
   symlink, its **symlink_target**), plus every directory and file name under it;
2. **task_file** — the header's ``eval.task_file``, named by however the lead
   agent named the variant's environment copy (``env_v6_inert_control/task.py``);
3. **tags** — ``eval.tags``, where the runner writes ``exp:<experiment>`` and
   ``cond:<condition>``;
4. **metadata** — ``eval.metadata``, where the runner writes ``condition=``;
5. **task_args** — every ``-T`` value. A ``-T``-driven variant exposes its
   manipulation here and nothing short of rewriting the log can scrub it, so
   this channel **warns and never blocks**: sometimes the ``-T`` value *is* the
   whole difference between conditions, and that leak is accepted and recorded.

What this is for: not an airtight firewall — an Analyst that went digging
through the logs could always eventually tell the conditions apart — but making
sure the *scaffold itself* never hands the Analyst an expectation about what a
condition is. Real names therefore block; incidental, irreducible channels
(a task name shared by every condition, a ``-T`` value) only warn.

The lead agent (Orchestrator or Diagnostician) runs this before **every**
Analyst invocation; a non-zero exit blocks the invocation.

What blocks
-----------
* Any **real condition name** recorded in the label map, in any channel.
* Any **role word** (``baseline``, ``control``, ``treatment``, ``resample``) in a
  label, a path component, a symlink target, or a directory/file name under a
  source. In a header field (task file, task name, dataset, tags, metadata) a
  role word blocks when that value distinguishes conditions; a value identical
  in every condition (typically the eval's own task or dataset name, e.g. a
  control-research eval) cannot reveal the mapping and is reported as a warning.
  With a single source there is nothing to compare against, so only the
  eval's own task and dataset names get that benefit of the doubt.
* A condition name recorded in a header (``metadata.condition`` / ``cond:`` tag)
  that is not one of the label map's opaque ids — the map did not record it, so
  nothing can vouch that it is opaque.
* A real name anywhere else in a whole Analyst request (topic, behaviour spec,
  construal checks). Role words are not checked there: a behaviour
  description may legitimately say "control".
* A source whose ``.eval`` headers all fail to read (fail closed: unverified).
* A transcript-source ``.eval`` that **is the original episode**: the same
  file (realpath) as the request's ``behaviour_spec.reference_transcript``, or a
  byte-identical copy of it (same SHA-256). The reference defines the behaviour
  class and is never a measured condition — in one smoke run the lead copied
  the flagged log into the round's ``logs/<id>/`` and sent it beside the fresh
  resample, and the Analyst spotted the identical file. (Only checkable when the
  whole request is given: a bare mapping carries no reference.)

Path components are checked from the investigation directory (the label map's
directory) down when the sources sit beneath it — so a role-word directory such
as ``iterations/00_resample`` blocks even for a lone source (the neutral layout
is ``iterations/00_round0``) — and otherwise below the sources' common
ancestor: above the investigation is the user's workspace, whose names are not
the lead agent's to choose. Symlink targets, of the sources themselves and of
any file or directory link beneath them, are judged the same way.

What warns (exit status unchanged)
----------------------------------
``task_args`` that differ between conditions (named argument by argument);
sources living under differently shaped directories, or task files of
differently shaped paths once opaque ids are masked (the odd one out can be
singled out — e.g. a control running the unprefixed ``task.py``); a role word in
a value common to every condition; a source with no ``.eval`` files; a header
that could not be read; map ids not in ``c_<4 hex>`` form. Conditions whose
headers differ in **task name, epoch count or ``created`` date** (a log from
another run or another day sits among the round's arms, and the Analyst can
single it out). ``execution_notes`` that use a **role or provenance word**
(``earlier``, ``original``, ``reference``, ``previous``, ``prior``, ``flagged``,
or a role word): the notes carry non-directional execution facts, and "an
earlier run" tells the Analyst where a condition came from.

Usage
-----
    uv run python scripts/check_blinding.py <label_map.json> <transcript_source.json>

``label_map.json`` is the iteration's private ``CONDITION_LABEL_MAP_iter<N>.json``:

    {"iteration": 1,
     "conditions": {"c_3f9a": {"name": "leverage_prohibition", "role": "variant"},
                    "c_8b21": {"name": "concurrent_control", "role": "control"}},
     "analyst_labels": {"iter1_analyst_1": {"condition_K": "c_3f9a", "condition_B": "c_8b21"}}}

(``conditions`` values may also be bare strings. A legacy map with no
``conditions`` key — e.g. ``{invocation: {label: real_name}}`` — is read by
taking every leaf string that is not an opaque id or label as a real name.)

``transcript_source.json`` is either the Analyst request's ``{label: dir}``
mapping or the whole Analyst request JSON (its ``transcript_source`` is used).

Exit codes: 0 clean (warnings may be printed), 1 at least one blocking hit
(each listed with its channel, file and offending token), 2 usage error.
Everything is printed to stdout; nothing here reads a scorer output — headers
are read with ``header_only=True`` and only the naming fields are inspected, so
the check is safe to run during the score blackout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Words that name an arm's role rather than its content. Any of them on a
# discriminating channel tells the Analyst which condition is the comparator.
ROLE_WORDS: tuple[str, ...] = ("baseline", "control", "treatment", "resample")

# Words that say where a condition came from rather than how it ran. In
# `execution_notes` ("condition_B: an earlier run of the same task") they tell
# the Analyst which source is the original or a frozen pool, so they warn.
PROVENANCE_WORDS: tuple[str, ...] = ("earlier", "original", "reference", "previous", "prior", "flagged")

# Hash in 1 MiB blocks so a large log never sits in memory whole.
_HASH_BLOCK = 1 << 20

# The opaque execution id the lead agent assigns every condition (`c_3f9a`).
OPAQUE_ID_RE = re.compile(r"c_[0-9a-f]{4}")
OPAQUE_ID_FULL_RE = re.compile(r"^c_[0-9a-f]{4}$")

# Analyst-time labels (`condition_A`, `set_B`); never treated as real names.
LABEL_FULL_RE = re.compile(r"^(condition|set)_[A-Za-z0-9]+$")

# Characters treated as interchangeable word separators when matching a real
# name, so `leverage_prohibition` also matches `leverage-prohibition` (Inspect
# turns underscores in task names into hyphens in log filenames).
_SEP = r"[\s_.\-]*"

# Inspect's default log filename: `<timestamp>_<task-name>_<id>.eval`. The
# middle segment is the eval's own task name, which belongs to the environment
# rather than to the lead agent's naming.
_INSPECT_LOGNAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9\-+]+)_(.+)_([A-Za-z0-9]+)$")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    """One forbidden token and how to find it.

    Attributes
    ----------
    text : str
        The token as recorded (a real name or a role word).
    kind : str
        ``"real name"`` or ``"role word"``.
    pattern : re.Pattern
        Case-insensitive, separator-tolerant, boundary-anchored matcher.
    """

    text: str
    kind: str
    pattern: re.Pattern


@dataclass(frozen=True)
class Finding:
    """One blocking hit.

    Attributes
    ----------
    channel : str
        Where the token was found (``label``, ``path``, ``symlink_target``,
        ``filename``, ``task_file``, ``task``, ``dataset``, ``tags``,
        ``metadata``).
    label : str
        The transcript-source label whose directory the hit belongs to.
    where : str
        The file or path the hit was found in.
    token : str
        The offending token.
    detail : str
        The value it was found in, and why it blocks.
    """

    channel: str
    label: str
    where: str
    token: str
    detail: str


class UsageError(Exception):
    """Raised for bad arguments or unreadable inputs (exit code 2)."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _load_json(path: str, what: str) -> Any:
    """Read a JSON file, converting every failure into a `UsageError`.

    Parameters
    ----------
    path : str
        File to read.
    what : str
        Human name for the file, used in the error message.

    Returns
    -------
    Any
        The parsed JSON.
    """
    if not os.path.isfile(path):
        raise UsageError(f"{what} not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{what} is not readable JSON ({path}): {exc}") from exc


def load_label_map(path: str) -> tuple[list[str], set[str] | None, list[str]]:
    """Extract the real names and opaque ids from a condition label map.

    Parameters
    ----------
    path : str
        Path to ``CONDITION_LABEL_MAP_iter<N>.json``.

    Returns
    -------
    tuple[list[str], set[str] | None, list[str]]
        ``(real_names, ids, warnings)``. ``ids`` is the set of opaque ids from
        the ``conditions`` section, or ``None`` for a legacy map that has none
        (the unmapped-condition rule is then skipped).

    Examples
    --------
    ``{"conditions": {"c_3f9a": {"name": "leverage_prohibition"}}}`` gives
    ``(["leverage_prohibition"], {"c_3f9a"}, [])``.
    """
    data = _load_json(path, "label map")
    if not isinstance(data, dict):
        raise UsageError(f"label map must be a JSON object: {path}")
    warnings: list[str] = []
    names: list[str] = []

    # --- Current shape: {"conditions": {id: name | {"name": ...}}} ---
    if "conditions" in data:
        conds = data["conditions"]
        if not isinstance(conds, dict) or not conds:
            raise UsageError(f"label map 'conditions' must be a non-empty object: {path}")
        for cid, entry in conds.items():
            if not OPAQUE_ID_FULL_RE.match(str(cid)):
                warnings.append(
                    f"label map id {cid!r} is not an opaque `c_<4 hex>` id; if it is "
                    "descriptive it is itself a leak wherever the runner records it"
                )
            name = entry.get("name") if isinstance(entry, dict) else entry
            if not isinstance(name, str) or not name.strip():
                raise UsageError(f"label map condition {cid!r} has no real name: {path}")
            names.append(name)
        return names, {str(c) for c in conds}, warnings

    # --- Legacy shape: every leaf string that is not an id or a label ---
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            if not (OPAQUE_ID_FULL_RE.match(node) or LABEL_FULL_RE.match(node)):
                names.append(node)

    walk(data)
    if not names:
        warnings.append("label map records no real names; only role words are checked")
    return names, None, warnings


def load_transcript_source(path: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Read the Analyst's ``{label: dir}`` mapping, bare or inside a request.

    Parameters
    ----------
    path : str
        Either a JSON object mapping labels to directories, or a whole Analyst
        request JSON with a ``transcript_source`` field.

    Returns
    -------
    tuple[dict[str, str], list[tuple[str, str]]]
        ``(sources, request_strings)``: label → absolute directory path, and —
        when a whole request was given — every other string in it as
        ``(json_path, value)`` (topic, behaviour spec, construal checks, …), so
        a real name typed into the request itself is caught too.
    """
    data = _load_json(path, "transcript source")
    request_strings: list[tuple[str, str]] = []
    if isinstance(data, dict) and isinstance(data.get("transcript_source"), dict):
        rest = {k: v for k, v in data.items() if k != "transcript_source"}
        request_strings = [(k, v) for k, v in _flatten("", rest) if v]
        data = data["transcript_source"]
    if not isinstance(data, dict) or not data or not all(isinstance(v, str) for v in data.values()):
        raise UsageError(
            f"transcript source must be a non-empty {{label: dir}} object or an Analyst request "
            f"carrying one: {path}"
        )
    sources: dict[str, str] = {}
    for label, d in data.items():
        if not os.path.exists(d):
            raise UsageError(f"transcript source for {label!r} does not exist: {d}")
        sources[str(label)] = os.path.abspath(d)
    return sources, request_strings


def load_request_extras(path: str) -> tuple[str | None, list[str]]:
    """The reference transcript and execution notes of a whole Analyst request.

    Parameters
    ----------
    path : str
        The transcript-source JSON given on the command line (already
        validated by `load_transcript_source`).

    Returns
    -------
    tuple[str | None, list[str]]
        ``(reference_transcript, execution_notes)``: the request's
        ``behaviour_spec.reference_transcript`` (``None`` for a bare mapping or
        an Orchestrator-mode request) and its ``execution_notes`` as a list of
        strings (empty when absent).
    """
    data = _load_json(path, "transcript source")
    if not isinstance(data, dict) or "transcript_source" not in data:
        return None, []
    spec = data.get("behaviour_spec")
    ref = spec.get("reference_transcript") if isinstance(spec, dict) else None
    notes = data.get("execution_notes")
    if isinstance(notes, str):
        notes = [notes]
    notes = [str(n) for n in notes] if isinstance(notes, list) else []
    return (ref if isinstance(ref, str) and ref.strip() else None), notes


def sha256_file(path: str) -> str:
    """Hex SHA-256 of a file's bytes.

    Parameters
    ----------
    path : str
        File to hash.

    Returns
    -------
    str
        The digest.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_HASH_BLOCK), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Token matching
# ---------------------------------------------------------------------------


def build_tokens(real_names: list[str]) -> list[Token]:
    """Compile matchers for every real name and role word.

    Real names match on alphanumeric boundaries (``v1`` hits ``logs/v1`` but not
    ``v10``); role words match on letter boundaries and tolerate a plural, so
    ``baseline2`` and ``controls`` hit but ``controller`` does not.

    Parameters
    ----------
    real_names : list[str]
        Real condition names from the label map.

    Returns
    -------
    list[Token]
        One entry per distinct token (case-insensitive).
    """
    tokens: list[Token] = []
    seen: set[str] = set()
    for word in ROLE_WORDS:
        seen.add(word)
        # Inflections too (`resampled`, `resampling`, `controls`), but not an
        # unrelated longer word (`controller`).
        body = rf"{word[:-1]}(?:e|es|ed|ing)" if word.endswith("e") else rf"{word}(?:s|es|ed|ing|led|ling)?"
        tokens.append(Token(word, "role word", re.compile(rf"(?<![a-z]){body}(?![a-z])", re.I)))
    for name in real_names:
        # Same camelCase split as `find_tokens`, so the two agree.
        split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip())
        parts = [p for p in re.split(r"[\s_.\-]+", split) if p]
        key = "_".join(parts).lower()
        if not parts or key in seen or len(key) < 2:
            continue
        seen.add(key)
        body = _SEP.join(re.escape(p) for p in parts)
        tokens.append(Token(name, "real name", re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.I)))
    return tokens


def find_tokens(text: str, tokens: list[Token]) -> list[Token]:
    """Return every token that occurs in ``text``.

    Parameters
    ----------
    text : str
        The value to search.
    tokens : list[Token]
        Matchers from `build_tokens`.

    Returns
    -------
    list[Token]
        Matching tokens, in `tokens` order.
    """
    # Split camelCase so `TreatmentArm` reads as `Treatment_Arm`.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return [t for t in tokens if t.pattern.search(text)]


def mask_ids(text: str) -> str:
    """Replace opaque ids with a placeholder, to compare the *shape* of names.

    Parameters
    ----------
    text : str
        A path or value.

    Returns
    -------
    str
        ``text`` with every ``c_<4 hex>`` replaced by ``<id>``
        (``c_3f9a/task.py`` -> ``<id>/task.py``).
    """
    return OPAQUE_ID_RE.sub("<id>", text)


# ---------------------------------------------------------------------------
# Filesystem channels
# ---------------------------------------------------------------------------


def _is_under(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or lies beneath it.

    Parameters
    ----------
    path, root : str
        Absolute paths.

    Returns
    -------
    bool
    """
    return os.path.commonpath([path, root]) == root


def discriminating_components(paths: dict[str, str], anchor: str | None = None) -> dict[str, list[str]]:
    """The path components the Analyst can see that are the lead agent's naming.

    Parameters
    ----------
    paths : dict[str, str]
        Label → absolute path.
    anchor : str | None
        The investigation directory. When every path lies strictly beneath
        it, components are taken from there down (``iterations/00_round0/
        logs/c_0e4d``) — the investigation's own layout is the lead agent's to
        name; above it is the user's workspace.

    Returns
    -------
    dict[str, list[str]]
        Label → components below the anchor, or else below the sources' common
        ancestor; for a single source (or a degenerate overlap) at least its
        own final component.

    Examples
    --------
    ``{"A": "/i/it1/logs/c_3f9a", "B": "/i/it0/logs/c_8b21"}`` gives
    ``{"A": ["it1", "logs", "c_3f9a"], "B": ["it0", "logs", "c_8b21"]}``.
    """
    values = list(paths.values())
    if anchor and all(_is_under(p, anchor) and p != anchor for p in values):
        common = anchor
    else:
        common = os.path.commonpath(values) if len(values) > 1 else values[0]
    out: dict[str, list[str]] = {}
    for label, p in paths.items():
        rel = os.path.relpath(p, common)
        comps = [] if rel == "." else Path(rel).parts
        out[label] = list(comps) or [os.path.basename(p.rstrip(os.sep))]
    return out


def walk_source(root: str) -> tuple[list[str], list[str], list[str]]:
    """List every name under a source, its ``.eval`` files and its symlink targets.

    Parameters
    ----------
    root : str
        The source directory (or a single ``.eval`` file).

    Returns
    -------
    tuple[list[str], list[str], list[str]]
        ``(relative_names, eval_paths, link_targets)``: every directory and file
        path relative to ``root``; the absolute paths of every ``.eval`` found;
        and the resolved target of every symlink (file or directory) under
        ``root`` — a staged ``x.eval -> …/leverage_prohibition/x.eval`` leaks
        through its target exactly as a staged directory does.
    """
    if os.path.isfile(root):
        return [], ([root] if root.endswith(".eval") else []), []
    names: list[str] = []
    evals: list[str] = []
    targets: list[str] = []
    visited: set[str] = set()
    # Staging layouts are built from symlinks, so follow them — with a visited
    # set, because a symlink loop would otherwise walk forever.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in visited:
            dirnames[:] = []
            continue
        visited.add(real)
        for entry in dirnames + filenames:
            full = os.path.join(dirpath, entry)
            names.append(os.path.relpath(full, root))
            if os.path.islink(full):
                targets.append(os.path.realpath(full))
            if entry in filenames and entry.endswith(".eval"):
                evals.append(full)
    return sorted(names), sorted(evals), sorted(set(targets))


# ---------------------------------------------------------------------------
# Header channels
# ---------------------------------------------------------------------------


def _flatten(prefix: str, node: Any) -> list[tuple[str, str]]:
    """Flatten nested metadata into ``(key_path, value)`` string pairs.

    Parameters
    ----------
    prefix : str
        Key path so far.
    node : Any
        The value at that path.

    Returns
    -------
    list[tuple[str, str]]
        ``{"a": {"b": 1}}`` -> ``[("a.b", "1")]``.
    """
    if isinstance(node, dict):
        out: list[tuple[str, str]] = []
        for k, v in node.items():
            out.extend(_flatten(f"{prefix}.{k}" if prefix else str(k), v))
        return out
    if isinstance(node, list):
        return [(prefix, json.dumps(node, sort_keys=True, default=str))]
    return [(prefix, "" if node is None else str(node))]


def header_fields(eval_path: str) -> tuple[list[tuple[str, str, str]], dict[str, Any], dict[str, str]]:
    """Read the naming fields of one ``.eval`` header.

    Only naming fields are returned; scorer results are never touched (the
    header is read with ``header_only=True`` and ``results`` is ignored).

    Parameters
    ----------
    eval_path : str
        Path to the log.

    Returns
    -------
    tuple[list[tuple[str, str, str]], dict[str, Any], dict[str, str]]
        ``(fields, task_args, profile)`` where each field is
        ``(channel, field, value)`` — e.g. ``("tags", "tags", "cond:c_3f9a")``,
        ``("metadata", "condition", "c_3f9a")`` —, ``task_args`` is the
        header's full ``task_args`` mapping, and ``profile`` is the run's shape
        (``{"task": "episode", "epochs": "20", "created": "2026-10-12"}``),
        compared across conditions to spot a log from another run.
    """
    from inspect_ai.log import read_eval_log

    spec = read_eval_log(eval_path, header_only=True).eval
    fields: list[tuple[str, str, str]] = []
    if spec.task_file:
        fields.append(("task_file", "task_file", str(spec.task_file)))
    for attr in ("task", "task_display_name", "task_registry_name"):
        value = getattr(spec, attr, None)
        if value:
            fields.append(("task", attr, str(value)))
    dataset = getattr(spec, "dataset", None)
    for attr in ("name", "location"):
        value = getattr(dataset, attr, None) if dataset is not None else None
        if value:
            fields.append(("dataset", attr, str(value)))
    for tag in spec.tags or []:
        fields.append(("tags", "tags", str(tag)))
    for key, value in _flatten("", spec.metadata or {}):
        # The key is checked as well as the value: a task that records
        # `{"treatment_arm": true}` leaks through its key.
        fields.append(("metadata", key, f"{key}={value}"))
    # The run's shape: same task, same epoch count, same day for every arm of a
    # round. `created` is ISO-8601, so its first ten characters are the date.
    config = getattr(spec, "config", None)
    profile = {
        "task": str(spec.task or ""),
        # Unset epochs means one epoch, so `None` and `1` compare equal.
        "epochs": str(getattr(config, "epochs", None) or 1),
        "created": str(spec.created or "")[:10],
    }
    return fields, dict(spec.task_args or {}), profile


def recorded_condition(fields: list[tuple[str, str, str]]) -> list[str]:
    """The condition name(s) the runner recorded in one header.

    Parameters
    ----------
    fields : list[tuple[str, str, str]]
        Output of `header_fields`.

    Returns
    -------
    list[str]
        Values of ``metadata.condition`` and of every ``cond:`` tag.
    """
    out: list[str] = []
    for channel, field, value in fields:
        if channel == "metadata" and field == "condition":
            out.append(value.split("=", 1)[1])
        elif channel == "tags" and value.startswith("cond:"):
            out.append(value[len("cond:"):])
    return out


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def check(label_map_path: str, source_path: str) -> tuple[list[Finding], list[str]]:
    """Run every channel check over the sources an Analyst request names.

    Parameters
    ----------
    label_map_path : str
        The iteration's private condition label map.
    source_path : str
        The transcript-source mapping (bare or inside a request).

    Returns
    -------
    tuple[list[Finding], list[str]]
        ``(blocking_findings, warnings)``.
    """
    real_names, map_ids, warnings = load_label_map(label_map_path)
    sources, request_strings = load_transcript_source(source_path)
    reference, notes = load_request_extras(source_path)
    if reference and (not os.path.isfile(reference) or not reference.endswith(".eval")):
        # The Analyst reads the reference as a log; a text dump here is both a
        # request error and a reference this check cannot compare with logs.
        warnings.append(
            f"behaviour_spec.reference_transcript is not a readable .eval file ({reference}) — "
            "could not confirm the original episode is absent from the transcript source"
        )
    tokens = build_tokens(real_names)
    real_tokens = [t for t in tokens if t.kind == "real name"]
    # The map lives in the investigation directory, so paths are judged from
    # there down when the sources sit under it: the iteration directory is
    # then checked even for a lone source (a legacy `00_resample` blocks).
    investigation_dir = os.path.dirname(os.path.abspath(label_map_path))
    findings: list[Finding] = []
    seen: set[tuple[str, str, str, str]] = set()

    def block(channel: str, label: str, where: str, token: Token, value: str, why: str = "") -> None:
        key = (channel, label, where, token.text.lower())
        if key in seen:
            return
        seen.add(key)
        findings.append(Finding(channel, label, where, token.text, f"{token.kind} in {value!r}{why}"))

    # --- Channel: the labels themselves ---
    for label in sources:
        for tok in find_tokens(label, tokens):
            block("label", label, label, tok, label)

    # --- Channel: the rest of the request (real names only — a behaviour
    # description may legitimately say "control") ---
    for key, value in request_strings:
        for tok in find_tokens(value, real_tokens):
            block("request", "-", f"request field {key}", tok, value[:120])

    # --- Channel 1: path components and symlink targets ---
    given = discriminating_components(sources, investigation_dir)
    resolved_paths = {lbl: os.path.realpath(p) for lbl, p in sources.items()}
    resolved = discriminating_components(resolved_paths, os.path.realpath(investigation_dir))
    # Root against which symlink targets *inside* a source are judged.
    resolved_values = list(resolved_paths.values())
    link_root = os.path.realpath(investigation_dir)
    if not all(_is_under(p, link_root) for p in resolved_values):
        link_root = os.path.commonpath(resolved_values) if len(resolved_values) > 1 else os.path.dirname(resolved_values[0])
    for label in sources:
        for comp in given[label]:
            for tok in find_tokens(comp, tokens):
                block("path", label, sources[label], tok, comp)
        if resolved_paths[label] != sources[label]:
            for comp in resolved[label]:
                for tok in find_tokens(comp, tokens):
                    block("symlink_target", label, resolved_paths[label], tok, comp)
    shapes = {mask_ids("/".join(resolved[lbl][:-1])) for lbl in sources}
    if len(shapes) > 1:
        warnings.append(
            "sources live under differently shaped directories "
            f"({', '.join(sorted(s or '.' for s in shapes))}); the Analyst can tell which conditions "
            "came from where — e.g. a frozen pool from an earlier iteration. Stage by copying into "
            "one neutral directory if that matters, and record it in the caveats either way"
        )

    # --- Channel 1b: names under each source; collect the headers ---
    per_cond_fields: dict[str, set[tuple[str, str, str]]] = {}
    per_cond_args: dict[str, list[dict[str, Any]]] = {}
    per_cond_taskfile_shape: dict[str, set[str]] = {}
    per_cond_profile: dict[str, dict[str, set[str]]] = {}
    file_of: dict[tuple[str, tuple[str, str, str]], str] = {}
    for label, root in sources.items():
        names, evals, link_targets = walk_source(root)
        # The original episode is never a condition (block, any channel).
        for ev in reference_copies(evals, reference):
            findings.append(Finding(
                "reference", label, ev, "reference_transcript",
                "this .eval is the request's behaviour_spec.reference_transcript (same file or "
                "byte-identical copy); the original episode defines the behaviour class and is never a "
                "measured condition — the transcript source is the fresh resample pool only",
            ))
        for target in link_targets:
            if _is_under(target, link_root):
                comps = Path(os.path.relpath(target, link_root)).parts
            else:
                comps = Path(target).parts[-2:]
            for comp in comps:
                for tok in find_tokens(comp, tokens):
                    block("symlink_target", label, target, tok, comp)
        for rel in names:
            for comp in Path(rel).parts:
                stem = comp[:-5] if comp.endswith(".eval") else None
                m = _INSPECT_LOGNAME_RE.match(stem) if stem is not None else None
                if m:
                    # An Inspect-named log: the task segment is the eval's own
                    # task name and is judged with the header fields below.
                    per_cond_fields.setdefault(label, set()).add(("task", "log filename", m.group(2)))
                    file_of[(label, ("task", "log filename", m.group(2)))] = os.path.join(root, rel)
                    continue
                for tok in find_tokens(comp, tokens):
                    block("filename", label, os.path.join(root, rel), tok, comp)
        if not evals:
            warnings.append(f"{label}: no .eval files under {root} — header channels unchecked, and the Analyst will find no transcripts")
        readable = 0
        for ev in evals:
            try:
                fields, task_args, profile = header_fields(ev)
            except Exception as exc:  # noqa: BLE001 — any unreadable header is reported, not fatal
                warnings.append(f"{label}: could not read header of {ev} ({type(exc).__name__}: {exc}) — header channels unchecked for this file")
                continue
            readable += 1
            for key, value in profile.items():
                per_cond_profile.setdefault(label, {}).setdefault(key, set()).add(value)
            for f in fields:
                per_cond_fields.setdefault(label, set()).add(f)
                file_of.setdefault((label, f), ev)
            per_cond_args.setdefault(label, []).append(task_args)
            per_cond_taskfile_shape.setdefault(label, set()).update(
                mask_ids(v) for c, _, v in fields if c == "task_file"
            )
            # The unmapped-condition rule: a recorded condition name the map
            # does not list cannot be vouched for as opaque.
            if map_ids is not None:
                for name in recorded_condition(fields):
                    key = ("unmapped", label, "", name)
                    if name not in map_ids and key not in seen:
                        seen.add(key)
                        findings.append(Finding(
                            "metadata", label, ev, name,
                            "recorded condition name is not one of the label map's opaque ids",
                        ))
        # Fail closed: logs whose headers cannot be read at all are logs whose
        # naming nobody has verified, and the Analyst may still read them.
        if evals and not readable:
            findings.append(Finding(
                "header", label, root, "-",
                f"none of {len(evals)} .eval header(s) could be read, so channels 2-5 are unverified",
            ))

    # --- Channels 2-4: header fields, judged against the other conditions ---
    # A role word in a value every condition shares cannot reveal the mapping
    # (typically the eval's own name), so it only warns. With a single source
    # there is nothing to compare against, so only the environment-owned
    # fields (task name, dataset) get that benefit of the doubt; tags,
    # task_file and metadata are the runner's and the lead's own naming.
    all_sets = [per_cond_fields.get(lbl, set()) for lbl in sources]
    common = set.intersection(*all_sets) if all_sets else set()
    if len(sources) < 2:
        common = {f for f in common if f[0] in ("task", "dataset")}
    common_warned: set[tuple[str, str]] = set()
    for label in sources:
        for f in sorted(per_cond_fields.get(label, set())):
            channel, field, value = f
            for tok in find_tokens(value, tokens):
                if tok.kind == "role word" and f in common:
                    key = (tok.text, value)
                    if key not in common_warned:
                        common_warned.add(key)
                        warnings.append(
                            f"role word {tok.text!r} in {channel} {field} {value!r}, identical in every "
                            "condition — it cannot reveal the mapping (typically the eval's own name)"
                        )
                    continue
                block(channel, label, file_of.get((label, f), sources[label]), tok, value,
                      "" if tok.kind == "real name" else " (distinguishes this condition)")

    # --- Shape of task_file paths ---
    tf_shapes = {frozenset(s) for s in per_cond_taskfile_shape.values()}
    if len(tf_shapes) > 1:
        warnings.append(
            "task_file paths differ in shape between conditions once ids are masked "
            f"({'; '.join(sorted(', '.join(sorted(s)) for s in tf_shapes))}): the odd condition can be "
            "singled out — name every arm's environment copy by its id, the control's included"
        )

    # --- Run shape: task name, epoch count, created date (warn) ---
    warnings.extend(profile_warnings(per_cond_profile))

    # --- Channel 5: task_args (warn, never block) ---
    warnings.extend(task_args_warnings(per_cond_args, tokens))

    # --- execution_notes: role and provenance words (warn) ---
    warnings.extend(notes_warnings(notes))
    return findings, warnings


def reference_copies(evals: list[str], reference: str | None) -> list[str]:
    """The transcript-source logs that are the reference transcript itself.

    Parameters
    ----------
    evals : list[str]
        ``.eval`` paths under one transcript source.
    reference : str | None
        The request's ``behaviour_spec.reference_transcript``, if any.

    Returns
    -------
    list[str]
        Every entry of ``evals`` that resolves to the same file as
        ``reference`` or has identical bytes (sizes are compared first, so
        only a same-sized log is ever hashed).
    """
    if not reference or not os.path.isfile(reference):
        return []
    ref_real = os.path.realpath(reference)
    ref_size = os.path.getsize(ref_real)
    ref_hash: str | None = None
    out: list[str] = []
    for ev in evals:
        real = os.path.realpath(ev)
        if real == ref_real:
            out.append(ev)
            continue
        try:
            if os.path.getsize(real) != ref_size:
                continue
            ref_hash = ref_hash or sha256_file(ref_real)
            if sha256_file(real) == ref_hash:
                out.append(ev)
        except OSError:
            # A dangling link or unreadable log is not the reference; its
            # header failure is reported by the header channel.
            continue
    return out


def profile_warnings(per_cond_profile: dict[str, dict[str, set[str]]]) -> list[str]:
    """Name every run-shape field that differs between conditions.

    Parameters
    ----------
    per_cond_profile : dict[str, dict[str, set[str]]]
        Label → field (``task``, ``epochs``, ``created``) → the values seen
        across that condition's logs.

    Returns
    -------
    list[str]
        One warning per differing field, e.g. ``conditions differ in header
        epochs (condition_A: 20; condition_B: 30)``.
    """
    if len(per_cond_profile) < 2:
        return []
    out: list[str] = []
    for key in ("task", "epochs", "created"):
        values = {lbl: frozenset(p.get(key, set())) for lbl, p in per_cond_profile.items()}
        differs = len(set(values.values())) > 1
        if differs and key == "created":
            # A round that straddles midnight UTC spans two adjacent dates;
            # only a gap of more than a day says "another run".
            differs = _date_span_days(set().union(*values.values())) > 1
        if differs:
            shown = "; ".join(f"{lbl}: {', '.join(sorted(v)) or '?'}" for lbl, v in sorted(values.items()))
            out.append(
                f"conditions differ in header {key} ({shown}) — a log from another run or another day "
                "sits among this invocation's arms and the Analyst can single it out. Every condition "
                "should come from the same execution; if one is a frozen pool, record the exposure"
            )
    return out


def _date_span_days(dates: set[str]) -> int:
    """Days between the earliest and latest of some ISO dates.

    Parameters
    ----------
    dates : set[str]
        ``YYYY-MM-DD`` strings; unparseable or empty ones are ignored.

    Returns
    -------
    int
        ``{"2026-10-11", "2026-10-12"}`` -> 1; 0 when fewer than two parse.
    """
    from datetime import date

    parsed = []
    for d in dates:
        try:
            parsed.append(date.fromisoformat(d))
        except ValueError:
            continue
    return (max(parsed) - min(parsed)).days if len(parsed) > 1 else 0


def notes_warnings(notes: list[str]) -> list[str]:
    """Flag role and provenance words in ``execution_notes``.

    Parameters
    ----------
    notes : list[str]
        The request's execution notes.

    Returns
    -------
    list[str]
        One warning per note naming the words it uses
        (``"an earlier run"`` -> ``earlier``).
    """
    # Role words reuse `build_tokens`' inflection rules (`resampled`, not
    # `controller`); provenance words allow only a plural or adverb ending, so
    # `previously` and `references` hit but `priority` does not.
    role_tokens = [t for t in build_tokens([]) if t.kind == "role word"]
    provenance = re.compile(rf"(?<![a-z])({'|'.join(PROVENANCE_WORDS)})(?:s|ly)?(?![a-z])", re.I)
    out: list[str] = []
    for note in notes:
        hits = sorted({t.text for t in find_tokens(note, role_tokens)}
                      | {m.group(1).lower() for m in provenance.finditer(note)})
        if hits:
            out.append(
                f"execution_notes use role/provenance word(s) {', '.join(hits)} in {note[:120]!r} — "
                "notes carry non-directional execution facts only; where a condition came from "
                "(an earlier run, the original episode) is a role disclosure"
            )
    return out


def task_args_warnings(per_cond_args: dict[str, list[dict[str, Any]]], tokens: list[Token]) -> list[str]:
    """Name every ``-T`` argument whose value differs between conditions.

    Parameters
    ----------
    per_cond_args : dict[str, list[dict]]
        Label → the ``task_args`` of each of its logs.
    tokens : list[Token]
        Forbidden tokens; a hit inside a ``task_args`` value sharpens the
        warning but still does not block.

    Returns
    -------
    list[str]
        Zero or more warning lines. Besides the differing-argument line, a
        real name inside *any* ``task_args`` value is named (the Analyst reads
        it whether or not it differs), as is a role word inside a differing one.
    """
    keys = sorted({k for runs in per_cond_args.values() for args in runs for k in args})
    differing: list[str] = []
    for k in keys:
        values = {
            label: frozenset(json.dumps(a.get(k), sort_keys=True, default=str) for a in runs)
            for label, runs in per_cond_args.items()
        }
        if len(per_cond_args) > 1 and len(set(values.values())) > 1:
            differing.append(k)
    out: list[str] = []
    if differing:
        out.append(
            f"task_args differ between conditions on: {', '.join(differing)}. The Analyst can read "
            "every -T value in the log header, so these conditions are distinguishable by their "
            "arguments. Prefer file-edit variants (environment copies named by id) when blinding "
            "matters; otherwise record this leak in the report's caveats."
        )
    for label, runs in per_cond_args.items():
        for args in runs:
            for k in keys:
                hits = find_tokens(json.dumps(args.get(k), default=str), tokens)
                if k not in differing:
                    hits = [t for t in hits if t.kind == "real name"]
                for tok in hits:
                    out.append(
                        f"{label}: task_args {k}={args.get(k)!r} contains {tok.kind} {tok.text!r} — "
                        "rename the referenced file by the condition's id"
                    )
    return sorted(set(out), key=out.index)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Parameters
    ----------
    argv : list[str] | None
        Arguments (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        0 clean, 1 blocking hit(s), 2 usage error.
    """
    args = sys.argv[1:] if argv is None else argv
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    if len(args) != 2:
        print("usage: check_blinding.py <label_map.json> <transcript_source.json>")
        return 2
    try:
        findings, warnings = check(args[0], args[1])
    except UsageError as exc:
        print(f"check_blinding: usage error: {exc}")
        return 2
    except ImportError as exc:
        print(f"check_blinding: usage error: inspect_ai is needed to read log headers ({exc}); run with `uv run`")
        return 2

    for w in warnings:
        print(f"WARN   {w}")
    for f in findings:
        print(f"BLOCK  [{f.channel}] {f.label}: token {f.token!r} — {f.detail} — {f.where}")
    if findings:
        print(f"check_blinding: BLOCKED — {len(findings)} blocking hit(s); do not invoke the Analyst on these sources.")
        return 1
    print(f"check_blinding: clean ({len(warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
