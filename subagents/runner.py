"""Shared Agent SDK runner for all sub-agents."""

import fnmatch
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    HookMatcher,
    ProcessError,
    ResultMessage,
    ThinkingConfig,
    query,
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The scaffold checkout itself, resolved through symlinks. In an installed
# target every scaffold file is a gitignored symlink: `abspath` gives the
# target-side path (which Glob/search skip), while `realpath` follows the link
# back to the checkout, where the docs always exist. Used to hand each
# sub-agent the absolute paths of the documents it must read first, so a
# `--cwd` elsewhere can never leave it unable to find its own contract (a
# Critic failed as a gate this way twice in one run).
SCAFFOLD_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def scaffold_doc(relpath: str) -> str:
    """Absolute path of a scaffold document, resolved from the checkout.

    Parameters
    ----------
    relpath : str
        Path relative to the scaffold root, e.g.
        ``".claude/docs/contracts/mapper_interface_contract.md"``.

    Returns
    -------
    str
        The absolute path inside the scaffold checkout.
    """
    return os.path.join(SCAFFOLD_ROOT, relpath)


def contract_section(*relpaths: str) -> str:
    """Render the prompt section naming the documents to read first.

    Parameters
    ----------
    *relpaths : str
        Scaffold-relative paths, in reading order (contract first).

    Returns
    -------
    str
        A ``## Contract`` markdown section listing each absolute path.

    Raises
    ------
    RuntimeError
        If any document is missing — a sub-agent without its contract fails as
        a gate, so this surfaces as an agent error (exit 2) before any spend.
    """
    paths = [scaffold_doc(p) for p in relpaths]
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise RuntimeError(f"scaffold document(s) not found: {', '.join(missing)}")
    listing = "\n".join(f"- `{p}`" for p in paths)
    return (
        "## Contract\n\n"
        "Read these with a direct Read of each exact absolute path before doing "
        "anything else. Do not search for them and do not substitute a relative "
        "path; each file is where its path says it is.\n\n" + listing
    )

# Model every sub-agent runs on unless the invocation overrides it via the
# `subagent_model` input field. Kept as a single default so the override is the
# only thing that ever varies (e.g. a less refusal-prone model on a
# dangerous-capability eval, or a cheaper/weaker model on the user's request).
# Defaults to the most capable Opus tier: per-token cost is equal across Opus
# models, so there is no reason to run the sub-agents on a weaker one — the extra
# capability sharpens the Explorer's site reasoning and the Analyst's scanner /
# stats work, which is where investigation quality is won. (A Sonnet-class model
# is the natural cost/speed downgrade via `subagent_model` if ever wanted.)
DEFAULT_MODEL = "claude-opus-4-8"

# Extended-thinking config every sub-agent runs with unless the invocation
# overrides it via the `subagent_thinking` input field. Adaptive thinking lets
# the model scale reasoning depth to the task on its own — so the Explorer's
# site reasoning and the Analyst's scanner/stats work draw deeply, while the
# Executor's mechanical "run these conditions" work naturally draws little.
# Adaptive is supported on Opus 4.6+ (so it holds for the 4.8 default and any
# reasonable `subagent_model` override) and is in fact the CLI default there; we
# set it explicitly so the behaviour is pinned in the repo rather than left to
# the CLI/model version, and so it can be overridden per invocation (e.g.
# {"type": "disabled"} or {"type": "enabled", "budget_tokens": N}) the same way
# `subagent_model` is — e.g. when overriding to a model without adaptive support.
DEFAULT_THINKING: ThinkingConfig = {"type": "adaptive"}

# Turn cap every sub-agent runs under unless the invocation overrides it via
# the `subagent_max_turns` input field. 100 is the SDK cap the six-runs
# analysis saw Analyst invocations die on (D14); the policy for sizing work
# orders so the cap is not hit lives in the Analyst docs — this is only the
# mechanism for changing it per invocation.
DEFAULT_MAX_TURNS = 100

# Where the cost sidecar for the current invocation goes. Set by
# `scripts/launch_subagent.py` for every launch; when unset the runner only
# logs the usage line to stderr.
COST_FILE_ENV = "PRISM_COST_FILE"

# OpenRouter exposes an Anthropic-Messages-compatible endpoint, so the Claude
# Code CLI the Agent SDK spawns can be pointed at it without a local proxy. This
# is what allows a sub-agent to run on a non-Claude model — e.g. running the
# Analyst on a different model family as a robustness check on whether the
# sub-agent model really is "infrastructure, not experiment".
OPENROUTER_ANTHROPIC_BASE_URL = "https://openrouter.ai/api"


def _resolve_route_env(route: str | None, model: str) -> dict[str, str]:
    """Build the environment overrides that select a sub-agent's provider.

    These are set per invocation via `ClaudeAgentOptions.env` rather than in the
    process environment, so one sub-agent can run through OpenRouter while
    another runs directly against Anthropic in the same investigation.

    Parameters
    ----------
    route : str | None
        Either "openrouter" or None/"direct" (the default, unchanged behaviour).
    model : str
        The model string, used only for the error message.

    Returns
    -------
    dict[str, str]
        Environment variables to pass to the Claude Code subprocess. Empty for
        the direct route.

    Raises
    ------
    RuntimeError
        If the OpenRouter route is requested without `OPENROUTER_API_KEY` set.
    """
    if route in (None, "direct"):
        return {}
    if route != "openrouter":
        raise RuntimeError(f"Unknown subagent_route: {route!r} (expected 'direct' or 'openrouter')")

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"subagent_route='openrouter' requested for model {model!r} but "
            "OPENROUTER_API_KEY is not set. Add it to the project's .env file."
        )

    # ANTHROPIC_API_KEY must be explicitly blank: if both it and the auth token
    # are set, the CLI prefers the API key and would authenticate against the
    # wrong service.
    return {
        "ANTHROPIC_BASE_URL": OPENROUTER_ANTHROPIC_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_API_KEY": "",
    }


def _resolve_thinking(
    thinking: ThinkingConfig | None, model: str, route: str | None
) -> ThinkingConfig:
    """Choose the extended-thinking config, guarding non-Claude models.

    Adaptive thinking is a Claude feature. Sending it to a non-Claude model
    routed through OpenRouter's Anthropic-compatible endpoint is at best
    ignored and at worst a request error, so the default is downgraded to
    disabled for those models. An explicit `subagent_thinking` always wins —
    the guard only replaces the *default*.

    Parameters
    ----------
    thinking : ThinkingConfig | None
        The per-invocation override, if any.
    model : str
        The resolved model string for this sub-agent.
    route : str | None
        The resolved route.

    Returns
    -------
    ThinkingConfig
        The thinking config to run with.
    """
    if thinking is not None:
        return thinking
    if route == "openrouter" and "claude" not in model.lower():
        return {"type": "disabled"}
    return DEFAULT_THINKING

_MEMORY_INSTRUCTIONS = """\
## Agent Memory

You have a persistent memory file at `{memory_path}`. Its contents are \
included below (if any). You may append observations, lessons learned, \
or notes for future invocations by writing to this file. **Append only** — \
you must preserve all existing content and add new entries at the end. \
The file will be blocked if you attempt to remove or rewrite existing content.

{memory_content}
---

"""


_FOREGROUND_INSTRUCTIONS = """

## Foreground execution only

Run every command in the foreground and wait for it to finish before \
continuing. Never background a task — do not set `run_in_background: true`, \
and do not use a trailing `&` or `nohup`. Long-running commands (evals, \
scripts) are expected; run them in the foreground and wait. Backgrounding \
breaks the scaffold's assumption that a step has completed, and its outputs \
exist, before the next step begins.
"""


def _build_hooks(
    agent_name: str,
    restricted_files: dict[str, str],
    memory_abs_path: str | None,
    restrict_writes_to_memory: bool,
) -> dict[str, Any] | None:
    """Build PreToolUse hooks for file access control and memory.

    Args:
        agent_name: Human-readable agent name for error messages.
        restricted_files: Mapping of path patterns to denial reasons. A
            pattern ending in "/" denies an entire directory (matched
            anywhere in the path); a pattern containing a shell wildcard
            (`*`, `?`, `[`) is matched against the file's basename
            (e.g. "CONDITION_LABEL_MAP*.json" covers every iteration's map);
            any other pattern denies paths ending with it, which is
            normally a bare filename. Directory patterns
            exist so that a whole class of documents can be blinded once
            rather than enumerated — a file added to a denied directory
            later is blocked automatically, whereas a new filename is
            invisible to a list of filenames until someone remembers it.
        memory_abs_path: Absolute path to the agent's memory.md, or None.
        restrict_writes_to_memory: If True, block all writes except to
            memory.md. If False, only enforce append-only on memory.md.

    Returns:
        A hooks dict suitable for ClaudeAgentOptions, or None if no
        hooks are needed.
    """
    read_hooks: list[Any] = []
    write_hooks: list[Any] = []

    # --- Read hook: allow memory.md, then check restricted files ---
    if restricted_files or memory_abs_path:

        async def check_read_access(
            input_data: dict, tool_use_id: str, context: dict
        ) -> dict:
            file_path = input_data.get("tool_input", {}).get("file_path", "")
            # Always allow reading the agent's own memory file.
            if memory_abs_path and os.path.abspath(file_path) == memory_abs_path:
                return {}
            for pattern, reason in restricted_files.items():
                # A trailing slash marks a directory pattern: match it
                # anywhere in the path, since a file path ends in the
                # filename and would never end with the directory itself.
                if pattern.endswith("/"):
                    matched = pattern in file_path
                elif any(ch in pattern for ch in "*?["):
                    # A family of files sharing a name shape (one per
                    # iteration, say) is denied by one pattern rather than
                    # an enumeration that silently stops at its last entry.
                    matched = fnmatch.fnmatchcase(os.path.basename(file_path), pattern)
                else:
                    matched = file_path.endswith(pattern)
                if matched:
                    return {
                        "decision": "block",
                        "reason": (
                            f"[{agent_name}] Access denied: {pattern} — {reason}"
                        ),
                    }
            return {}

        read_hooks.append(check_read_access)

    # --- Write hook: append-only memory, optional general write restriction ---
    if memory_abs_path or restrict_writes_to_memory:

        async def check_write_access(
            input_data: dict, tool_use_id: str, context: dict
        ) -> dict:
            tool_input = input_data.get("tool_input", {})
            file_path = tool_input.get("file_path", "")
            is_memory = memory_abs_path and (
                os.path.abspath(file_path) == memory_abs_path
            )

            if is_memory:
                # Enforce append-only: new content must start with existing.
                new_content = tool_input.get("content", "")
                try:
                    with open(memory_abs_path) as f:
                        existing = f.read()
                except FileNotFoundError:
                    existing = ""
                if existing and not new_content.startswith(existing):
                    return {
                        "decision": "block",
                        "reason": (
                            f"[{agent_name}] memory.md is append-only. "
                            "You must preserve all existing content and "
                            "add new entries at the end."
                        ),
                    }
                return {}

            if restrict_writes_to_memory:
                return {
                    "decision": "block",
                    "reason": (
                        f"[{agent_name}] Write access is restricted to memory.md only."
                    ),
                }
            return {}

        write_hooks.append(check_write_access)

    # --- Bash hook: forbid backgrounding so each step runs to completion ---
    # Applies to every sub-agent, and is scoped to this Agent SDK query only, so
    # it never affects the orchestrator or any other Claude Code session.
    async def check_no_background(
        input_data: dict, tool_use_id: str, context: dict
    ) -> dict:
        if input_data.get("tool_input", {}).get("run_in_background"):
            return {
                "decision": "block",
                "reason": (
                    f"[{agent_name}] Backgrounding is disabled for sub-agents. "
                    "Run the command in the foreground and wait for it to "
                    "finish; long-running commands are expected."
                ),
            }
        return {}

    bash_hooks = [check_no_background]

    matchers = []
    if read_hooks:
        matchers.append(HookMatcher(matcher="Read", hooks=read_hooks))
    if write_hooks:
        matchers.append(HookMatcher(matcher="Write", hooks=write_hooks))
    if bash_hooks:
        matchers.append(HookMatcher(matcher="Bash", hooks=bash_hooks))
    return {"PreToolUse": matchers}


async def run_agent(
    prompt: str,
    system_prompt: str,
    allowed_tools: list[str],
    disallowed_tools: list[str],
    agent_name: str,
    cwd: str | None = None,
    restricted_files: dict[str, str] | None = None,
    memory_file: str | None = None,
    restrict_writes_to_memory: bool = True,
    model: str | None = None,
    thinking: ThinkingConfig | None = None,
    route: str | None = None,
    max_turns: int | None = None,
) -> str:
    """Run an Agent SDK agent and return its output.

    Args:
        prompt: The user prompt to send to the agent.
        system_prompt: The system prompt defining agent behaviour.
        allowed_tools: Tools the agent may use.
        disallowed_tools: Tools explicitly denied (defense-in-depth).
        agent_name: Human-readable name for error messages.
        cwd: Working directory for the agent. Defaults to project root.
        restricted_files: Optional mapping of filename patterns to denial
            reasons. Reads of matching files are blocked with the reason
            shown to the agent.
        memory_file: Optional path to the agent's memory.md, relative to
            the project root. Enables persistent memory (append-only writes,
            content injected into prompt).
        restrict_writes_to_memory: If True (default), block all writes
            except to memory.md. Set to False for agents that need general
            write access (e.g., the transcript analyst writing scripts).
        model: The Claude model this sub-agent runs on. If None, falls back
            to DEFAULT_MODEL. Set per-invocation (via the `subagent_model`
            input field) to override the default — e.g. a less refusal-prone
            model on a dangerous-capability eval, or a cheaper model on
            request.
        thinking: Extended-thinking configuration for this sub-agent. If None,
            falls back to DEFAULT_THINKING (adaptive). Set per-invocation (via
            the `subagent_thinking` input field) to override — e.g.
            {"type": "disabled"} to switch thinking off, or
            {"type": "enabled", "budget_tokens": N} for a fixed budget on a
            model that doesn't support adaptive thinking.
        route: How this sub-agent reaches its model — "direct" (default,
            Anthropic) or "openrouter", which points the spawned CLI at
            OpenRouter's Anthropic-compatible endpoint so `model` may name any
            model family. Set per-invocation via the `subagent_route` input
            field. This is an infrastructure choice, independent of the eval's
            target models. No role is restricted to Claude: the interface
            contracts aim to specify each sub-agent completely enough that any
            capable model can fulfil it, so a contract violation under a
            non-Claude model is evidence the guidance is underspecified rather
            than a reason to restrict the model.
        max_turns: Turn cap for this invocation. If None, falls back to
            DEFAULT_MAX_TURNS. Set per-invocation via the `subagent_max_turns`
            input field when a work order is known to need more (or fewer)
            turns than the default.

    Returns:
        The agent's output as a string.

    Raises:
        RuntimeError: If the agent produces no output or the SDK fails.
    """
    working_dir = cwd or _PROJECT_ROOT

    # Append the shared foreground-execution rule to every sub-agent's system
    # prompt (mirrors the _MEMORY_INSTRUCTIONS pattern): sub-agents must run
    # tasks in the foreground so each step completes before the next begins.
    system_prompt = system_prompt + _FOREGROUND_INSTRUCTIONS

    # --- Memory injection ---
    memory_abs_path: str | None = None
    if memory_file:
        memory_abs_path = os.path.join(_PROJECT_ROOT, memory_file)
        try:
            with open(memory_abs_path) as f:
                memory_content = f.read().strip()
        except FileNotFoundError:
            memory_content = "(empty — no notes from previous runs)"
        prompt = (
            _MEMORY_INSTRUCTIONS.format(
                memory_path=memory_file,
                memory_content=memory_content,
            )
            + prompt
        )

    # --- Hooks ---
    hooks = _build_hooks(
        agent_name=agent_name,
        restricted_files=restricted_files or {},
        memory_abs_path=memory_abs_path,
        restrict_writes_to_memory=restrict_writes_to_memory,
    )

    # Ensure Write is in allowed_tools if memory is enabled.
    if memory_file and "Write" not in allowed_tools:
        allowed_tools = [*allowed_tools, "Write"]

    # --- Model, route, and thinking resolution ---
    resolved_model = model or DEFAULT_MODEL
    route_env = _resolve_route_env(route, resolved_model)

    options = ClaudeAgentOptions(
        model=resolved_model,
        thinking=_resolve_thinking(thinking, resolved_model, route),
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode="bypassPermissions",
        max_turns=max_turns or DEFAULT_MAX_TURNS,
        cwd=working_dir,
        **({"env": route_env} if route_env else {}),
        **({"hooks": hooks} if hooks else {}),
    )

    try:
        result: str | None = None
        result_message: ResultMessage | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message.result
                result_message = message
    except CLINotFoundError:
        raise RuntimeError(
            f"{agent_name}: Claude Code CLI not found. "
            "Install with: pip install claude-agent-sdk"
        )
    except CLIConnectionError as exc:
        raise RuntimeError(f"{agent_name}: connection error: {exc}")
    except ProcessError as exc:
        raise RuntimeError(f"{agent_name}: process error: {exc}")

    if result_message is not None:
        _write_cost_sidecar(agent_name, resolved_model, route, result_message)

    if not result:
        # Say *why* there is no output: a turn-cap exit (`error_max_turns`) and
        # an API refusal need different recoveries, and the subtype is the only
        # place the SDK tells them apart.
        detail = ""
        if result_message is not None:
            detail = (
                f" (subtype={result_message.subtype}, stop_reason={result_message.stop_reason}, "
                f"num_turns={result_message.num_turns}, max_turns={options.max_turns})"
            )
        raise RuntimeError(f"{agent_name} agent produced no output{detail}.")

    return result


def _write_cost_sidecar(
    agent_name: str, model: str, route: str | None, message: ResultMessage
) -> None:
    """Record the invocation's SDK usage and cost.

    Written to the path in `PRISM_COST_FILE` when set (the launcher sets it per
    invocation); a one-line summary always goes to stderr so a launch without
    the sidecar still leaves the figure in the stderr log. `scripts/spend.py`
    sums the sidecars.

    Parameters
    ----------
    agent_name : str
        Human-readable role name.
    model : str
        The resolved sub-agent model.
    route : str | None
        The resolved route.
    message : ResultMessage
        The SDK's final message for the query.
    """
    record = {
        "prism_cost": 1,
        "agent": agent_name,
        "model": model,
        "route": route or "direct",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "subtype": message.subtype,
        "stop_reason": message.stop_reason,
        "is_error": message.is_error,
        "num_turns": message.num_turns,
        "duration_ms": message.duration_ms,
        "duration_api_ms": message.duration_api_ms,
        "total_cost_usd": message.total_cost_usd,
        "usage": message.usage,
        "model_usage": message.model_usage,
        "permission_denials": message.permission_denials,
    }
    print(
        f"[cost] agent={agent_name!r} model={model} total_cost_usd={message.total_cost_usd} "
        f"turns={message.num_turns} subtype={message.subtype}",
        file=sys.stderr,
    )
    path = os.environ.get(COST_FILE_ENV)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(record, f, indent=2, default=str)
    except OSError as exc:  # never fail a completed invocation over telemetry
        print(f"[cost] could not write {path}: {exc}", file=sys.stderr)
