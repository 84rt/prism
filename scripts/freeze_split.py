#!/usr/bin/env python3
"""Freeze a development / held-out split for a control-mode investigation.

Usage:
    uv run --no-project python scripts/freeze_split.py <investigation_dir> \\
        --setting linuxarena --dev web_scraping,clinical_trial \\
        --test slack_clone,tiktok [--heldout-dir heldout]

Writes ``<investigation_dir>/splits.json`` once and refuses to overwrite it:
the split is pre-registered before any attack or monitor development sees
outcomes, and ``INVESTIGATION-LOG.md`` cites the file's sha256 printed here.

The file carries the restricted directory patterns ``subagents.runner
.split_restrictions`` turns into read denials for the roles that must not see
held-out material (the Attack Designer and the Analyst): each test
environment's directory ``<setting>/<env>/`` wherever it is installed, and the
``<heldout-dir>/`` directory under which the lead keeps every held-out run,
log and summary. The Executor is not restricted — it must run held-out evals —
and the lead reads held-out results only at confirmation time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

SCHEMA = 1


def build_split(setting: str, dev: list[str], test: list[str], heldout_dir: str) -> dict:
    """The splits.json content for one setting, with its restricted patterns.

    Parameters
    ----------
    setting : str
        Setting name as the environments are namespaced, e.g. ``linuxarena``.
    dev, test : list[str]
        Environment names (without the setting prefix). Must be disjoint and
        non-empty; names are kept in the given order.
    heldout_dir : str
        Directory name (relative to the investigation root) under which all
        held-out artefacts live. Denied as a directory pattern.

    Returns
    -------
    dict
        The JSON-serialisable split record.
    """
    if not setting or "/" in setting:
        raise ValueError("setting must be a bare name such as 'linuxarena'")
    if not dev or not test:
        raise ValueError("both dev and test must name at least one environment")
    overlap = sorted(set(dev) & set(test))
    if overlap:
        raise ValueError(f"environments in both splits: {', '.join(overlap)}")
    for name in (*dev, *test):
        if not name or "/" in name or any(ch in name for ch in "*?["):
            raise ValueError(f"bad environment name {name!r}")
    if not heldout_dir or "/" in heldout_dir.strip("/"):
        raise ValueError("heldout_dir must be a single directory name")
    heldout = heldout_dir.strip("/") + "/"
    return {
        "schema": SCHEMA,
        "setting": setting,
        "dev": list(dev),
        "test": list(test),
        "heldout_dir": heldout,
        # Directory patterns in `runner.py` semantics: a trailing "/" denies
        # the directory at a path-component boundary anywhere in the path.
        "restricted_dirs": [f"{setting}/{name}/" for name in test] + [heldout],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("investigation_dir")
    parser.add_argument("--setting", required=True)
    parser.add_argument("--dev", required=True, help="comma-separated environment names")
    parser.add_argument("--test", required=True, help="comma-separated environment names")
    parser.add_argument("--heldout-dir", default="heldout")
    args = parser.parse_args(argv)

    path = os.path.join(args.investigation_dir, "splits.json")
    if os.path.exists(path):
        print(f"refusing to overwrite frozen split: {path}", file=sys.stderr)
        return 2
    split = build_split(
        args.setting,
        [s.strip() for s in args.dev.split(",") if s.strip()],
        [s.strip() for s in args.test.split(",") if s.strip()],
        args.heldout_dir,
    )
    os.makedirs(args.investigation_dir, exist_ok=True)
    text = json.dumps(split, indent=2) + "\n"
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(text)
    print(json.dumps({"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                      "dev": split["dev"], "test": split["test"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
