"""Report which provider API keys are present — never their values.

Both lead agents check credentials at scoping (the Orchestrator's Phase 1 key
check, the Diagnostician's Phase 0 cross-family Critic gate). Doing that by
hand means reading or sourcing ``.env``: ``cat .env``, ``set -a; . ./.env``,
``python -c 'from dotenv import …'``. Each of those puts secrets into the
transcript, and the shipped settings deny reading ``.env``, so in an unattended
run the attempt either stalls on a permission prompt or is refused. This
script is the sanctioned route: it reads the environment and the nearest
``.env`` itself, and prints only *whether* each key is set.

The leads never need the values: ``scripts/launch_subagent.py`` and Inspect
load ``.env`` themselves when they run.

Usage
-----
    uv run --no-project python scripts/check_keys.py [--key NAME ...] [--env-file PATH] [--json]

Checked by default: ``ANTHROPIC_API_KEY``, ``OPENROUTER_API_KEY``,
``OPENAI_API_KEY``; ``--key`` adds more (repeatable). The ``.env`` used is
``--env-file`` if given, else the first ``.env`` found walking up from the
current directory (the same file ``python-dotenv`` and Inspect pick up when
run from here). The default is the normal case; ``--env-file`` is for another
file, and the shipped settings deny any command naming a bare ``.env``, so
pass it as a path to a differently named file.

Example output::

    .env: /workspaces/project/.env
    ANTHROPIC_API_KEY   present (environment, .env)
    OPENROUTER_API_KEY  present (.env)
    OPENAI_API_KEY      missing

Exit status is always 0: a missing key is information for the lead, not an
error of this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# The providers the scaffold routes to: Anthropic direct, OpenRouter, and
# OpenAI (a target model's provider; not a sub-agent route).
DEFAULT_KEYS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")


def find_env_file(start: str) -> str | None:
    """The nearest ``.env`` at or above ``start``.

    Parameters
    ----------
    start : str
        Directory to start from.

    Returns
    -------
    str | None
        Absolute path of the first ``.env`` found walking upward, or ``None``.
    """
    d = os.path.abspath(start)
    while True:
        candidate = os.path.join(d, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def env_file_names(path: str) -> dict[str, bool]:
    """Which names a ``.env`` file assigns, and whether each value is non-empty.

    Values are parsed only far enough to tell empty from non-empty; nothing
    is returned or printed that would reveal them.

    Parameters
    ----------
    path : str
        The ``.env`` file.

    Returns
    -------
    dict[str, bool]
        Name → ``True`` when assigned a non-empty value
        (``KEY=abc`` -> ``{"KEY": True}``, ``KEY=""`` -> ``{"KEY": False}``).
        Comments, blank lines and ``export`` prefixes are handled; an
        unreadable file gives ``{}``.
    """
    out: dict[str, bool] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        # Strip an inline comment and surrounding quotes before judging emptiness.
        value = value.split(" #", 1)[0].strip().strip("'\"")
        out[name.strip()] = bool(value)
    return out


def check(keys: list[str], env_file: str | None) -> dict[str, Any]:
    """Presence of each key in the environment and in the ``.env`` file.

    Parameters
    ----------
    keys : list[str]
        Variable names to check.
    env_file : str | None
        The ``.env`` file to consult, if any.

    Returns
    -------
    dict
        ``{"env_file": path | None, "keys": {name: {"status": s, "sources": [...]}}}``
        where ``s`` is ``present`` (non-empty somewhere), ``empty`` (assigned
        but blank everywhere it appears) or ``missing``, and ``sources`` lists
        where it is non-empty (``environment``, ``.env``).
    """
    file_names = env_file_names(env_file) if env_file else {}
    report: dict[str, Any] = {"env_file": env_file, "keys": {}}
    for name in keys:
        sources: list[str] = []
        seen = False
        if name in os.environ:
            seen = True
            if os.environ[name].strip():
                sources.append("environment")
        if name in file_names:
            seen = True
            if file_names[name]:
                sources.append(".env")
        status = "present" if sources else ("empty" if seen else "missing")
        report["keys"][name] = {"status": status, "sources": sources}
    return report


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Parameters
    ----------
    argv : list[str] | None
        Arguments (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        Always 0.
    """
    parser = argparse.ArgumentParser(description="Report which API keys are present, never their values.")
    parser.add_argument("--key", action="append", default=[], metavar="NAME", help="an extra variable to check (repeatable)")
    parser.add_argument("--env-file", default=None, help="the .env to consult (default: nearest at or above the cwd)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    keys = list(dict.fromkeys(list(DEFAULT_KEYS) + args.key))
    env_file = os.path.abspath(args.env_file) if args.env_file else find_env_file(os.getcwd())
    report = check(keys, env_file)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f".env: {env_file or 'none found'}")
    width = max(len(k) for k in keys) + 2
    for name, entry in report["keys"].items():
        where = f" ({', '.join(entry['sources'])})" if entry["sources"] else ""
        print(f"{name.ljust(width)}{entry['status']}{where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
