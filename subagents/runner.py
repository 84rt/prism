"""Shared Agent SDK runner for all sub-agents."""

import functools
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from types import ModuleType
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


# Heading of the one-re-ask section; tests and the docs refer to it verbatim.
REASK_HEADING = "## Re-ask: Conformance Defects In Your Previous Report"


def reask_section(
    agent_name: str,
    conformance_defects: Any,
    previous_report: Any,
    rebuild_note: str,
) -> str | None:
    """Render the one-re-ask section, or None on a first attempt.

    A lead agent that finds a sub-agent report non-conformant
    (`scripts/check_report.py` exits non-zero) re-invokes the same request
    once, adding `conformance_defects` (the printed defect lines) and
    `previous_report` (the failed report's absolute path). The agent is asked
    to *restructure* that report into the contract's headings rather than
    redo the work: the content is usually all there under the wrong headings,
    and redoing it would cost a full run and could change the findings.

    The two fields travel together. A re-ask with one but not the other, a
    non-list or empty defect list, or a report path that is relative or
    missing is malformed and raises, so the CLI exits 2 instead of the
    sub-agent silently running a first attempt again (the `**data` hazard:
    a key no builder reads is dropped without a trace).

    Parameters
    ----------
    agent_name : str
        Used in the error message, e.g. "Environment explorer".
    conformance_defects : Any
        `data.get("conformance_defects")` — expected list[str] or None.
    previous_report : Any
        `data.get("previous_report")` — expected absolute path or None.
    rebuild_note : str
        The role's rule for re-deriving a genuinely missing section, e.g. the
        Analyst's "from your saved artefacts; never re-run a scan".

    Returns
    -------
    str | None
        A markdown section starting with `REASK_HEADING`, or None when
        neither field was sent.

    Raises
    ------
    RuntimeError
        On a malformed re-ask (see above).
    """
    if conformance_defects is None and previous_report is None:
        return None

    # --- Validate: both fields, the right shapes, a readable report --------
    problems: list[str] = []
    if not (
        isinstance(conformance_defects, list)
        and conformance_defects
        and all(isinstance(d, str) and d.strip() for d in conformance_defects)
    ):
        problems.append("conformance_defects must be a non-empty list of non-empty strings")
    if not isinstance(previous_report, str) or not os.path.isabs(previous_report):
        problems.append("previous_report must be an absolute path")
    elif not os.path.isfile(previous_report):
        problems.append(f"previous_report not found: {previous_report}")
    if problems:
        raise RuntimeError(f"{agent_name}: malformed re-ask — {'; '.join(problems)}")

    # --- Render ---------------------------------------------------------------
    defects = "\n".join(f"- {d}" for d in conformance_defects)
    return (
        f"{REASK_HEADING}\n\n"
        "This is the one re-ask for this request. Your previous report failed the "
        "mechanical conformance check (`scripts/check_report.py`) with these defects:\n\n"
        f"{defects}\n\n"
        f"Read your previous report first: `{previous_report}`\n\n"
        "Then emit the complete report again, restructured into the contract's exact "
        "section headings — verbatim, at level 3, in order, once each, nothing before "
        "the first. Carry the previous report's content across under the right "
        "headings without re-doing the work behind it; re-derive content only for a "
        f"section (or item) the defects show was genuinely missing. {rebuild_note} "
        "Emit the whole report, not a patch, a diff, or an apology."
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

# The Claude Code CLI only knows Claude models' context windows. For any other
# model it prints `[claude-code:unrecognized_model]` (a warning, not a
# rejection) and assumes a Claude-sized window, so a 1M-window model is
# silently compacted at ~200k tokens. These two variables switch that
# assumption off and state the real window; R01 diagnosed both by hand (D27).
WINDOW_ENFORCEMENT_ENV = "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"
MAX_CONTEXT_ENV = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"

# One public, key-free catalogue request per model per process; short, because
# it sits in front of every non-Claude invocation and must never stall one.
CONTEXT_FETCH_TIMEOUT = 10.0


@functools.lru_cache(maxsize=1)
def _load_openrouter_endpoints() -> ModuleType:
    """Import `scripts/openrouter_endpoints.py` (stdlib-only) by path.

    Loaded from the scaffold checkout rather than imported as a package:
    `scripts/` is not a package, and the checkout always holds the file even
    when a target's symlinks are incomplete.

    Returns
    -------
    ModuleType
        The loaded module (cached, so tests can patch its `_get_json`).
    """
    path = os.path.join(SCAFFOLD_ROOT, "scripts", "openrouter_endpoints.py")
    spec = importlib.util.spec_from_file_location("prism_openrouter_endpoints", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def _openrouter_context_length(model: str) -> int | None:
    """The model's context window from OpenRouter's public catalogue, or None.

    Offline-safe by design: any failure (no network, unlisted model, malformed
    payload) returns None after one stderr line, and the caller then leaves
    the CLI's default window enforcement in place. A sub-agent invocation is
    never failed over this. Cached per process, so the line appears once.

    Parameters
    ----------
    model : str
        The sub-agent model string, e.g. ``"google/gemini-3-pro"``.

    Returns
    -------
    int | None
        The window in tokens, or None when it could not be resolved.
    """
    try:
        window = _load_openrouter_endpoints().model_context_length(
            model, timeout=CONTEXT_FETCH_TIMEOUT
        )
    except Exception as exc:  # deliberately broad: telemetry-grade lookup
        print(
            f"[route] context window for {model!r} not resolved from the OpenRouter "
            f"catalogue ({exc}); leaving the CLI's default window enforcement in place",
            file=sys.stderr,
        )
        return None
    if window is None:
        print(
            f"[route] OpenRouter catalogue lists no context length for {model!r}; "
            "leaving the CLI's default window enforcement in place",
            file=sys.stderr,
        )
    return window


def _context_window_env(model: str) -> dict[str, str]:
    """Window-enforcement overrides for a non-Claude model on OpenRouter.

    Parameters
    ----------
    model : str
        The sub-agent model string.

    Returns
    -------
    dict[str, str]
        Both variables when the window resolved, otherwise empty (CLI
        default). A value the user already exported wins over the catalogue.

    Examples
    --------
    ``{"CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1048576"}``
    """
    if "claude" in model.lower():
        return {}  # the CLI knows Claude windows; nothing to override
    if os.environ.get(MAX_CONTEXT_ENV):
        return {WINDOW_ENFORCEMENT_ENV: "1", MAX_CONTEXT_ENV: os.environ[MAX_CONTEXT_ENV]}
    window = _openrouter_context_length(model)
    if window is None:
        return {}
    return {WINDOW_ENFORCEMENT_ENV: "1", MAX_CONTEXT_ENV: str(window)}


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
        the direct route. For a non-Claude model on OpenRouter it also carries
        the context-window overrides from `_context_window_env` when the
        catalogue lookup succeeds.

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
        **_context_window_env(model),
    }


# --- Credential exhaustion ----------------------------------------------------
#
# A provider refusing on account credit or a usage cap is its own failure
# class: it is not transient (retrying fails identically until credit is
# restored), not a refusal, and not a scaffold bug — and when it takes both
# routes it is the trigger for the lead docs' Suspension procedure. Provider
# caps are not observable in advance, so this is detected reactively, from
# the error text. Every pattern is the provider's own wording, lower-cased;
# the archive provenance is noted per entry. Kept identical to
# `scripts/execute_evals.py`'s `_CREDENTIAL_PATTERNS` (a test pins the two).

FAILURE_CREDENTIALS_EXHAUSTED = "credentials_exhausted"

CREDENTIAL_PATTERNS: tuple[str, ...] = (
    # Anthropic workspace spend cap. Archive: R04/R05 stderr verbatim,
    # "API Error: 400 You have reached your specified workspace API usage
    # limits. You will regain access on 2026-10-01 at 00:00 UTC." (also R06,
    # R03, R01 logs).
    "reached your specified workspace api usage limits",
    # OpenRouter workspace budget. Archive: R02 log, "Failed to authenticate.
    # API Error: 403 Workspace lifetime budget of $150.00 exceeded." (also R01,
    # R04 logs). Note the "Failed to authenticate" prefix: it is not a bad key.
    "workspace lifetime budget",
    # Anthropic prepaid credit and OpenRouter 402 wording — the providers'
    # documented messages, not observed in the archive.
    "credit balance is too low",
    "requires more credits",
    "insufficient credits",
)

# A bare "402" also matches sample ids and hashes, so the status code counts
# only where an error line states it as one.
CREDENTIAL_STATUS_RE = re.compile(r"(?:error code|api error|http|status(?:[ _]code)?)[:=\s]*402\b", re.I)


def classify_failure(text: str | None) -> str | None:
    """Name the failure class of an SDK/CLI error, where one is recognised.

    Parameters
    ----------
    text : str | None
        Exception text or an error result's text.

    Returns
    -------
    str | None
        ``"credentials_exhausted"`` for a credit/usage-cap refusal, else None.

    Examples
    --------
    >>> classify_failure("API Error: 400 You have reached your specified workspace API usage limits.")
    'credentials_exhausted'
    """
    if not text:
        return None
    low = text.lower()
    if any(p in low for p in CREDENTIAL_PATTERNS) or CREDENTIAL_STATUS_RE.search(text):
        return FAILURE_CREDENTIALS_EXHAUSTED
    return None


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
            anywhere in the path); any other pattern denies paths ending
            with it, which is normally a bare filename. Directory patterns
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

    result: str | None = None
    result_message: ResultMessage | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message.result
                result_message = message
    except CLINotFoundError:
        raise RuntimeError(
            f"{agent_name}: Claude Code CLI not found. "
            "Install with: pip install claude-agent-sdk"
        )
    except (CLIConnectionError, ProcessError) as exc:
        kind = "connection error" if isinstance(exc, CLIConnectionError) else "process error"
        # The CLI emits its error result before exiting non-zero, so the
        # usage of a failed run is usually still in hand: record it, tagged.
        # Older SDKs raise a generic "Command failed with exit code 1"
        # ProcessError and leave the provider's text only in the error
        # result, so classify both.
        error_result = result_message.result if result_message is not None else None
        _raise_if_credentials_exhausted(
            agent_name, resolved_model, route,
            "\n".join(t for t in (f"{exc}", error_result) if t), result_message,
        )
        if result_message is not None:
            _write_cost_sidecar(agent_name, resolved_model, route, result_message)
        raise RuntimeError(f"{agent_name}: {kind}: {exc}")

    # An API failure can also arrive as an error result with the provider's
    # text in `result` and no exception; that text is not a report.
    if result_message is not None and result_message.is_error:
        _raise_if_credentials_exhausted(
            agent_name, resolved_model, route, result_message.result, result_message
        )

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


def _raise_if_credentials_exhausted(
    agent_name: str,
    model: str,
    route: str | None,
    text: str | None,
    message: ResultMessage | None,
) -> None:
    """Raise the distinct credential-exhaustion error if `text` is one.

    The message carries the marker ``credentials_exhausted (route=<route>)``,
    which `scripts/launch_subagent.py` copies into the receipt; the cost
    sidecar records the same class and route, written even when the SDK
    yielded no result message, so the lead agent can tell "this route is out
    of credit" from a bug without reading prose.

    Parameters
    ----------
    agent_name : str
        Human-readable role name.
    model : str
        The resolved sub-agent model.
    route : str | None
        The resolved route.
    text : str | None
        The exception or error-result text to classify.
    message : ResultMessage | None
        The SDK's final message, if one arrived.

    Raises
    ------
    RuntimeError
        When `text` is a credit/usage-cap refusal; otherwise returns None.
    """
    if classify_failure(text) != FAILURE_CREDENTIALS_EXHAUSTED:
        return
    route_name = route or "direct"
    _write_cost_sidecar(
        agent_name, model, route, message, failure_class=FAILURE_CREDENTIALS_EXHAUSTED
    )
    raise RuntimeError(
        f"{agent_name}: {FAILURE_CREDENTIALS_EXHAUSTED} (route={route_name}, model={model}): "
        f"the provider refused on account credit or a usage cap — not transient and "
        f"not a scaffold bug; see the Suspension procedure. Provider text: {text}"
    )


def _write_cost_sidecar(
    agent_name: str,
    model: str,
    route: str | None,
    message: ResultMessage | None,
    failure_class: str | None = None,
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
    message : ResultMessage | None
        The SDK's final message for the query; None when a failure arrived
        before one (usage fields are then recorded as null).
    failure_class : str | None
        A recognised failure class (``"credentials_exhausted"``), else None.
    """
    def field(name: str) -> Any:
        return getattr(message, name, None) if message is not None else None

    record = {
        "prism_cost": 1,
        "agent": agent_name,
        "model": model,
        "route": route or "direct",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "failure_class": failure_class,
        "subtype": field("subtype"),
        "stop_reason": field("stop_reason"),
        "is_error": True if failure_class else field("is_error"),
        "num_turns": field("num_turns"),
        "duration_ms": field("duration_ms"),
        "duration_api_ms": field("duration_api_ms"),
        "total_cost_usd": field("total_cost_usd"),
        "usage": field("usage"),
        "model_usage": field("model_usage"),
        "permission_denials": field("permission_denials"),
    }
    print(
        f"[cost] agent={agent_name!r} model={model} total_cost_usd={field('total_cost_usd')} "
        f"turns={field('num_turns')} subtype={field('subtype')}"
        + (f" failure_class={failure_class} route={route or 'direct'}" if failure_class else ""),
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
