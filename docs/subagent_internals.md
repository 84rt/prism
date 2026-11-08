# Sub-Agent Runner Internals

Maintainer notes on how sub-agents are run. The lead agents do not need any of this to run an investigation — their working reference is `.claude/docs/investigator/subagent_invocation.md` — so it lives here, outside the documents they load.

## The CLI under the launcher

The launcher runs `uv run --with claude-agent-sdk==0.2.165 --with python-dotenv python subagents/<agent>/main.py <input.json> [--cwd <dir>]` — input JSON with agent-specific fields, markdown report on stdout, errors on stderr. The SDK version is pinned (`launch_subagent.CLAUDE_AGENT_SDK_REQUIREMENT`) to the release the test suite runs against, because the isolation below was verified on it; move it deliberately.

The launcher starts each sub-agent under its own session (`setsid` semantics), so a harness teardown or a Bash timeout cannot kill it. A receipt records each pid with its process start time, so a pid recycled by an unrelated process after a restart reads as dead, not running. The sub-agent CLI strips any text before the first `### ` heading as it emits the report.

`list --unfinished` leaves out a failed invocation once a later one of the same role and tag, asking for the same work — the request equal apart from `subagent_*` and the re-ask pair `conformance_defects` / `previous_report` — has exited 0.

## Isolation

The runner loads no settings files (`setting_sources=[]`: no `CLAUDE.md`, no auto-memory, no user or project hooks, plugins, skills or permission rules from whichever repo it runs in), offers exactly its role's tools (`tools=` its allowed list — no Skill, Agent/Task, Edit or NotebookEdit), and enforces its role's denied files with PreToolUse hooks on Read, Grep, Glob and (best-effort, word-level) Bash, after normalising every path. A content search over a directory (Grep, or Bash `grep -r` / `rg` / `find`) is denied when a restricted file lies beneath it — or when the tree is too large to check — with a request to narrow it; Bash wildcards are checked as the paths they expand to. The condition label map (`CONDITION_LABEL_MAP*`) is denied to every sub-agent (`runner.ALWAYS_RESTRICTED`) and listed again in the Analyst's `RESTRICTED_FILES`. The hooks catch the accidental read, not a determined one; an OS-level sandbox is future work.

## Request fields

- **`subagent_max_turns`** is threaded through `runner.py`; each agent's `agent.py` prompt builder must pass it to `run_agent(max_turns=…)` — a builder that does not read the key silently drops it. All six builders read it, and a test pins this, so a new builder that forgets it fails the suite.
- **`subagent_thinking`** defaults to `{"type": "adaptive"}`, which requires Opus 4.6 or later. Claude sub-agents run at `runner.DEFAULT_EFFORT`.
- **`subagent_route: "openrouter"` and context windows.** The Claude Code CLI only knows Claude models' windows: for any other model it prints a `[claude-code:unrecognized_model]` warning to stderr — a note that it is *assuming* a window size, not a rejection — and would compact a 1M-window model at ~200k tokens. So for a non-Claude model on this route the runner sets `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` to the model's window, read from OpenRouter's public model catalogue (the largest window that at least half of its endpoints serve, so a rare small-window host does not shrink it; one key-free request, cached per process). Offline, or for a model the catalogue does not list, it leaves the CLI default in place and logs one `[route] …` stderr line; the invocation is never failed over it. A `CLAUDE_CODE_MAX_CONTEXT_TOKENS` exported by the user wins.
- **Every role is open to any sufficiently capable model.** The contracts and system prompts are meant to specify each role completely enough that any capable agent can fulfil it. The defaults remain Claude because that is what the scaffold has been exercised against, not because the roles depend on it. Running the Analyst cross-family is additionally a robustness check on the scaffold's assumption that the sub-agent model is infrastructure rather than part of the experiment — the one place where a differing result is informative rather than merely a bug.
- **Report conformance re-asks.** The builder validates the pair (`conformance_defects` a non-empty list of strings, `previous_report` an absolute path to an existing file, neither without the other); a malformed re-ask is an agent error (exit 2), never silently run as a first attempt. A Claude sub-agent's report may be checked the same way, but the rule binds only for non-Claude models, where substituted and merged headings were observed in every family tried.

## The Analyst's forensic mode

- `agent.py` selects the forensic system prompt when `behaviour_spec` is present and the unchanged Orchestrator prompt when it is absent; the base pipeline's behaviour is preserved by leaving the field out, not by any in-prompt conditional.
- `main.py`'s `required_fields` is unchanged (`topic`, `transcript_source`). `agent.py` raises on a malformed `construal_checks` (both fields, non-list, empty property, duplicate ids), and on the retired single-string `manipulation_check` field, when the prompt is built rather than in the CLI's input validation.
- `agent.py` renders the construal checks as their own `## Construal Checks` request section with the no-verdict rule attached, assigns `K<n>` to any check sent without an id, and appends a `## Work Order` section to every forensic request restating the addendum's order.
- `execution_notes` is rendered under its own `## Execution Notes` heading with a data-quality-only rule.
