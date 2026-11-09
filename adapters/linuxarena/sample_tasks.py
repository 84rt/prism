#!/usr/bin/env python3
"""Draw a reproducible sample of LinuxArena main tasks for a batch.

Usage (from the Control Tower venv directory):
    python3 -I sample_tasks.py --splits investigations/<name>/splits.json \\
        --split dev --per-env 4 --seed 20261109 [--exclude-used <request.json> ...]

Prints a JSON list of {"env": "linuxarena/<env>", "main_task": <id>} to stdout.

Always excluded:
- whole-codebase rewrites (`*_rewrite_in_*`): 100-200 agent actions each,
  so a full-history monitor costs roughly quadratically more on them, and
  they are unrepresentative of the task distribution;
- tasks listed in `BROKEN`, whose own setup fails before the agent starts;
- tasks without a `scorer.py`, which Control Tower refuses to run ungraded.

`--exclude-used` drops tasks already run in earlier requests, so batches
do not repeat a task by accident.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random

SETTINGS = pathlib.Path("settings/linuxarena")

# Tasks whose setup script fails on the pinned environment revision.
BROKEN = {
    # 2026-11-06: setup.sh patch hunk fails (app/policies/channel_policy.rb).
    ("slack_clone", "channel_policy_member_check_wrong_table_join"),
    # 2026-11-09: setup.sh patch hunk fails (app/controllers/api/messages_controller.rb).
    ("slack_clone", "es_index_wrong_date_format"),
}


def excluded(env: str, task: str) -> bool:
    return "_rewrite_in_" in task or (env, task) in BROKEN


def used_tasks(paths: list[str]) -> set[tuple[str, str]]:
    used = set()
    for p in paths:
        for cond in json.loads(pathlib.Path(p).read_text())["conditions"].values():
            args = cond.get("args", {})
            used.add((args.get("env", "").split("/")[-1], args.get("main_task", "")))
    return used


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--splits", required=True)
    ap.add_argument("--split", choices=("dev", "test"), default="dev")
    ap.add_argument("--per-env", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--exclude-used", nargs="*", default=[])
    args = ap.parse_args()

    envs = json.loads(pathlib.Path(args.splits).read_text())[args.split]
    used = used_tasks(args.exclude_used)
    rng = random.Random(args.seed)
    picks = []
    for env in envs:
        tasks = sorted(
            p.name for p in (SETTINGS / env / "main_tasks").iterdir()
            if p.is_dir() and not p.name.startswith(("_", "."))
            and not excluded(env, p.name) and (env, p.name) not in used
            and (p / "scorer.py").exists()
        )
        if len(tasks) < args.per_env:
            raise SystemExit(f"{env}: only {len(tasks)} eligible tasks, {args.per_env} requested")
        picks += [{"env": f"linuxarena/{env}", "main_task": t} for t in rng.sample(tasks, args.per_env)]
    print(json.dumps(picks, indent=2))


if __name__ == "__main__":
    main()
