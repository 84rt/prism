"""Sub-agent isolation and the file firewall (`subagents/runner.py`).

What these pin, and why each fails silently without a test:

- **Isolation fields reach `ClaudeAgentOptions`.** With `setting_sources`
  unset the CLI loads every settings source (CLAUDE.md, auto-memory, user
  plugins and hooks); with `tools` unset a sub-agent under bypassPermissions
  is offered every default tool, Skill included — a back door around the
  `.claude/skills/` read block. Nothing errors when either is dropped.
- **Every builder passes its role's denied files, and the runner adds the
  label map for all of them.** A builder that forgets its list runs with an
  open firewall and no warning.
- **The hooks deny across tools, after normalisation.** Read, Grep, Glob and
  (best-effort) Bash; `a/../investigator/x.md` must not slip past a pattern.
- **The Critic's denied set depends on its mode**: diff_review must not see
  the Designer's reports; candidate_critique legitimately receives the ledger.
- **Run outcomes**: an error result is raised, never returned as a report;
  any SDK error is an agent error (RuntimeError → exit 2).

Run with the self-contained recipe in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from claude_agent_sdk import CLIJSONDecodeError, ResultMessage

from subagents import runner
from subagents.environment_explorer import agent as explorer
from subagents.environment_mapper import agent as mapper
from subagents.experiment_executor import agent as executor
from subagents.redteam_critic import agent as critic
from subagents.transcript_analyst import agent as analyst
from subagents.variant_designer import agent as designer

INV = "/w/target/investigations/inv1"


# --- Helpers -------------------------------------------------------------------


class FakeOptions:
    """Records the keyword arguments `run_agent` builds `ClaudeAgentOptions` from."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.max_turns = kwargs.get("max_turns")


def _result(**overrides) -> ResultMessage:
    """A ResultMessage with harmless defaults (a successful one-turn run)."""
    fields = dict(
        subtype="success", duration_ms=5, duration_api_ms=4, is_error=False, num_turns=1,
        session_id="s", stop_reason="end_turn", total_cost_usd=0.0, usage={}, result="### Summary\nok",
    )
    fields.update(overrides)
    return ResultMessage(**fields)


def _stub_sdk(monkeypatch, *messages, raises: Exception | None = None) -> list[FakeOptions]:
    """Swap the runner's options class and `query` for recorders.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
    *messages : ResultMessage
        What the fake `query` yields, in order.
    raises : Exception | None
        Raised by the fake `query` after yielding `messages`.

    Returns
    -------
    list[FakeOptions]
        Filled with the options object of each `run_agent` call.
    """
    built: list[FakeOptions] = []

    def make_options(**kwargs):
        built.append(FakeOptions(**kwargs))
        return built[-1]

    async def fake_query(prompt, options):
        for m in messages:
            yield m
        if raises is not None:
            raise raises

    monkeypatch.setattr(runner, "ClaudeAgentOptions", make_options)
    monkeypatch.setattr(runner, "query", fake_query)
    monkeypatch.delenv(runner.COST_FILE_ENV, raising=False)
    return built


def _hooks(options: FakeOptions) -> dict[str, list]:
    """PreToolUse hooks of a built options object, keyed by tool matcher."""
    return {m.matcher: m.hooks for m in options.kwargs["hooks"]["PreToolUse"]}


def _decide(hooks: list, tool: str, tool_input: dict, cwd: str = "/w/target") -> bool:
    """True when any of a matcher's hooks blocks this tool call."""
    data = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
    return any(asyncio.run(h(data, "id", {})).get("decision") == "block" for h in hooks)


def _requests(tmp_path) -> dict:
    """Minimal valid request per role: (module, entry point, data)."""
    candidates = [{"id": "C1", "slug": "s", "statement": "st", "discriminating_signature": "sig", "status": "live"}]
    return {
        "explorer": (explorer, explorer.run_environment_explorer,
                     {"experiment_description": "d", "hypothesis": "h", "environment_path": "/env"}),
        "mapper": (mapper, mapper.run_environment_mapper,
                   {"environment_path": "/env", "behaviour_description": "b"}),
        "designer": (designer, designer.run_variant_designer,
                     {"environment_path": "/env", "mapper_report_path": "/m.md", "candidates": candidates,
                      "regime": "ablate", "environment_rate": {"rate": 0.5, "n": 30}, "variant_budget": 3}),
        "executor": (executor, executor.run_experiment_executor,
                     {"experiment_name": "x", "experiment_dir": str(tmp_path / "exp"),
                      "conditions": {"c_1": "task.py"}, "models": ["mockllm/model"]}),
        "analyst": (analyst, analyst.run_transcript_analyst,
                    {"topic": "t", "transcript_source": {"condition_A": "/logs/a"}}),
        "critic": (critic, critic.run_redteam_critic,
                   {"mode": "candidate_critique", "transcript_paths": ["/t.txt"], "mapper_report_path": "/m.md",
                    "candidates": candidates, "iteration": 0}),
    }


ROLES = ["explorer", "mapper", "designer", "executor", "analyst", "critic"]


# --- A1: isolation fields ----------------------------------------------------------


def test_run_agent_isolates_the_sub_agent(monkeypatch):
    """No settings sources, no skills, exactly the allowed tools, no CLAUDE.md or memory."""
    built = _stub_sdk(monkeypatch, _result())
    asyncio.run(runner.run_agent("p", "s", ["Read", "Grep"], ["Bash"], "X", cwd="/w/target"))
    kw = built[0].kwargs
    assert kw["setting_sources"] == []
    assert kw["skills"] == []
    assert kw["tools"] == ["Read", "Grep"]  # Skill, Agent, Edit, NotebookEdit never offered
    assert kw["strict_mcp_config"] is True
    assert kw["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert kw["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    # The one project rule sub-agents relied on travels explicitly.
    assert "Read(**/.env)" in json.loads(kw["settings"])["permissions"]["deny"]


def test_memory_adds_write_to_the_offered_tools(monkeypatch, tmp_path):
    """The runner injects Write for an agent with memory; `tools` must follow, or Write is unusable."""
    built = _stub_sdk(monkeypatch, _result())
    monkeypatch.setattr(runner, "_PROJECT_ROOT", str(tmp_path))
    asyncio.run(runner.run_agent("p", "s", ["Read"], [], "X", memory_file="memory.md"))
    assert built[0].kwargs["tools"] == ["Read", "Write"]


def test_openrouter_route_keeps_the_isolation_env(monkeypatch):
    """Route variables are merged with, not substituted for, the isolation variables."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    built = _stub_sdk(monkeypatch, _result())
    asyncio.run(runner.run_agent("p", "s", ["Read"], [], "X", model="anthropic/claude-opus-4-8", route="openrouter"))
    env = built[0].kwargs["env"]
    assert env["ANTHROPIC_BASE_URL"] == runner.OPENROUTER_ANTHROPIC_BASE_URL
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


def test_real_options_accept_the_isolation_fields():
    """The pinned SDK's `ClaudeAgentOptions` has every field the runner sets (a rename would TypeError)."""
    from claude_agent_sdk import ClaudeAgentOptions

    opts = ClaudeAgentOptions(tools=["Read"], skills=[], setting_sources=[], settings="{}", strict_mcp_config=True)
    assert opts.setting_sources == [] and opts.skills == [] and opts.tools == ["Read"]


def test_launcher_pins_the_sdk_the_tests_run_against():
    """The launcher's pin names the installed release, so isolation is verified on what runs."""
    import importlib.metadata
    import sys

    sys.path.insert(0, str(Path(runner.SCAFFOLD_ROOT) / "scripts"))
    import launch_subagent as ls

    assert ls.CLAUDE_AGENT_SDK_REQUIREMENT == f"claude-agent-sdk=={importlib.metadata.version('claude-agent-sdk')}"
    assert ls.CLAUDE_AGENT_SDK_REQUIREMENT in ls.build_command("analyst", "/r.json", None)


# --- A2: matchers attached, per builder ----------------------------------------------


def test_run_agent_attaches_matchers_for_every_file_tool(monkeypatch):
    """Read, Grep, Glob, Bash and Write each get a PreToolUse hook."""
    built = _stub_sdk(monkeypatch, _result())
    asyncio.run(runner.run_agent("p", "s", ["Read"], [], "X", restricted_files={"CANDIDATES.md": "r"}))
    assert set(_hooks(built[0])) == {"Read", "Grep", "Glob", "Bash", "Write"}


@pytest.mark.parametrize("role", ROLES)
def test_every_builder_passes_its_list_and_the_runner_denies_the_label_map(monkeypatch, tmp_path, role):
    """The builder hands over its role's list, and the built hooks deny the label map whatever the role."""
    module, entry, data = _requests(tmp_path)[role]
    expected = critic.restricted_files_for(data["mode"]) if role == "critic" else module.RESTRICTED_FILES
    seen: dict = {}
    real_run_agent = module.run_agent

    async def spy(**kwargs):
        seen.update(kwargs)
        return await real_run_agent(**kwargs)

    built = _stub_sdk(monkeypatch, _result())
    monkeypatch.setattr(module, "run_agent", spy)
    asyncio.run(entry(cwd="/w/target", **data))

    assert seen["restricted_files"] == expected and expected
    hooks = _hooks(built[0])
    label_map = f"{INV}/CONDITION_LABEL_MAP_iter_01.json"
    assert _decide(hooks["Read"], "Read", {"file_path": label_map})
    assert _decide(hooks["Grep"], "Grep", {"pattern": "c_", "path": label_map})
    assert _decide(hooks["Bash"], "Bash", {"command": f"cat {label_map}"})
    assert not _decide(hooks["Read"], "Read", {"file_path": f"{INV}/iter_01/transcripts/a.txt"})


def test_analyst_list_covers_requests_reports_and_resume():
    """The Analyst is denied persisted requests, other roles' reports, RESUME.md and the label map."""
    files = analyst.RESTRICTED_FILES
    for pattern in (runner.LABEL_MAP_PATTERN, runner.INVESTIGATION_REQUESTS, "artefacts/designer/", "artefacts/critic/",
                    "artefacts/explorer/", "artefacts/mapper/", "RESUME.md", "CANDIDATES.md", "INVESTIGATION-LOG.md"):
        assert pattern in files, pattern


def test_designer_list_matches_the_launchers_report_names():
    """The Designer's denial covers `report_<tag>_<role>_<n>.md`, not a `report.md` nobody writes."""
    hooks = runner._build_hooks("D", designer.RESTRICTED_FILES, None, False, cwd="/w")
    read = {m.matcher: m.hooks for m in hooks["PreToolUse"]}["Read"]
    assert _decide(read, "Read", {"file_path": f"{INV}/iter_01/artefacts/executor/report_iter_01_executor_1.md"})
    assert _decide(read, "Read", {"file_path": f"{INV}/iter_01/artefacts/analyst/report_iter_01_analyst_1.md"})
    assert not _decide(read, "Read", {"file_path": f"{INV}/iter_01/artefacts/mapper/report_iter_01_mapper_1.md"})


def test_executor_is_denied_the_quarantine():
    """The batch runner's score-bearing `private/` is closed to the Executor; the run directory is not."""
    hooks = runner._build_hooks("E", executor.RESTRICTED_FILES, None, True, cwd="/w")
    read = {m.matcher: m.hooks for m in hooks["PreToolUse"]}["Read"]
    run = f"{INV}/iter_01/artefacts/executor/runs/x_20260917T120000Z"
    assert _decide(read, "Read", {"file_path": f"{run}/private/eval_output/c_1.attempt0.stdout"})
    assert not _decide(read, "Read", {"file_path": f"{run}/status.json"})


def test_critic_denied_set_depends_on_mode():
    """diff_review loses the Designer's reports and the requests; candidate_critique keeps them."""
    review = critic.restricted_files_for("diff_review")
    critique = critic.restricted_files_for("candidate_critique")
    for pattern in ("artefacts/designer/", runner.INVESTIGATION_REQUESTS, "INVESTIGATION-LOG.md"):
        assert pattern in review and pattern not in critique
    # prediction mode reads the investigation log it is sent.
    assert "INVESTIGATION-LOG.md" not in critic.restricted_files_for("prediction")
    hooks = runner._build_hooks("C", {**runner.ALWAYS_RESTRICTED, **review}, None, True, cwd="/w")
    read = {m.matcher: m.hooks for m in hooks["PreToolUse"]}["Read"]
    assert _decide(read, "Read", {"file_path": f"{INV}/artefacts/designer/report_iter_01_designer_1.md"})
    assert _decide(read, "Read", {"file_path": f"{INV}/CONDITION_LABEL_MAP.json"})


# --- A2: hook behaviour across tools ---------------------------------------------------

RESTRICTED = {
    ".claude/docs/investigator/": "lead docs",
    "requests/": "requests",
    runner.LABEL_MAP_PATTERN: "label map",
    "CANDIDATES.md": "ledger",
}


def _matchers(cwd: str = "/w/target") -> dict[str, list]:
    hooks = runner._build_hooks("T", RESTRICTED, None, False, cwd=cwd)
    return {m.matcher: m.hooks for m in hooks["PreToolUse"]}


@pytest.mark.parametrize(
    "path",
    [
        "/w/target/.claude/docs/foo/../investigator/subagent_invocation.md",  # `..` normalised
        ".claude/docs/investigator/subagent_invocation.md",  # relative to the cwd
        f"{INV}/iter_01/../CONDITION_LABEL_MAP_iter_01.json",
        f"{INV}/requests",  # the directory itself
    ],
)
def test_read_denies_after_normalisation(path):
    assert _decide(_matchers()["Read"], "Read", {"file_path": path})


def test_directory_pattern_matches_whole_components_only():
    """`requests/` does not catch `my_requests/` (component boundary), but does catch `requests/x`."""
    read = _matchers()["Read"]
    assert not _decide(read, "Read", {"file_path": "/w/target/my_requests/a.json"})
    assert _decide(read, "Read", {"file_path": f"{INV}/requests/iter_01_analyst_1.json"})


def test_symlink_to_a_restricted_file_is_denied(tmp_path):
    """A link with an innocent name resolves to the label map, and the resolved form is checked."""
    target = tmp_path / "CONDITION_LABEL_MAP_iter_01.json"
    target.write_text("{}")
    link = tmp_path / "notes.json"
    link.symlink_to(target)
    assert _decide(_matchers(str(tmp_path))["Read"], "Read", {"file_path": str(link)})


def test_grep_and_glob_check_path_and_shape_arguments():
    m = _matchers()
    assert _decide(m["Grep"], "Grep", {"pattern": "x", "path": "/w/target/.claude/docs/investigator"})
    assert _decide(m["Grep"], "Grep", {"pattern": "x", "path": INV, "glob": "CONDITION_LABEL_MAP*"})
    assert _decide(m["Glob"], "Glob", {"pattern": "**/CONDITION_LABEL_MAP*.json", "path": INV})
    assert _decide(m["Glob"], "Glob", {"pattern": "requests/*.json", "path": INV})
    # A Glob merely listing names elsewhere is fine.
    assert not _decide(m["Glob"], "Glob", {"pattern": "**/*.json", "path": f"{INV}/iter_01"})


def test_grep_over_a_directory_holding_a_restricted_file_is_denied(tmp_path):
    """A content search would quote the label map without naming it; a narrower one is allowed."""
    inv = tmp_path / "inv"
    (inv / "iter_01" / "transcripts").mkdir(parents=True)
    (inv / "CONDITION_LABEL_MAP_iter_01.json").write_text("{}")
    (inv / "iter_01" / "transcripts" / "a.txt").write_text("hello")
    grep = _matchers(str(tmp_path))["Grep"]
    assert _decide(grep, "Grep", {"pattern": "c_", "path": str(inv)}, cwd=str(tmp_path))
    assert _decide(grep, "Grep", {"pattern": "c_"}, cwd=str(inv))  # no path: the cwd is searched
    assert not _decide(grep, "Grep", {"pattern": "c_", "path": str(inv / "iter_01")}, cwd=str(tmp_path))
    # A glob filter that excludes the map lets the search through.
    assert not _decide(grep, "Grep", {"pattern": "c_", "path": str(inv), "glob": "*.txt"}, cwd=str(tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        f"cat {INV}/CONDITION_LABEL_MAP_iter_01.json",
        "cat ../CONDITION_LABEL_MAP*.json",  # stem inside a glob
        "jq . .claude/docs/investigator/subagent_invocation.md",
        f"python x.py --ledger={INV}/CANDIDATES.md",  # --opt=value split
        f"ls {INV}/requests/",
    ],
)
def test_bash_denies_commands_naming_restricted_files(command):
    assert _decide(_matchers()["Bash"], "Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    ["uv run python scripts/scan.py --out /w/target/inv/iter_01/artefacts/analyst", "pip install requests", "ls -la"],
)
def test_bash_allows_ordinary_commands(command):
    """Bare words are not paths unless they exist: `requests` the package is not `requests/` the directory."""
    assert not _decide(_matchers()["Bash"], "Bash", {"command": command})


# --- A3.11: backgrounding ------------------------------------------------------------------


@pytest.mark.parametrize("command", ["sleep 100 &", "nohup python x.py", "setsid python x.py", "a & b"])
def test_backgrounding_forms_are_blocked(command):
    assert _decide(_matchers()["Bash"], "Bash", {"command": command})


@pytest.mark.parametrize("command", ["a && b", "x 2>&1 | tee log", "echo 'tom & jerry'", "cmd &> out.log"])
def test_foreground_forms_are_allowed(command):
    assert not _decide(_matchers()["Bash"], "Bash", {"command": command})


def test_run_in_background_flag_is_blocked():
    assert _decide(_matchers()["Bash"], "Bash", {"command": "ls", "run_in_background": True})


# --- A3.2 / A3.6 / A3.8: run outcomes ---------------------------------------------------------


def test_error_result_is_raised_not_returned(monkeypatch):
    """An API 500 delivered as an error result (subtype success!) is not a report."""
    _stub_sdk(monkeypatch, _result(is_error=True, result="API Error: 500 Internal server error", stop_reason=None))
    with pytest.raises(RuntimeError, match=r"error result \(subtype=success, stop_reason=None"):
        asyncio.run(runner.run_agent("p", "s", ["Read"], [], "X"))


def test_error_result_still_writes_the_cost_sidecar(monkeypatch, tmp_path):
    _stub_sdk(monkeypatch, _result(is_error=True, result="API Error: 500", total_cost_usd=0.5))
    cost = tmp_path / "cost.json"
    monkeypatch.setenv(runner.COST_FILE_ENV, str(cost))
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run_agent("p", "s", ["Read"], [], "X"))
    assert json.loads(cost.read_text())["total_cost_usd"] == 0.5


def test_any_sdk_error_is_an_agent_error_with_its_sidecar(monkeypatch, tmp_path):
    """A non-connection SDK error (malformed CLI JSON) becomes RuntimeError → exit 2, cost recorded."""
    _stub_sdk(monkeypatch, _result(total_cost_usd=0.25), raises=CLIJSONDecodeError("{bad", ValueError("x")))
    cost = tmp_path / "cost.json"
    monkeypatch.setenv(runner.COST_FILE_ENV, str(cost))
    with pytest.raises(RuntimeError, match="SDK error") as info:
        asyncio.run(runner.run_agent("p", "s", ["Read"], [], "X"))
    assert isinstance(info.value.__cause__, CLIJSONDecodeError)  # chained, not swallowed
    assert json.loads(cost.read_text())["total_cost_usd"] == 0.25


def test_successful_report_about_credit_is_not_classified(monkeypatch):
    """A clean report that discusses a billing-themed eval is returned, not mistaken for a cap."""
    report = "### Summary\nThe model told the user their credit balance is too low."
    _stub_sdk(monkeypatch, _result(result=report))
    assert asyncio.run(runner.run_agent("p", "s", ["Read"], [], "X")) == report


# --- Review follow-ups: wildcards, recursion, heredocs, anchoring, fail-closed ----------------


def _investigation(tmp_path) -> Path:
    """A small investigation tree holding a label map and one transcript."""
    inv = tmp_path / "investigations" / "inv1"
    (inv / "iter_01" / "transcripts").mkdir(parents=True)
    (inv / "CONDITION_LABEL_MAP_iter_01.json").write_text("{}")
    (inv / "iter_01" / "transcripts" / "a.txt").write_text("hello")
    return inv


@pytest.mark.parametrize("command", ["cat COND*", "cat *.json", "head -c 100 ./C?NDITION*"])
def test_bash_wildcards_are_checked_as_their_expansions(tmp_path, command):
    """A glob that never spells the label map's name still reaches it."""
    inv = _investigation(tmp_path)
    assert _decide(_matchers(str(inv))["Bash"], "Bash", {"command": command}, cwd=str(inv))


@pytest.mark.parametrize("command", ["grep -r hello .", "grep -rn x", "rg hello", "find . -name '*.txt'"])
def test_bash_recursive_searches_over_a_restricted_tree_are_denied(tmp_path, command):
    inv = _investigation(tmp_path)
    assert _decide(_matchers(str(inv))["Bash"], "Bash", {"command": command}, cwd=str(inv))


def test_bash_recursive_search_of_a_clean_subtree_is_allowed(tmp_path):
    inv = _investigation(tmp_path)
    bash = _matchers(str(inv))["Bash"]
    assert not _decide(bash, "Bash", {"command": "grep -rn hello iter_01/transcripts"}, cwd=str(inv))
    assert not _decide(bash, "Bash", {"command": "grep hello iter_01/transcripts/a.txt"}, cwd=str(inv))


def test_ampersand_inside_a_heredoc_script_is_not_backgrounding():
    """A pandas mask in a `python - <<'EOF'` body is program text, not shell."""
    command = "python - <<'EOF'\nimport pandas as pd\nprint((a > 1) & (b < 2))\nEOF"
    assert not _decide(_matchers()["Bash"], "Bash", {"command": command})
    assert _decide(_matchers()["Bash"], "Bash", {"command": command + "\nsleep 10 &"})


def test_requests_denial_is_anchored_to_the_investigation_layout():
    """The Analyst may read the `requests` package; it may not read persisted requests."""
    hooks = runner._build_hooks("A", analyst.RESTRICTED_FILES, None, False, cwd="/w")
    read = {m.matcher: m.hooks for m in hooks["PreToolUse"]}["Read"]
    assert not _decide(read, "Read", {"file_path": "/venv/lib/python3.12/site-packages/requests/api.py"})
    assert not _decide(read, "Read", {"file_path": "/w/target/env/requests/task.json"})
    assert _decide(read, "Read", {"file_path": f"{INV}/requests/iter_01_designer_1.json"})


def test_reask_reports_stay_readable():
    """A re-ask reads its own previous report: the Analyst under analyst/, a diff_review Critic under critic/."""
    a = runner._build_hooks("A", analyst.RESTRICTED_FILES, None, False, cwd="/w")
    c = runner._build_hooks("C", critic.restricted_files_for("diff_review"), None, True, cwd="/w")
    a_read = {m.matcher: m.hooks for m in a["PreToolUse"]}["Read"]
    c_read = {m.matcher: m.hooks for m in c["PreToolUse"]}["Read"]
    assert not _decide(a_read, "Read", {"file_path": f"{INV}/iter_01/artefacts/analyst/report_iter_01_analyst_1.md"})
    assert not _decide(c_read, "Read", {"file_path": f"{INV}/artefacts/critic/report_iter_01_critic_1.md"})


def test_grep_over_an_uncheckably_large_tree_fails_closed(tmp_path, monkeypatch):
    """Past the walk cap the label map might lie unseen; the search is denied, not waved through."""
    monkeypatch.setattr(runner, "GREP_WALK_LIMIT", 3)
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x")
    grep = _matchers(str(tmp_path))["Grep"]
    assert _decide(grep, "Grep", {"pattern": "x", "path": str(tmp_path)})


def test_dependency_directories_do_not_count_toward_the_walk_cap(tmp_path, monkeypatch):
    """A big `node_modules` must not turn an ordinary repo-wide search into a refusal."""
    monkeypatch.setattr(runner, "GREP_WALK_LIMIT", 3)
    deps = tmp_path / "node_modules" / "pkg"
    deps.mkdir(parents=True)
    for i in range(10):
        (deps / f"f{i}.js").write_text("x")
    (tmp_path / "task.py").write_text("x")
    grep = _matchers(str(tmp_path))["Grep"]
    assert not _decide(grep, "Grep", {"pattern": "x", "path": str(tmp_path)})


def test_skipping_dependency_dirs_still_finds_restricted_files_elsewhere(tmp_path):
    """The skip list prunes only dependency dirs; investigation state beside them is still caught."""
    (tmp_path / "node_modules").mkdir()
    inv = tmp_path / "investigations" / "demo"
    inv.mkdir(parents=True)
    (inv / "CONDITION_LABEL_MAP_iter1.json").write_text("{}")
    grep = _matchers(str(tmp_path))["Grep"]
    assert _decide(grep, "Grep", {"pattern": "x", "path": str(tmp_path)})


def test_a_hook_that_raises_blocks_the_call():
    """A NUL byte makes path resolution raise; the firewall must fail closed, not open."""
    assert _decide(_matchers()["Read"], "Read", {"file_path": "/w/target/a\x00b"})
