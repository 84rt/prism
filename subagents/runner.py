"""Shared Agent SDK runner for all sub-agents."""

import fnmatch
import functools
import glob
import importlib.util
import itertools
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from types import ModuleType
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
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

# --- Isolation from the host's Claude Code configuration ---------------------
#
# `run_agent` passes `setting_sources=[]`, so no user/project/local settings
# file is read: no CLAUDE.md, no hooks, plugins or permission rules from the
# repo a sub-agent happens to run in. These variables close the two context
# channels that do not go through a settings source: managed CLAUDE.md files
# and the auto-memory directory (both checked against the bundled CLI of the
# pinned `claude-agent-sdk`, see `scripts/launch_subagent.py`).
ISOLATION_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}

# --- No auto-backgrounding inside a sub-agent ---------------------------------
#
# The CLI's default Bash timeout is 120 s, and a command that outruns it can be
# moved to the background rather than killed. In one smoke run an Analyst scan
# did exactly that: the command left the agent's view and slipped past the
# no-background guard in `_build_hooks`, which only sees the call as issued.
# So every sub-agent runs with a 10-minute default (long enough for a typical
# scan) and a 60-minute ceiling an agent may request explicitly — the
# Executor's 3300 s `execute_evals.py wait` and a long scan — and with
# background tasks disabled so a timeout ends the command instead of
# detaching it. All three names were checked against the bundled CLI of the
# pinned `claude-agent-sdk` (0.2.165, CLI 2.1.294); a user's own exported
# value wins, as with the context-window override — so a user who exports a
# ceiling below the Executor's 3300 s wait reintroduces the problem.
BASH_DEFAULT_TIMEOUT_MS = 10 * 60 * 1000  # 600000
BASH_MAX_TIMEOUT_MS = 60 * 60 * 1000  # 3600000


def _bash_timeout_env() -> dict[str, str]:
    """Bash timeout and background-task settings for every sub-agent.

    Returns
    -------
    dict[str, str]
        ``{"BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS":
        "3600000", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}``, with any
        timeout the user exported taking precedence.
    """
    return {
        "BASH_DEFAULT_TIMEOUT_MS": os.environ.get("BASH_DEFAULT_TIMEOUT_MS") or str(BASH_DEFAULT_TIMEOUT_MS),
        "BASH_MAX_TIMEOUT_MS": os.environ.get("BASH_MAX_TIMEOUT_MS") or str(BASH_MAX_TIMEOUT_MS),
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    }

# The one project setting sub-agents relied on, passed explicitly now that
# project settings are not loaded: secrets in `.env` files are never read.
SUBAGENT_SETTINGS: dict[str, Any] = {
    "permissions": {"deny": ["Read(**/.env)", "Read(**/.env.local)"]},
}

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
    # OpenAI quota exhaustion, sent as a 429 that would otherwise read as a
    # rate limit: error code `insufficient_quota`, message "You exceeded your
    # current quota, please check your plan and billing details" (documented,
    # not observed in the archive).
    "insufficient_quota",
    "you exceeded your current quota",
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


# --- File firewall ------------------------------------------------------------
#
# The restricted-file hooks below keep the scaffold from *priming* a sub-agent:
# they stop a lead-agent document, a ledger, a log or the condition label map
# from reaching a sub-agent through an ordinary tool call. They catch accidents,
# not intent. Read, Grep and Glob are checked on their path arguments; Bash is
# checked best-effort on the words of the command, which a determined agent
# could evade (`cat $(echo ...)`). An OS-level sandbox around the sub-agent
# process is the airtight version and is future work.

# Every sub-agent other than the lead is denied the condition label map,
# whatever its role-specific list says, so a new role (or a builder that forgot
# the entry) can never read which opaque condition id is which. One basename
# wildcard covers every iteration's map, and any backup copy beside it.
LABEL_MAP_PATTERN = "CONDITION_LABEL_MAP*"
ALWAYS_RESTRICTED: dict[str, str] = {
    LABEL_MAP_PATTERN: (
        "Private condition label map: opaque execution ids → real condition names. "
        "Only the lead agent reads it; no sub-agent may learn which condition is which."
    ),
}

# The launcher's persisted requests (`investigations/<name>/requests/`, see
# `scripts/launch_subagent.py`). Anchored to that layout rather than any
# directory called `requests/`, which would also catch the `requests` package
# or an eval environment's own folder.
INVESTIGATION_REQUESTS = "investigations/*/requests/"

# Upper bound on the directory walk behind a Grep over a directory, so a search
# rooted at a huge tree costs a bounded few milliseconds. Past the cap the walk
# stops and the search is denied with a request to narrow it: an unchecked
# tree may hold the label map, and a narrower search costs the agent one turn.
GREP_WALK_LIMIT = 20000

# Dependency and build directories the walk does not descend into. They are
# what push an ordinary eval repo past the cap (a `node_modules` alone can hold
# tens of thousands of files), and by convention they never hold investigation
# state, so skipping them removes false refusals without opening a channel.
# Deliberately a fixed list of names rather than the repo's .gitignore: that
# would only be safe if the search tool also skipped every gitignored path, and
# an investigation directory a user chose to gitignore would then go unchecked.
GREP_WALK_SKIP_DIRS = frozenset(
    {"node_modules", "__pycache__", "venv", "site-packages", "bower_components"}
)

# Bound on how many paths one wildcard word in a Bash command is expanded to.
BASH_GLOB_LIMIT = 500

# Commands that read file contents across a directory tree when given one
# (`find` via -exec; grep and ls only with a recursive flag, see
# `_is_recursive_search`). Tools that only list names are left out: a name
# alone tells nothing.
_RECURSIVE_TOOLS = frozenset({"rg", "find"})

_WILDCARD_CHARS = "*?["


def _normalised_paths(path: str, cwd: str) -> list[str]:
    """Both spellings of a tool's path argument that the firewall checks.

    Parameters
    ----------
    path : str
        The path as the tool received it — absolute, relative to the agent's
        working directory, or containing ``..`` / ``~``.
    cwd : str
        The sub-agent's working directory, against which a relative path
        resolves.

    Returns
    -------
    list[str]
        The lexical form (``normpath`` of the absolute path, so
        ``a/../investigator/x.md`` cannot slip past a pattern) and the
        resolved form (``realpath``, so a symlink cannot rename a restricted
        file), de-duplicated. A path is denied when either form matches.

    Examples
    --------
    >>> _normalised_paths("docs/../.claude/docs/investigator/x.md", "/t")[0]
    '/t/.claude/docs/investigator/x.md'
    """
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    lexical = os.path.normpath(expanded)
    return list(dict.fromkeys([lexical, os.path.realpath(lexical)]))


def _pattern_matches(pattern: str, abs_path: str) -> bool:
    """Whether one restricted-file pattern denies one normalised absolute path.

    Parameters
    ----------
    pattern : str
        A restricted-file pattern (semantics in `_build_hooks`).
    abs_path : str
        A normalised absolute path, from `_normalised_paths`.

    Returns
    -------
    bool
        True when the pattern covers the path.
    """
    if pattern.endswith("/"):
        # A directory pattern matches at a path-component boundary anywhere in
        # the path (so `requests/` never matches `my_requests/`), and also the
        # directory itself — hence the trailing "/" added to the path. Shell
        # wildcards inside it span components (`runs/*/private/`).
        return fnmatch.fnmatchcase(abs_path + "/", "*/" + pattern + "*")
    if any(ch in pattern for ch in _WILDCARD_CHARS):
        # A family of files sharing a name shape (one per iteration, say) is
        # denied by one pattern rather than an enumeration that silently stops
        # at its last entry.
        return fnmatch.fnmatchcase(os.path.basename(abs_path), pattern)
    return abs_path.endswith(pattern)


def _first_denial(
    restricted_files: dict[str, str], path: str, cwd: str
) -> tuple[str, str] | None:
    """The first restricted pattern denying `path`, with its reason.

    Parameters
    ----------
    restricted_files : dict[str, str]
        Pattern → denial reason.
    path : str
        A tool's path argument, in any spelling.
    cwd : str
        The sub-agent's working directory.

    Returns
    -------
    tuple[str, str] | None
        ``(pattern, reason)``, or None when the path is allowed.
    """
    for candidate in _normalised_paths(path, cwd):
        for pattern, reason in restricted_files.items():
            if _pattern_matches(pattern, candidate):
                return pattern, reason
    return None


def _restricted_beneath(
    restricted_files: dict[str, str], root: str, name_filter: str | None
) -> tuple[str, str] | None:
    """A restricted file or directory that a recursive search of `root` would read.

    A Grep over a directory returns matching *lines* from every file below
    it, so searching an investigation directory would quote the label map
    without ever naming it. The walk mirrors what the search tool reads by
    default: hidden directories are skipped (ripgrep skips them), symlinked
    directories are not followed, and a ``glob`` filter limits the files.

    Parameters
    ----------
    restricted_files : dict[str, str]
        Pattern → denial reason.
    root : str
        Normalised absolute directory the search is rooted at.
    name_filter : str | None
        The search's ``glob`` filter, e.g. ``"*.py"`` (its basename is used),
        or None for every file.

    Returns
    -------
    tuple[str | None, str] | None
        ``(pattern, path_found)`` for the first restricted entry;
        ``(None, root)`` when the tree is larger than `GREP_WALK_LIMIT`
        entries and could not be checked (the caller denies: fail closed);
        None when the search is clear.
    """
    name_glob = os.path.basename(name_filter) if name_filter else None
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in GREP_WALK_SKIP_DIRS
        ]
        # A restricted directory is reported only when no filter could exclude
        # its files; a restricted file only when the filter would include it.
        entries = [os.path.join(dirpath, d) for d in dirnames] if name_glob is None else []
        entries += [
            os.path.join(dirpath, f)
            for f in filenames
            if name_glob is None or fnmatch.fnmatch(f, name_glob)
        ]
        for entry in entries:
            for pattern in restricted_files:
                if _pattern_matches(pattern, entry):
                    return pattern, entry
        seen += len(dirnames) + len(filenames)
        if seen > GREP_WALK_LIMIT:
            return None, root
    return None


def _beneath_reason(
    restricted_files: dict[str, str], found: tuple[str | None, str]
) -> tuple[str, str]:
    """Denial ``(pattern, reason)`` for a `_restricted_beneath` hit.

    Parameters
    ----------
    restricted_files : dict[str, str]
        Pattern → denial reason.
    found : tuple[str | None, str]
        The walk's result: a pattern and the entry found, or ``(None, root)``
        when the tree was too large to check.

    Returns
    -------
    tuple[str, str]
        Arguments for the hook's denial message.
    """
    pattern, where = found
    if pattern is None:
        return "search root", (
            f"`{where}` holds more than {GREP_WALK_LIMIT} entries, too many to check for "
            "restricted files; search a narrower directory"
        )
    return pattern, (
        f"{restricted_files[pattern]} (the search covers `{where}`; narrow the "
        "path or the file filter so the search excludes it)"
    )


def _strip_heredocs(command: str) -> str:
    """Remove heredoc bodies, which are a program's text rather than shell syntax.

    A Python script fed through ``python - <<'EOF'`` legitimately contains
    ``&`` (a pandas mask, a bitwise and); tokenised as shell it would read as
    backgrounding.

    Parameters
    ----------
    command : str
        The Bash tool's ``command`` string.

    Returns
    -------
    str
        The command with every ``<<WORD … WORD`` body removed.

    Examples
    --------
    >>> _strip_heredocs("python - <<'EOF'\nprint(a & b)\nEOF\necho done")
    "python - <<'EOF'\necho done"
    """
    return re.sub(
        r"(<<-?\s*(['\"]?)(\w+)\2[^\n]*)\n.*?\n\s*\3[ \t]*(?=\n|$)",
        r"\1",
        command,
        flags=re.S,
    )


def _is_recursive_search(words: list[str]) -> bool:
    """Whether a command reads directory trees it is given (grep -r, rg, find …).

    Parameters
    ----------
    words : list[str]
        Output of `_command_words`.

    Returns
    -------
    bool
        True for `_RECURSIVE_TOOLS`, or grep/ls with a recursive flag
        (``-r``, ``-R``, ``-rn``, ``--recursive``).
    """
    names = {os.path.basename(w) for w in words}
    if names & _RECURSIVE_TOOLS:
        return True
    if names & {"grep", "egrep", "ls"}:
        return any(
            w == "--recursive" or (re.fullmatch(r"-[A-Za-z]+", w) and ("r" in w or "R" in w))
            for w in words
        )
    return False


def _command_words(command: str) -> list[str]:
    """Split a shell command into words and operators for the Bash checks.

    Parameters
    ----------
    command : str
        The Bash tool's ``command`` string.

    Returns
    -------
    list[str]
        Words with quotes removed, and operators (``&``, ``&&``, ``|``, ``;``,
        ``>&`` …) as their own entries. An unparseable command (an unbalanced
        quote) falls back to a whitespace split, which still exposes paths.

    Examples
    --------
    >>> _command_words("cat 'a b.md' 2>&1 &")
    ['cat', 'a b.md', '2', '>&', '1', '&']
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _bash_denial(
    restricted_files: dict[str, str], command: str, cwd: str
) -> tuple[str, str] | None:
    """Best-effort check of a Bash command against the restricted patterns.

    Every word that looks like a path (it contains "/" or names an existing
    entry) is normalised and checked as a path; `--opt=value` words are split
    at "=". A word with a shell wildcard is expanded against the filesystem
    (up to `BASH_GLOB_LIMIT` paths) and each expansion checked, so
    ``cat inv/COND*`` is caught although it never spells the name. When the
    command searches recursively (``grep -r``, ``rg``, ``find``), every
    directory it names — or the cwd, if none — is walked like a Grep root.
    Independently, the command text is searched for each pattern's fixed stem
    (``CONDITION_LABEL_MAP``, ``CANDIDATES.md``), which catches a restricted
    file named inside a longer string or a script. This catches the
    accidental read; it is not a sandbox (see the section comment): a path
    built at run time (`cat $(ls | head -1)`) is invisible to it.

    Parameters
    ----------
    restricted_files : dict[str, str]
        Pattern → denial reason.
    command : str
        The command text.
    cwd : str
        The sub-agent's working directory.

    Returns
    -------
    tuple[str, str] | None
        ``(pattern, reason)``, or None when nothing restricted is referenced.
    """
    words = _command_words(command)
    directories: list[str] = []

    # --- Path-shaped words, resolved like a Read's file_path ---
    for word in words:
        for part in word.split("="):
            if not part:
                continue
            # A wildcard word is checked as each path it expands to.
            expansions = [part]
            if any(ch in part for ch in _WILDCARD_CHARS):
                pattern_path = part if os.path.isabs(part) else os.path.join(cwd, part)
                expansions += list(itertools.islice(glob.iglob(pattern_path), BASH_GLOB_LIMIT))
            for candidate in expansions:
                looks_like_path = "/" in candidate or os.path.lexists(os.path.join(cwd, candidate))
                if not looks_like_path:
                    continue
                hit = _first_denial(restricted_files, candidate, cwd)
                if hit:
                    return hit
                resolved = _normalised_paths(candidate, cwd)[0]
                if os.path.isdir(resolved):
                    directories.append(resolved)

    # --- Recursive searches read whole trees, like a Grep over a directory ---
    if _is_recursive_search(words):
        for root in directories or [cwd]:
            found = _restricted_beneath(restricted_files, root, None)
            if found:
                return _beneath_reason(restricted_files, found)

    # --- Fixed stems anywhere in the text ---
    for pattern, reason in restricted_files.items():
        if pattern.endswith("/"):
            continue  # a directory is only meaningful as a path (checked above)
        stem = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
        stem = stem.rsplit("/", 1)[-1]
        if len(stem) >= 6 and stem in command:
            return pattern, reason
    return None


# Command words that detach a process from the agent's turn. Checked as whole
# words, so `nohup` inside a quoted string or a filename is not a match.
_BACKGROUND_WORDS = frozenset({"&", "nohup", "setsid", "disown"})


def _fail_closed(agent_name: str, hook: Any) -> Any:
    """Wrap a PreToolUse hook so an exception blocks the call instead of passing it.

    Parameters
    ----------
    agent_name : str
        For the denial message.
    hook : Callable
        An async ``(input_data, tool_use_id, context) -> dict`` hook.

    Returns
    -------
    Callable
        The same hook, returning a block decision when it raises.
    """

    @functools.wraps(hook)
    async def guarded(input_data: dict, tool_use_id: str, context: dict) -> dict:
        try:
            return await hook(input_data, tool_use_id, context)
        except Exception as exc:  # deliberately broad: a firewall must not fail open
            return {
                "decision": "block",
                "reason": f"[{agent_name}] Access check failed ({type(exc).__name__}: {exc}); "
                "the call was blocked. Retry with a plain path.",
            }

    return guarded


def _build_hooks(
    agent_name: str,
    restricted_files: dict[str, str],
    memory_abs_path: str | None,
    restrict_writes_to_memory: bool,
    cwd: str | None = None,
) -> dict[str, Any] | None:
    """Build PreToolUse hooks for file access control and memory.

    Args:
        agent_name: Human-readable agent name for error messages.
        restricted_files: Mapping of path patterns to denial reasons. A
            pattern ending in "/" denies an entire directory, matched at a
            path-component boundary anywhere in the path (shell wildcards
            inside it span components, e.g. "runs/*/private/"); a pattern
            containing a shell wildcard (`*`, `?`, `[`) is matched against
            the file's basename (e.g. "CONDITION_LABEL_MAP*" covers every
            iteration's map); any other pattern denies paths ending with it,
            which is normally a bare filename. Directory patterns
            exist so that a whole class of documents can be blinded once
            rather than enumerated — a file added to a denied directory
            later is blocked automatically, whereas a new filename is
            invisible to a list of filenames until someone remembers it.
            Enforced on Read (`file_path`), Grep and Glob (`path`, plus the
            `glob`/`pattern` joined to it, plus — for Grep over a directory —
            any restricted entry the search would read), and best-effort on
            Bash (see `_bash_denial`). Every path is normalised first.
        memory_abs_path: Absolute path to the agent's memory.md, or None.
        restrict_writes_to_memory: If True, block all writes except to
            memory.md. If False, only enforce append-only on memory.md.
        cwd: The sub-agent's working directory, against which relative tool
            paths resolve. Defaults to the process's working directory.

    Returns:
        A hooks dict suitable for ClaudeAgentOptions, or None if no
        hooks are needed.
    """
    base_cwd = cwd or os.getcwd()
    read_hooks: list[Any] = []
    search_hooks: list[Any] = []
    write_hooks: list[Any] = []
    memory_real = os.path.realpath(memory_abs_path) if memory_abs_path else None

    def deny(pattern: str, reason: str) -> dict:
        return {
            "decision": "block",
            "reason": f"[{agent_name}] Access denied: {pattern} — {reason}",
        }

    def agent_cwd(input_data: dict) -> str:
        # The CLI reports the session's cwd in every hook input; fall back to
        # the configured one (tests and older CLIs omit it).
        return input_data.get("cwd") or base_cwd

    # --- Read hook: allow memory.md, then check restricted files ---
    if restricted_files or memory_abs_path:

        async def check_read_access(
            input_data: dict, tool_use_id: str, context: dict
        ) -> dict:
            file_path = input_data.get("tool_input", {}).get("file_path", "")
            if not file_path:
                return {}
            here = agent_cwd(input_data)
            # Always allow reading the agent's own memory file.
            if memory_real and memory_real in _normalised_paths(file_path, here):
                return {}
            hit = _first_denial(restricted_files, file_path, here)
            return deny(*hit) if hit else {}

        read_hooks.append(check_read_access)

    # --- Grep/Glob hook: the same firewall on the search arguments ---
    if restricted_files:

        async def check_search_access(
            input_data: dict, tool_use_id: str, context: dict
        ) -> dict:
            tool_input = input_data.get("tool_input", {})
            here = agent_cwd(input_data)
            root = tool_input.get("path") or here
            # Glob's `pattern` and Grep's `glob` are path shapes relative to
            # the search root; Grep's `pattern` is a regex, not a path.
            shape = tool_input.get("glob") if input_data.get("tool_name") == "Grep" else tool_input.get("pattern")
            candidates = [root] + ([os.path.join(root, shape)] if shape else [])
            for candidate in candidates:
                hit = _first_denial(restricted_files, candidate, here)
                if hit:
                    return deny(*hit)
            # A content search over a directory reads every file beneath it.
            if input_data.get("tool_name") == "Grep":
                root_abs = _normalised_paths(root, here)[0]
                if os.path.isdir(root_abs):
                    found = _restricted_beneath(restricted_files, root_abs, shape)
                    if found:
                        return deny(*_beneath_reason(restricted_files, found))
            return {}

        search_hooks.append(check_search_access)

    # --- Write hook: append-only memory, optional general write restriction ---
    if memory_abs_path or restrict_writes_to_memory:

        async def check_write_access(
            input_data: dict, tool_use_id: str, context: dict
        ) -> dict:
            tool_input = input_data.get("tool_input", {})
            file_path = tool_input.get("file_path", "")
            is_memory = bool(
                memory_real
                and file_path
                and memory_real in _normalised_paths(file_path, agent_cwd(input_data))
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
    # it never affects the orchestrator or any other Claude Code session. Both
    # the tool flag and the shell's own detaching forms (a trailing `&`,
    # `nohup`, `setsid`, `disown`) are refused.
    async def check_no_background(
        input_data: dict, tool_use_id: str, context: dict
    ) -> dict:
        tool_input = input_data.get("tool_input", {})
        words = set(_command_words(_strip_heredocs(tool_input.get("command", "") or "")))
        if tool_input.get("run_in_background") or words & _BACKGROUND_WORDS:
            return {
                "decision": "block",
                "reason": (
                    f"[{agent_name}] Backgrounding is disabled for sub-agents "
                    "(no run_in_background, trailing `&`, nohup, setsid or disown). "
                    "Run the command in the foreground and wait for it to "
                    "finish; long-running commands are expected."
                ),
            }
        return {}

    bash_hooks: list[Any] = [check_no_background]

    # --- Bash hook: best-effort firewall on the command's words ---
    if restricted_files:

        async def check_bash_access(
            input_data: dict, tool_use_id: str, context: dict
        ) -> dict:
            command = input_data.get("tool_input", {}).get("command", "") or ""
            hit = _bash_denial(restricted_files, command, agent_cwd(input_data))
            return deny(*hit) if hit else {}

        bash_hooks.append(check_bash_access)

    # A hook that raises (a NUL byte in a path, say) would be treated by the
    # CLI as non-blocking, i.e. fail open; every hook is wrapped to block.
    read_hooks = [_fail_closed(agent_name, h) for h in read_hooks]
    search_hooks = [_fail_closed(agent_name, h) for h in search_hooks]
    write_hooks = [_fail_closed(agent_name, h) for h in write_hooks]
    bash_hooks = [_fail_closed(agent_name, h) for h in bash_hooks]

    matchers = []
    if read_hooks:
        matchers.append(HookMatcher(matcher="Read", hooks=read_hooks))
    if search_hooks:
        matchers.append(HookMatcher(matcher="Grep", hooks=search_hooks))
        matchers.append(HookMatcher(matcher="Glob", hooks=search_hooks))
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
    # The label map is denied to every sub-agent on top of its role's list.
    hooks = _build_hooks(
        agent_name=agent_name,
        restricted_files={**ALWAYS_RESTRICTED, **(restricted_files or {})},
        memory_abs_path=memory_abs_path,
        restrict_writes_to_memory=restrict_writes_to_memory,
        cwd=working_dir,
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
        # --- Isolation: the sub-agent sees only what the scaffold gives it ---
        # `tools` is the set the model is offered at all; under
        # bypassPermissions an unlisted default tool (Skill, Agent/Task,
        # NotebookEdit, Edit…) would otherwise still be usable, and Skill would
        # load lead-agent methodology around the `.claude/skills/` read block.
        tools=list(allowed_tools),
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        skills=[],
        # No filesystem settings: no CLAUDE.md, no user/project hooks, plugins
        # or permission rules from whichever repo the sub-agent runs in. The
        # one project rule a sub-agent relied on — never read `.env` secrets —
        # is passed explicitly below instead.
        setting_sources=[],
        settings=json.dumps(SUBAGENT_SETTINGS),
        strict_mcp_config=True,
        permission_mode="bypassPermissions",
        max_turns=max_turns or DEFAULT_MAX_TURNS,
        cwd=working_dir,
        env={**ISOLATION_ENV, **_bash_timeout_env(), **route_env},
        **({"hooks": hooks} if hooks else {}),
    )

    result: str | None = None
    result_message: ResultMessage | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message.result
                result_message = message
    except CLINotFoundError as exc:
        raise RuntimeError(
            f"{agent_name}: Claude Code CLI not found. "
            "Install with: pip install claude-agent-sdk"
        ) from exc
    except ClaudeSDKError as exc:
        # Connection and process errors are the common cases; any other SDK
        # error (a malformed CLI message, say) is still an agent error (exit
        # 2), never a bare traceback (exit 1) misread as a scaffold crash.
        if isinstance(exc, CLIConnectionError):
            kind = "connection error"
        elif isinstance(exc, ProcessError):
            kind = "process error"
        else:
            kind = f"SDK error ({type(exc).__name__})"
        # The CLI emits its error result before exiting non-zero, so the
        # usage of a failed run is usually still in hand: record it, tagged.
        # Older SDKs raise a generic "Command failed with exit code 1"
        # ProcessError and leave the provider's text only in the error
        # result, so classify both — but only an *error* result: a successful
        # result's text is the agent's report, which may well discuss credit.
        error_result = (
            result_message.result
            if result_message is not None and result_message.is_error
            else None
        )
        _raise_if_credentials_exhausted(
            agent_name, resolved_model, route,
            "\n".join(t for t in (f"{exc}", error_result) if t), result_message,
        )
        if result_message is not None:
            _write_cost_sidecar(agent_name, resolved_model, route, result_message)
        raise RuntimeError(f"{agent_name}: {kind}: {exc}") from exc

    if result_message is not None:
        _write_cost_sidecar(agent_name, resolved_model, route, result_message)

    # An API failure can also arrive as an error result with the provider's
    # text in `result` and no exception; that text is not a report, so it is
    # raised (exit 2) rather than returned and saved as one. The sidecar above
    # has already recorded its usage.
    if result_message is not None and result_message.is_error:
        _raise_if_credentials_exhausted(
            agent_name, resolved_model, route, result_message.result, result_message
        )
        raise RuntimeError(
            f"{agent_name}: the run ended in an error result "
            f"(subtype={result_message.subtype}, stop_reason={result_message.stop_reason}, "
            f"num_turns={result_message.num_turns}, max_turns={options.max_turns}): "
            f"{result_message.result}"
        )

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
        # Temp file + rename, so `spend.py` or the launcher reading the
        # sidecar concurrently never sees a half-written record.
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(record, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError as exc:  # never fail a completed invocation over telemetry
        print(f"[cost] could not write {path}: {exc}", file=sys.stderr)
