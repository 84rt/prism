"""Render Inspect `.eval` logs as plain-text, one file per sample.

`.eval` logs are zip archives, not text. A sub-agent handed one to *read* —
the Mapper checking the episode-as-run, the Critic reading transcripts for its
independent reading — gets nothing, and gets it silently: the read succeeds and
returns bytes that no reasoning can be built on. Every run that hit this
reinvented the same workaround mid-investigation (dump to text, pass the text
path). This script is that workaround, shipped once, so the Diagnostician passes
dump *paths* to any log-reading sub-agent and never the `.eval` itself.

It is a rendering step, not an analysis step: it applies no judgement and
computes no rates.

**Scores are omitted unless `--with-scores` is passed.** The score blackout
(no scorer output reaches the Diagnostician before the Analyst reports) is the
invariant most easily broken by a convenience tool, and the dumps are typically
made *before* the Analyst reports — to brief the Critic on a round's transcripts,
say. Emitting scores by default would make every dump a leak surface. Pass the
flag only for logs whose scores are already known to the Diagnostician (the
reference episode; logs the Analyst has already reported on).

Usage:
    uv run python scripts/dump_transcripts.py <log.eval | dir> [...] --out <dir>
        [--sample-id ID] [--epoch N] [--cap CHARS] [--with-scores]

Directories are searched recursively for `*.eval`. Each log gets its own
sub-directory under `--out`, holding one `<sample_id>_epoch_<n>.txt` per
sample. For a directory input the log's path *relative to that directory* is
mirrored, so the Executor's `logs/<condition>/` layout survives and a whole
condition can be handed over as one directory; a file input lands directly
under `--out`:

    dump_transcripts.py iterations/01_round1/logs --out iterations/01_round1/transcripts
    iterations/01_round1/transcripts/
      c_8b21/
        2026-09-08T22-44-52-00-00_agentic-misalignment_SBpaWBEn5936WCYGdiiumS/
          blackmail_explicit-america_restriction_epoch_1.txt
          blackmail_explicit-america_restriction_epoch_2.txt
          ...
      c_3f9a/
        ...

Mirroring rather than flattening matters: Inspect log stems are unique per
*run*, but investigations copy logs (a reference episode into
`artefacts/reference/`, a quarantined run alongside its re-run), and a flat
layout silently overwrites one copy with the other. Two logs that would still
land in the same directory are reported as an error rather than merged.

A one-line summary per log goes to stdout (`<log>: <n> samples -> <dir>`);
logs that cannot be read go to stderr and set exit code 1 without stopping the
rest of the batch.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, TextIO

from inspect_ai.log import read_eval_log

# Per-block truncation keeps a 66-message agentic transcript readable in one
# sub-agent context. 2500 chars per block matched what the runs converged on.
DEFAULT_CAP = 2500


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def trunc(text: str | None, cap: int) -> str:
    """Truncate `text` to roughly `cap` characters, keeping both ends.

    Parameters
    ----------
    text : str | None
        The text to truncate; None is treated as empty.
    cap : int
        Maximum characters to keep. 0 or negative disables truncation.

    Returns
    -------
    str
        `text` unchanged if it fits, otherwise its head and tail halves with an
        elision marker between them, e.g. ``"...[1234 chars elided]..."``. Both
        ends are kept because the start of a tool result carries its command and
        the end carries its outcome — the middle is usually the least
        informative part.
    """
    text = text or ""
    if cap <= 0 or len(text) <= cap:
        return text
    half = cap // 2
    return text[:half] + f"\n...[{len(text) - cap} chars elided]...\n" + text[-half:]


def safe_name(value: Any) -> str:
    """Make a sample id or log stem safe to use as a filename component.

    Parameters
    ----------
    value : Any
        The raw value (sample ids may be ints or strings, and strings may
        contain path separators or spaces).

    Returns
    -------
    str
        The value with every run of characters outside ``[A-Za-z0-9._-]``
        replaced by a single underscore, e.g. ``"a/b c" -> "a_b_c"``.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_reasoning(block: Any, cap: int) -> str:
    """Render a reasoning content block, choosing the readable trace.

    Parameters
    ----------
    block : ContentReasoning
        A content block with ``type == "reasoning"``.
    cap : int
        Truncation cap passed through to `trunc`.

    Returns
    -------
    str
        The reasoning wrapped in ``<reasoning>`` tags when the raw trace is
        readable (e.g. DeepSeek-R1, Claude), or the provider summary wrapped in
        ``<reasoning-summary>`` tags when the raw trace is encrypted/redacted
        (e.g. GPT-5, where `redacted` is set and only `summary` is legible).
    """
    raw = getattr(block, "reasoning", None) or ""
    summary = getattr(block, "summary", None)
    redacted = bool(getattr(block, "redacted", False))
    if redacted or not raw.strip():
        body = summary or "[reasoning redacted by provider; no summary]"
        return f"<reasoning-summary>\n{trunc(body, cap)}\n</reasoning-summary>"
    return f"<reasoning>\n{trunc(raw, cap)}\n</reasoning>"


def render_message(index: int, msg: Any, cap: int) -> str:
    """Render one chat message as a plain-text block.

    Parameters
    ----------
    index : int
        Position of the message in the sample's message list.
    msg : ChatMessage
        An Inspect chat message (system, user, assistant or tool).
    cap : int
        Truncation cap passed through to `trunc`.

    Returns
    -------
    str
        A header line naming the role (and, for tool messages, the function
        that produced the result), the content blocks in order, then one
        ``>>> TOOL CALL name({...})`` line per tool call the assistant made.
    """
    role = msg.role.upper()
    function = getattr(msg, "function", None)
    header = f"----- [{index}] {role}" + (f" ({function})" if function else "") + " -----"
    lines = [header]

    if getattr(msg, "error", None):
        lines.append(f"[tool error: {msg.error}]")

    content = msg.content
    if isinstance(content, str):
        lines.append(trunc(content, cap))
    else:
        for block in content:
            kind = getattr(block, "type", None)
            if kind == "reasoning":
                lines.append(render_reasoning(block, cap))
            elif kind == "text":
                lines.append(trunc(block.text, cap))
            else:
                # Images, audio, documents: name the block so the reader knows
                # something non-textual was there, without trying to render it.
                lines.append(f"<{kind}>")

    for call in getattr(msg, "tool_calls", None) or []:
        try:
            args = json.dumps(call.arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(call.arguments)
        lines.append(f"  >>> TOOL CALL {call.function}({trunc(args, cap)})")

    return "\n".join(lines)


def write_sample(sample: Any, log: Any, log_path: Path, out: TextIO, cap: int, with_scores: bool) -> None:
    """Write one sample's transcript to an open text stream.

    Parameters
    ----------
    sample : EvalSample
        The sample to render.
    log : EvalLog
        The log it came from, for the task/model header.
    log_path : Path
        Path of the source `.eval`, recorded in the header so a dump can always
        be traced back to its log.
    out : TextIO
        Destination stream.
    cap : int
        Truncation cap passed through to `trunc`.
    with_scores : bool
        Whether to emit the sample's scores. See the module docstring for why
        this defaults to off.
    """
    print(f"### LOG {log_path}", file=out)
    print(f"### TASK {log.eval.task}   MODEL {log.eval.model}", file=out)
    print(f"### SAMPLE {sample.id} epoch {sample.epoch}   messages={len(sample.messages)}", file=out)
    # Termination metadata: a reader should know a transcript was cut short
    # before drawing conclusions from where it stops.
    if sample.limit is not None:
        print(f"### LIMIT {sample.limit.type}: {sample.limit.limit}", file=out)
    if sample.error is not None:
        print(f"### ERROR {sample.error.message}", file=out)
    if with_scores and sample.scores:
        for name, score in sample.scores.items():
            print(f"### SCORE {name}: {score.value}", file=out)
            if score.explanation:
                print(f"### EXPLANATION {name}: {trunc(score.explanation, cap)}", file=out)
    print("=" * 100, file=out)
    for i, msg in enumerate(sample.messages):
        print("", file=out)
        print(render_message(i, msg, cap), file=out)


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------


def find_logs(paths: Iterable[str]) -> list[tuple[Path, Path]]:
    """Expand a mix of `.eval` files and directories into (log, output-subdir) pairs.

    Parameters
    ----------
    paths : Iterable[str]
        Files or directories; directories are searched recursively, following
        directory symlinks. Following matters because opaque-label staging
        (``staged/condition_A -> logs/v3``) is done with symlinks, and a
        blinded handoff is exactly when a whole staged directory gets dumped.

    Returns
    -------
    list[tuple[Path, Path]]
        One ``(log_path, rel_dir)`` per `.eval` found, sorted by log path so a
        re-run over the same inputs produces the same order. ``rel_dir`` is
        the sub-directory the log's dumps go under: for a directory input, the
        log's path relative to that directory with the `.eval` suffix dropped
        (``logs/control/x.eval`` under input ``logs`` -> ``control/x``); for a
        file input, just the stem. Every path component is passed through
        `safe_name`.
    """
    found: dict[Path, Path] = {}
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for dirpath, _dirnames, filenames in os.walk(p, followlinks=True):
                for name in sorted(filenames):
                    if not name.endswith(".eval"):
                        continue
                    log = Path(dirpath) / name
                    rel = log.relative_to(p).with_suffix("")
                    found.setdefault(log, Path(*(safe_name(part) for part in rel.parts)))
        elif p.is_file():
            found.setdefault(p, Path(safe_name(p.stem)))
        else:
            print(f"warning: {p} does not exist, skipping", file=sys.stderr)
    return sorted(found.items())


def dump_log(
    log_path: Path,
    log_dir: Path,
    cap: int,
    with_scores: bool,
    sample_id: str | None = None,
    epoch: int | None = None,
) -> int:
    """Dump every (selected) sample of one log to its own text file.

    Parameters
    ----------
    log_path : Path
        The `.eval` to read.
    log_dir : Path
        Directory the sample files are written into (created if missing).
    cap : int
        Truncation cap passed through to `trunc`.
    with_scores : bool
        Whether to emit scores (see module docstring).
    sample_id : str | None
        If given, only samples whose id (as a string) matches are dumped.
    epoch : int | None
        If given, only samples of this epoch are dumped.

    Returns
    -------
    int
        The number of sample files written. A log with no samples (e.g.
        status `error` before any completed) yields an empty directory and 0.
    """
    log = read_eval_log(str(log_path))
    log_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for sample in log.samples or []:
        if sample_id is not None and str(sample.id) != sample_id:
            continue
        if epoch is not None and sample.epoch != epoch:
            continue
        target = log_dir / f"{safe_name(sample.id)}_epoch_{sample.epoch}.txt"
        with target.open("w", encoding="utf-8") as fh:
            write_sample(sample, log, log_path, fh, cap, with_scores)
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, dump every log, and report per-log counts.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments; defaults to `sys.argv[1:]`.

    Returns
    -------
    int
        0 if every log was dumped, 1 if any log failed to read (the rest of
        the batch still runs), 2 if no logs were found at all.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help=".eval files or directories (searched recursively)")
    parser.add_argument("--out", required=True, help="output directory; one sub-directory per log is created under it")
    parser.add_argument("--sample-id", default=None, help="dump only this sample id")
    parser.add_argument("--epoch", type=int, default=None, help="dump only this epoch")
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP, help=f"per-block truncation in chars, 0 = none (default {DEFAULT_CAP})")
    parser.add_argument("--with-scores", action="store_true", help="include scorer output (off by default: score blackout)")
    args = parser.parse_args(argv)

    logs = find_logs(args.paths)
    if not logs:
        print("error: no .eval logs found", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    failures = 0
    total_samples = 0
    claimed: dict[Path, Path] = {}  # output dir -> the log that wrote it, this run
    for log_path, rel_dir in logs:
        log_dir = out_root / rel_dir
        if log_dir in claimed:
            # Two distinct logs mapping to one directory would overwrite each
            # other's sample files sample-id by sample-id, leaving a plausible
            # looking but mixed dump. Refuse rather than merge.
            failures += 1
            print(f"error: {log_path}: output dir {log_dir} already written by {claimed[log_dir]}", file=sys.stderr)
            continue
        claimed[log_dir] = log_path
        try:
            n = dump_log(log_path, log_dir, args.cap, args.with_scores, args.sample_id, args.epoch)
        except Exception as exc:  # noqa: BLE001 — one unreadable log must not abort the batch
            failures += 1
            print(f"error: {log_path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        total_samples += n
        print(f"{log_path}: {n} samples -> {log_dir}")

    print(f"\n{len(logs) - failures}/{len(logs)} logs dumped, {total_samples} sample files written to {out_root}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
