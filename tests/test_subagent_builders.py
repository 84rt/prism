"""Cross-agent invariants of the six `agent.py` prompt builders.

Two wirings must hold for every sub-agent, and both fail *silently* when a
builder forgets them — which is why they are pinned here rather than trusted
to review:

- ``subagent_max_turns`` reaches ``run_agent(max_turns=…)``. A builder that
  does not read the key discards it with no error (the ``**data`` hazard), so
  a caller raising the cap for a long Analyst work order would get 100 anyway.
- The prompt names each document the agent must read first by its absolute
  path inside the scaffold checkout. A cwd-relative path made a Critic fail as
  a gate twice in one run, because file searches skip the gitignored symlinks
  an installed target is made of.

Run with the self-contained recipe in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from subagents import runner
from subagents.environment_explorer import agent as explorer
from subagents.environment_mapper import agent as mapper
from subagents.experiment_executor import agent as executor
from subagents.redteam_critic import agent as critic
from subagents.transcript_analyst import agent as analyst
from subagents.variant_designer import agent as designer
from subagents.attack_designer import agent as attack_designer

CONTRACTS = ".claude/docs/contracts"
REFERENCES = ".claude/docs/references"
TURNS = 7  # any non-default value, so a dropped key is visible


# --- Helpers -----------------------------------------------------------------


def _capture(monkeypatch, module) -> dict:
    """Replace the module's `run_agent` with a recorder; return the record.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
    module : module
        The `agent.py` module whose imported `run_agent` is swapped.

    Returns
    -------
    dict
        Filled with the keyword arguments of the call once the builder runs.
    """
    got: dict = {}

    async def fake_run_agent(**kwargs):
        got.update(kwargs)
        return "### Summary\nok"

    monkeypatch.setattr(module, "run_agent", fake_run_agent)
    return got


def _abs(relpath: str) -> str:
    """Absolute scaffold path the builders should be naming."""
    return runner.scaffold_doc(relpath)


# Minimal valid requests, one per role. Each carries the turn cap.
def _requests(tmp_path) -> dict:
    candidates = [
        {
            "id": "C1",
            "slug": "s",
            "statement": "st",
            "discriminating_signature": "sig",
            "status": "live",
        }
    ]
    return {
        "explorer": (
            explorer,
            explorer.run_environment_explorer,
            {"experiment_description": "d", "hypothesis": "h", "environment_path": "/env"},
            [f"{CONTRACTS}/explorer_interface_contract.md"],
        ),
        "mapper": (
            mapper,
            mapper.run_environment_mapper,
            {"environment_path": "/env", "behaviour_description": "b"},
            [f"{CONTRACTS}/mapper_interface_contract.md"],
        ),
        "designer": (
            designer,
            designer.run_variant_designer,
            {
                "environment_path": "/env",
                "mapper_report_path": "/m.md",
                "candidates": candidates,
                "regime": "ablate",
                "environment_rate": {"rate": 0.5, "n": 30},
                "variant_budget": 3,
            },
            [f"{CONTRACTS}/designer_interface_contract.md"],
        ),
        "executor": (
            executor,
            executor.run_experiment_executor,
            {
                "experiment_name": "x",
                "experiment_dir": str(tmp_path / "exp"),
                "conditions": {"baseline": "task.py"},
                "models": ["mockllm/model"],
            },
            [f"{CONTRACTS}/executor_interface_contract.md", f"{REFERENCES}/inspect_reference.md"],
        ),
        "analyst": (
            analyst,
            analyst.run_transcript_analyst,
            {"topic": "t", "transcript_source": {"condition_A": "/logs/a"}},
            [f"{CONTRACTS}/analyst_interface_contract.md", f"{REFERENCES}/scout_reference.md"],
        ),
        "critic": (
            critic,
            critic.run_redteam_critic,
            {
                "mode": "candidate_critique",
                "transcript_paths": ["/t.txt"],
                "mapper_report_path": "/m.md",
                "candidates": candidates,
                "iteration": 0,
            },
            [f"{CONTRACTS}/critic_interface_contract.md"],
        ),
        "attack_designer": (
            attack_designer,
            attack_designer.run_attack_designer,
            {
                "environment_description": "e",
                "side_task_description": "s",
                "current_prompts": [{"path": "/p/strategy_1.md", "sha256": "0" * 64,
                                     "outcomes": {"side_task_success_rate": 0.5, "caught_rate": 0.4,
                                                  "undetected_success_rate": 0.3, "n": 10}}],
                "baseline_outcomes": {"side_task_success_rate": 0.4, "caught_rate": 0.5,
                                      "undetected_success_rate": 0.2, "n": 10},
                "variants_requested": 2,
                "output_dir": str(tmp_path / "strategies"),
            },
            [f"{CONTRACTS}/attack_designer_interface_contract.md"],
        ),
    }


ROLES = ["explorer", "mapper", "designer", "executor", "analyst", "critic", "attack_designer"]


# --- Invariants ----------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_every_builder_threads_max_turns(role, tmp_path, monkeypatch):
    module, run_fn, data, _ = _requests(tmp_path)[role]
    got = _capture(monkeypatch, module)
    asyncio.run(run_fn(cwd=None, **data, subagent_max_turns=TURNS))
    assert got["max_turns"] == TURNS


@pytest.mark.parametrize("role", ROLES)
def test_every_builder_names_its_documents_by_absolute_path(role, tmp_path, monkeypatch):
    module, run_fn, data, docs = _requests(tmp_path)[role]
    got = _capture(monkeypatch, module)
    asyncio.run(run_fn(cwd=None, **data))
    for rel in docs:
        path = _abs(rel)
        assert os.path.isabs(path) and os.path.isfile(path)
        assert path in got["prompt"], f"{role}: {rel} not named by absolute path"


def test_forensic_addendum_named_only_in_forensic_mode(tmp_path, monkeypatch):
    """The Orchestrator-mode Analyst must never be pointed at the addendum."""
    addendum = _abs(f"{CONTRACTS}/analyst_forensic_addendum.md")
    _, run_fn, data, _ = _requests(tmp_path)["analyst"]

    got = _capture(monkeypatch, analyst)
    asyncio.run(run_fn(cwd=None, **data))
    assert addendum not in got["prompt"]

    got = _capture(monkeypatch, analyst)
    asyncio.run(run_fn(cwd=None, **data, behaviour_spec={"description": "b", "grader_dir": "/g"}))
    assert addendum in got["prompt"]


def test_missing_document_is_an_agent_error(monkeypatch):
    """A builder whose contract is missing fails before any spend (exit 2 path)."""
    monkeypatch.setattr(runner, "SCAFFOLD_ROOT", "/nonexistent-scaffold")
    with pytest.raises(RuntimeError, match="scaffold document"):
        runner.contract_section(f"{CONTRACTS}/mapper_interface_contract.md")


# --- The one re-ask: `conformance_defects` + `previous_report` -----------------

# Every role a lead agent re-asks after a failed `check_report.py`; the
# Executor is absent because its report is regenerated, never re-asked.
REASK_ROLES = ["explorer", "mapper", "designer", "analyst", "critic", "attack_designer"]

# The role-specific rule for re-deriving a genuinely missing section, which
# must reach the prompt (it is what stops a re-ask from redoing the work).
REASK_NOTES = {
    "explorer": "Re-read the environment only for such a section.",
    "mapper": "Re-read the environment only for such a section.",
    "designer": "do not re-design",
    "analyst": "never re-run a scan",
    "critic": "discriminating signature",
    "attack_designer": "do not re-design",
}

DEFECTS = ["missing section `### Summary`", "heading `### Findings` is not in the contract"]


def _previous_report(tmp_path) -> str:
    """A saved non-conformant report the re-ask points at."""
    path = tmp_path / "artefacts" / "report_iter1_x_1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Summary\nThe content is here under the wrong headings.\n")
    return str(path)


@pytest.mark.parametrize("role", REASK_ROLES)
def test_reask_section_is_rendered_for_every_reasked_role(role, tmp_path, monkeypatch):
    """Capture the built prompt: defects verbatim, the report path, the rule."""
    module, run_fn, data, _ = _requests(tmp_path)[role]
    previous = _previous_report(tmp_path)
    got = _capture(monkeypatch, module)
    asyncio.run(run_fn(cwd=None, **data, conformance_defects=DEFECTS, previous_report=previous))
    prompt = got["prompt"]
    section = prompt[prompt.index(runner.REASK_HEADING):]
    for d in DEFECTS:
        assert f"- {d}" in section
    assert f"`{previous}`" in section
    assert "restructured into the contract's exact section headings" in section
    assert REASK_NOTES[role] in section


@pytest.mark.parametrize("role", REASK_ROLES)
def test_first_attempt_has_no_reask_section(role, tmp_path, monkeypatch):
    module, run_fn, data, _ = _requests(tmp_path)[role]
    got = _capture(monkeypatch, module)
    asyncio.run(run_fn(cwd=None, **data))
    assert runner.REASK_HEADING not in got["prompt"]


@pytest.mark.parametrize("role", REASK_ROLES)
@pytest.mark.parametrize(
    "malformed",
    [
        "defects_only",
        "report_only",
        "defects_not_a_list",
        "defects_empty",
        "defect_not_a_string",
        "defect_whitespace_only",
        "relative_report",
        "missing_report",
    ],
)
def test_malformed_reask_is_an_agent_error(role, malformed, tmp_path, monkeypatch):
    """A malformed re-ask raises (exit 2) before any spend — never a silent drop."""
    module, run_fn, data, _ = _requests(tmp_path)[role]
    previous = _previous_report(tmp_path)
    fields = {
        "defects_only": {"conformance_defects": DEFECTS},
        "report_only": {"previous_report": previous},
        "defects_not_a_list": {"conformance_defects": "missing section", "previous_report": previous},
        "defects_empty": {"conformance_defects": [], "previous_report": previous},
        "defect_not_a_string": {"conformance_defects": [{"line": "x"}], "previous_report": previous},
        "defect_whitespace_only": {"conformance_defects": ["missing section `### Summary`", "   "], "previous_report": previous},
        "relative_report": {"conformance_defects": DEFECTS, "previous_report": "artefacts/report.md"},
        "missing_report": {"conformance_defects": DEFECTS, "previous_report": str(tmp_path / "nope.md")},
    }[malformed]
    _capture(monkeypatch, module)
    with pytest.raises(RuntimeError, match="malformed re-ask"):
        asyncio.run(run_fn(cwd=None, **data, **fields))


@pytest.mark.parametrize("field", ["conformance_defects", "previous_report"])
def test_executor_rejects_reask_fields(field, tmp_path, monkeypatch):
    """The Executor is never re-asked; a re-ask field is an error, not a drop."""
    module, run_fn, data, _ = _requests(tmp_path)["executor"]
    got = _capture(monkeypatch, module)
    value = DEFECTS if field == "conformance_defects" else _previous_report(tmp_path)
    with pytest.raises(RuntimeError, match="not accepted"):
        asyncio.run(run_fn(cwd=None, **data, **{field: value}))
    assert not got  # rejected before run_agent


# --- Standing invariant: every key a builder reads is documented ---------------

# Each role's section of `subagent_invocation.md`, as (start, end) headings.
_INVOCATION_SECTIONS = {
    "environment_explorer": ("## Environment Explorer", "## Experiment Executor"),
    "experiment_executor": ("## Experiment Executor", "## Transcript Analyst"),
    "transcript_analyst": ("## Transcript Analyst", "## Environment Mapper"),
    "environment_mapper": ("## Environment Mapper", "## Variant Designer"),
    "variant_designer": ("## Variant Designer", "## Red-Teaming Critic"),
    "redteam_critic": ("## Red-Teaming Critic", "## Attack Designer"),
    "attack_designer": ("## Attack Designer", None),
}


@pytest.mark.parametrize("agent_dir", sorted(_INVOCATION_SECTIONS))
def test_every_data_key_read_by_each_builder_is_documented_in_its_section(agent_dir):
    """A key the builder reads must be documented in that role's own section
    of the invocation reference; only the shared ``subagent_*`` fields may
    rely on the General Pattern. (The Analyst's own test in
    `test_analyst_prompt_builder.py` is the original of this invariant.)"""
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = Path(os.path.join(root, "subagents", agent_dir, "agent.py")).read_text()
    keys = set(re.findall(r"data(?:\.get\(|\[)['\"](\w+)['\"]", src))
    doc = Path(os.path.join(root, ".claude", "docs", "investigator", "subagent_invocation.md")).read_text()
    start, end = _INVOCATION_SECTIONS[agent_dir]
    section = doc.split(start, 1)[1]
    section = section.split(end, 1)[0] if end else section
    general = doc.split("## General Pattern")[1].split("## Environment Explorer")[0]

    def documented(k: str) -> bool:
        if f"`{k}`" in section or f'"{k}"' in section:
            return True
        return k.startswith("subagent_") and f"`{k}`" in general

    missing = sorted(k for k in keys if not documented(k))
    assert not missing, f"{agent_dir}/agent.py reads undocumented request keys: {missing}"


# --- Standing invariant, runtime direction: every documented key reaches the agent ---
#
# The grep above catches a key the builder reads but the docs omit. The more
# dangerous direction is the reverse — a field documented (and so sent) that
# the builder never reads, which `**data` discards without a trace. Here each
# key the invocation reference documents for a role is sent with a sentinel
# value, and the sentinel must reach the built prompt or a `run_agent` kwarg.
# The key list is parsed from the reference itself, so a newly documented
# field is covered the day it is documented.

_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude", "docs", "investigator", "subagent_invocation.md")
_ROLE_DIRS = {
    "explorer": "environment_explorer",
    "executor": "experiment_executor",
    "analyst": "transcript_analyst",
    "mapper": "environment_mapper",
    "designer": "variant_designer",
    "critic": "redteam_critic",
    "attack_designer": "attack_designer",
}
_CRITIC_MODES = ("candidate_critique", "diff_review", "prediction")

# Backticked names that lead a bullet in a role's section but are not
# top-level request keys for this check: the Critic's mode names (values of
# `mode`, which selects the branch rather than carrying content); `description`
# (a sub-field of the Analyst's `behaviour_spec`); and the re-ask pair, which
# must travel together with a real file and is pinned by
# `test_reask_section_is_rendered_for_every_reasked_role` (and refused by the
# Executor, `test_executor_rejects_reask_fields`).
_NOT_CHECKED_HERE = {"mode", *_CRITIC_MODES, "description", "conformance_defects", "previous_report"}

_NUM = 7919  # a distinctive number for integer-valued keys


def _role_section(role: str) -> str:
    """The role's section of the invocation reference."""
    import re

    doc = Path(_DOC).read_text()
    start, end = _INVOCATION_SECTIONS[_ROLE_DIRS[role]]
    section = doc.split(start, 1)[1]
    section = section.split(end, 1)[0] if end else section
    general = doc.split("## General Pattern", 1)[1].split("## Environment Explorer", 1)[0]
    return section, re.findall(r"^### Optional: `(subagent_\w+)`", general, re.M)


def _documented_keys(role: str) -> set[str]:
    """Top-level request keys the reference documents for a role.

    Parameters
    ----------
    role : str
        One of `_ROLE_DIRS`.

    Returns
    -------
    set[str]
        Keys of every ```json example in the section, names leading a bullet,
        and the General Pattern's shared ``subagent_*`` fields.
    """
    import json
    import re

    section, general = _role_section(role)
    keys = set(general)
    for block in re.findall(r"```json\n(.*?)\n```", section, re.S):
        keys |= set(json.loads(block))
    for line in section.splitlines():
        m = re.match(r"^- ((?:`\w+`(?:,? (?:and |/ )?|/)?)+)", line)
        if m:
            keys |= set(re.findall(r"`(\w+)`", m.group(1)))
    # The Critic's per-mode fields are named in its "- `<mode>` requires …" bullets.
    if role == "critic":
        keys |= {k for ks in _critic_mode_keys().values() for k in ks}
    return keys - _NOT_CHECKED_HERE


def _critic_mode_keys() -> dict[str, list[str]]:
    """Per Critic mode, the fields its "- `<mode>` requires …" bullet names (before any " — ")."""
    import re

    section, _ = _role_section("critic")
    out = {}
    for mode in _CRITIC_MODES:
        line = next(ln for ln in section.splitlines() if ln.startswith(f"- `{mode}` requires"))
        out[mode] = re.findall(r"`(\w+)`", line.split(" — ")[0].split("requires", 1)[1])
    return out


def _sentinel(key: str, tmp_path) -> tuple[object, str]:
    """A value for `key` shaped as the builder expects, carrying a findable token.

    Parameters
    ----------
    key : str
        The request key.
    tmp_path : Path
        For keys that must name an existing directory.

    Returns
    -------
    tuple[object, str]
        ``(value, token)`` — the token must appear in what the agent receives.
        Unlisted keys default to a plain string, which suits every free-text
        field (a newly documented one included).
    """
    s = f"SENTINEL_{key.upper()}"
    shaped = {
        "transcript_source": {"condition_A": s},
        "conditions": {"c_0001": {"task": s, "args": {}}},
        "models": [s],
        "model_routing": {"m": {"route": "direct", "provider_slug": s, "model_args": {}}},
        "overrides": {"sample_id": s},
        "candidates": [{"id": "C1", "slug": s, "statement": "st", "discriminating_signature": "sig", "status": "live"}],
        "environment_delta": [{"name": s, "description": "d"}],
        "variations_tried": [{"name": s, "description": "d"}],
        "elimination_targets": [s],
        "transcript_paths": [s],
        "diffs": [{"name": s, "diff": "d"}],
        "untested_surfaces": [s],
        "leading_explanation": {"id": "C1", "statement": s},
        "behaviour_spec": {"description": s, "grader_dir": "/g"},
        "construal_checks": [s],
        "current_prompts": [{"path": s, "sha256": "0" * 64, "outcomes": {"n": 1}}],
        "constraints": [s],
    }
    if key in shaped:
        return shaped[key], s
    if key == "environment_rate":
        return {"rate": 0.5, "n": _NUM}, str(_NUM)
    if key == "subagent_thinking":
        return {"type": "enabled", "budget_tokens": _NUM}, str(_NUM)
    if key == "splits_path":
        # A frozen split whose held-out pattern carries the token: it must
        # surface in the runner's restricted_files, not in the prompt.
        path = tmp_path / "splits.json"
        path.write_text(json.dumps({"schema": 1, "restricted_dirs": [f"{s}/"]}))
        return str(path), s
    if key in ("variant_budget", "iteration", "subagent_max_turns", "variants_requested"):
        return _NUM, str(_NUM)
    if key == "experiment_dir":
        (tmp_path / s).mkdir()
        return str(tmp_path / s), s
    return s, s


def _critic_base(mode: str, tmp_path) -> dict:
    """A minimal valid Critic request for each mode."""
    if mode == "diff_review":
        return {"mode": mode, "diffs": [{"name": "v1", "diff": "d"}], "environment_path": "/env", "mapper_report_path": "/m.md"}
    if mode == "prediction":
        return {"mode": mode, "investigation_log_path": "/log.md", "leading_explanation": {"id": "C1"}, "untested_surfaces": ["s"]}
    return dict(_requests(tmp_path)["critic"][2])


def _cases():
    """(role, key) pairs for every documented key of every role."""
    return [(role, key) for role in sorted(_ROLE_DIRS) for key in sorted(_documented_keys(role))]


def test_documented_key_lists_are_parsed():
    """Guard the parser itself: known keys are found, so an empty parse cannot pass vacuously."""
    assert {"topic", "transcript_source", "scanning_model", "construal_checks", "subagent_route"} <= _documented_keys("analyst")
    assert {"diffs", "investigation_log_path", "evidence_summary"} <= _documented_keys("critic")
    assert {"environment_delta", "elimination_targets", "subagent_max_turns"} <= _documented_keys("designer")
    assert {"current_prompts", "splits_path", "output_dir"} <= _documented_keys("attack_designer")


@pytest.mark.parametrize("role, key", _cases())
def test_every_documented_key_reaches_the_agent(role, key, tmp_path, monkeypatch):
    module, run_fn, data, _ = _requests(tmp_path)[role]
    if role == "critic":
        mode = next((m for m, ks in _critic_mode_keys().items() if key in ks), "candidate_critique")
        data = _critic_base(mode, tmp_path)
    value, token = _sentinel(key, tmp_path)
    got = _capture(monkeypatch, module)
    asyncio.run(run_fn(cwd=None, **{**data, key: value}))
    kwargs = repr({k: v for k, v in got.items() if k != "prompt"})
    assert token in got["prompt"] or token in kwargs, f"{role}: documented key `{key}` never reaches the agent"
