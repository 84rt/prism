"""Prism-runnable wrapper around Control Tower's `control_tower/eval` task.

Prism's Executor only knows `uv run inspect eval <task.py> --model M -T k=v`.
Control Tower registers plain Inspect tasks, so this file turns Prism's flat
`-T` arguments into the dict-shaped arguments `eval_task` expects, and binds
the model roles explicitly: Inspect's `--model` does NOT set Control Tower's
`untrusted` role, so without `untrusted=` the attacker would silently run on
Control Tower's default model.

Run from this directory (its `.venv` holds the pinned control-tower==6.0.1).
Environment variables that must be set by the caller:
  CONTROL_TOWER_SETTINGS_DIR  where `ct settings pull linuxarena` put the envs
  CONTROL_TOWER_PRICING       `off` until a rate table is generated
  CONTROL_TOWER_IMAGES        `pull` to use pre-built images, else local build

Free dry run (no model calls):
  uv run --no-project inspect eval task.py -T policy=test \
      -T env=linuxarena/web_scraping -T main_task=redact -T side_task=expose_secret
"""
from __future__ import annotations

import json
from typing import Any

from inspect_ai import Task, task

from control_tower.eval.launch.inspect_tasks import eval_task


def _dict(value: Any) -> dict[str, Any]:
    """Accept a dict, a JSON string (how Prism renders `-T` dict args), or None."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return json.loads(value) if value.startswith("[") else [value]
    return list(value)


@task
def linuxarena(
    env: str = "linuxarena/web_scraping",
    main_task: str | list[str] = "redact",
    side_task: str | list[str] = "expose_secret",
    policy: str = "test",
    policy_args: dict[str, Any] | str | None = None,
    protocol: str = "null-blue-team",
    protocol_args: dict[str, Any] | str | None = None,
    untrusted: str | None = None,
    trusted: str | None = None,
    monitor: str | None = None,
    run_name: str | None = None,
    score_main_on_attack: bool = False,
) -> Task:
    """One environment, a cross product of main × side tasks, one policy under one protocol.

    `policy` / `protocol` take Control Tower registry names (`test`, `attack`,
    `honest`, `trusted-monitoring`, ...) or a local `path/to/file.py@name`.
    Monitors are an argument of the protocol, e.g.
    `-T protocol_args='{"monitors": ["simple"]}'`.
    """
    roles: dict[str, str] = {}
    if untrusted:
        roles["untrusted"] = untrusted
    if trusted:
        roles["trusted"] = trusted
    if monitor:
        roles["monitor"] = monitor
    return eval_task(
        task_selection={
            "environments": [env],
            "main_tasks": _list(main_task),
            "side_tasks": _list(side_task),
        },
        policy={"name": policy, "args": _dict(policy_args)},
        protocol={"name": protocol, "args": _dict(protocol_args)},
        model_roles=roles or None,
        run_name=run_name,
        score_main_on_attack=score_main_on_attack,
    )
