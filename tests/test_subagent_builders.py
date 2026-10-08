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
