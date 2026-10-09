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
import os

import pytest

from subagents import runner
from subagents.environment_explorer import agent as explorer
from subagents.environment_mapper import agent as mapper
from subagents.experiment_executor import agent as executor
from subagents.redteam_critic import agent as critic
from subagents.transcript_analyst import agent as analyst
from subagents.variant_designer import agent as designer

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
    }


ROLES = ["explorer", "mapper", "designer", "executor", "analyst", "critic"]


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
REASK_ROLES = ["explorer", "mapper", "designer", "analyst", "critic"]

# The role-specific rule for re-deriving a genuinely missing section, which
# must reach the prompt (it is what stops a re-ask from redoing the work).
REASK_NOTES = {
    "explorer": "Re-read the environment only for such a section.",
    "mapper": "Re-read the environment only for such a section.",
    "designer": "do not re-design",
    "analyst": "never re-run a scan",
    "critic": "discriminating signature",
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
    "redteam_critic": ("## Red-Teaming Critic", None),
}


@pytest.mark.parametrize("agent_dir", sorted(_INVOCATION_SECTIONS))
def test_every_data_key_read_by_each_builder_is_documented_in_its_section(agent_dir):
    """A key the builder reads must be documented in that role's own section
    of the invocation reference; only the shared ``subagent_*`` fields may
    rely on the General Pattern. (The Analyst's own test in
    `test_analyst_prompt_builder.py` is the original of this invariant.)"""
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "subagents", agent_dir, "agent.py")).read()
    keys = set(re.findall(r"data(?:\.get\(|\[)['\"](\w+)['\"]", src))
    doc = open(os.path.join(root, ".claude", "docs", "investigator", "subagent_invocation.md")).read()
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
