"""Print fresh opaque condition ids (``c_<4 hex>``).

Every condition a lead agent runs is named by an opaque execution id so no
real name or role word reaches the Analyst through a path or a log header
(``check_blinding.py``). Minting them by hand invited shell loops over
``openssl rand`` or ``$RANDOM`` — denied idioms that cost turns — and, across
iterations, a collision with an id an earlier map already used. This script
does both jobs: it draws ids from the OS's random source and skips any id
already present in the given label map(s).

Usage
-----
    uv run --no-project python scripts/new_ids.py [N] [--map PATH ...]

``N`` defaults to 1. ``--map`` (repeatable) names a
``CONDITION_LABEL_MAP_iter<N>.json`` — or a directory, whose every
``CONDITION_LABEL_MAP*.json`` is read — whose ids must not be reused; any
``c_<4 hex>`` string anywhere in the file counts. One id per line::

    c_3f9a
    c_8b21

Exit codes: 0 success, 2 usage error (an unreadable map, or more ids
requested than remain free).
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import secrets
import sys

# The id shape `check_blinding.py` treats as opaque.
# Lookarounds rather than `\b`, because `_` is a word character: `iter_c_3f9a`
# and `c_3f9a_x` must still yield `c_3f9a`.
OPAQUE_ID_RE = re.compile(r"(?<![0-9A-Za-z])c_[0-9a-f]{4}(?![0-9a-f])")

# 16^4 possible ids; keep well clear of exhausting them.
ID_SPACE = 16 ** 4


def used_ids(paths: list[str]) -> set[str]:
    """Every opaque id already present in the given label maps.

    Parameters
    ----------
    paths : list[str]
        Label-map files, or directories holding ``CONDITION_LABEL_MAP*.json``.

    Returns
    -------
    set[str]
        Ids found anywhere in the files' text (keys, values, label tables).

    Raises
    ------
    OSError
        If a named file cannot be read.
    """
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "CONDITION_LABEL_MAP*.json"))))
        else:
            files.append(p)
    found: set[str] = set()
    for f in files:
        with open(f, encoding="utf-8") as fh:
            found.update(OPAQUE_ID_RE.findall(fh.read()))
    return found


def fresh_ids(n: int, taken: set[str]) -> list[str]:
    """Draw ``n`` distinct ids, none in ``taken``.

    Parameters
    ----------
    n : int
        How many to draw.
    taken : set[str]
        Ids that must not be returned.

    Returns
    -------
    list[str]
        ``n`` ids such as ``["c_3f9a", "c_8b21"]``.
    """
    out: list[str] = []
    avoid = set(taken)
    while len(out) < n:
        cid = f"c_{secrets.randbelow(ID_SPACE):04x}"
        if cid not in avoid:
            avoid.add(cid)
            out.append(cid)
    return out


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Parameters
    ----------
    argv : list[str] | None
        Arguments (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        0 on success, 2 on a usage error.
    """
    parser = argparse.ArgumentParser(description="Print fresh opaque condition ids (c_<4 hex>).")
    parser.add_argument("n", nargs="?", type=int, default=1, help="how many ids (default 1)")
    parser.add_argument("--map", action="append", default=[], metavar="PATH",
                        help="a label map (or a directory of them) whose ids must not be reused; repeatable")
    args = parser.parse_args(argv)

    try:
        taken = used_ids(args.map)
    except OSError as exc:
        print(f"new_ids: usage error: cannot read label map: {exc}", file=sys.stderr)
        return 2
    if args.n < 1 or args.n > (ID_SPACE - len(taken)) // 2:
        print(f"new_ids: usage error: cannot draw {args.n} ids ({len(taken)} already taken)", file=sys.stderr)
        return 2
    for cid in fresh_ids(args.n, taken):
        print(cid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
