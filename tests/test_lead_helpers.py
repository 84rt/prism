"""Tests for the lead agents' helper scripts and the shipped permission settings.

`check_keys.py` replaces reading `.env` by hand (the smoke-run lead read it via
`.venv/bin/python` + `dotenv`); `new_ids.py` replaces shell loops for minting
opaque condition ids. The settings tests pin the autonomy rule: no `ask` rules
ship (an `ask` stalls an unattended run on a prompt), and Bash routes to
`.env` are denied.
"""

from __future__ import annotations

import json
import re

import pytest

import check_keys as ck
import new_ids as ni
from conftest import REPO_ROOT

SECRET = "sk-ant-SHOULD-NEVER-BE-PRINTED"


# --- check_keys.py -------------------------------------------------------------


def _env(tmp_path, text: str) -> str:
    """Write a `.env` and return its path."""
    p = tmp_path / ".env"
    p.write_text(text)
    return str(p)


@pytest.mark.parametrize("as_json", [False, True])
def test_check_keys_reports_presence_never_values(tmp_path, monkeypatch, capsys, as_json):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    env = _env(tmp_path, f"# comment\nexport ANTHROPIC_API_KEY={SECRET}\nOPENAI_API_KEY=\"\"\nEXTRA_KEY='{SECRET}'\n")
    args = ["--env-file", env, "--key", "EXTRA_KEY", "--key", "ABSENT_KEY"] + (["--json"] if as_json else [])
    assert ck.main(args) == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    if as_json:
        keys = json.loads(out)["keys"]
        assert keys["ANTHROPIC_API_KEY"] == {"status": "present", "sources": [".env"]}
        assert keys["OPENROUTER_API_KEY"] == {"status": "present", "sources": ["environment"]}
        assert keys["OPENAI_API_KEY"]["status"] == "empty"
        assert keys["EXTRA_KEY"]["status"] == "present"
        assert keys["ABSENT_KEY"]["status"] == "missing"
    else:
        assert re.search(r"OPENAI_API_KEY\s+empty", out)
        assert re.search(r"ABSENT_KEY\s+missing", out)


def test_check_keys_finds_nearest_env_upward(tmp_path, monkeypatch):
    _env(tmp_path, "ANTHROPIC_API_KEY=x\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert ck.find_env_file(str(sub)) == str(tmp_path / ".env")


def test_check_keys_exits_zero_without_env(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ck, "find_env_file", lambda start: None)
    assert ck.main([]) == 0
    assert ".env: none found" in capsys.readouterr().out


# --- new_ids.py ----------------------------------------------------------------


def test_new_ids_are_opaque_distinct_and_avoid_the_map(tmp_path, monkeypatch, capsys):
    label_map = tmp_path / "CONDITION_LABEL_MAP_iter1.json"
    label_map.write_text(json.dumps({"conditions": {"c_0000": {"name": "x"}}, "notes": "staged as iter_c_0001_x"}))
    # Force the first draws onto the taken ids, so avoidance is exercised.
    draws = iter([0, 1, 0, 2, 3])
    monkeypatch.setattr(ni.secrets, "randbelow", lambda n: next(draws))
    assert ni.main(["2", "--map", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["c_0002", "c_0003"]


def test_new_ids_default_and_shape(capsys):
    assert ni.main([]) == 0
    out = capsys.readouterr().out.split()
    assert len(out) == 1 and re.fullmatch(r"c_[0-9a-f]{4}", out[0])


def test_new_ids_usage_errors(tmp_path, capsys):
    assert ni.main(["0"]) == 2
    assert ni.main(["1", "--map", str(tmp_path / "missing.json")]) == 2


# --- Shipped settings ----------------------------------------------------------


def _permissions() -> dict:
    return json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())["permissions"]


def test_settings_ship_no_ask_rules():
    # An `ask` rule prompts even in auto mode: a silent stall for an unattended run.
    assert not _permissions().get("ask")


def test_settings_deny_find_exec_and_bash_env_routes():
    deny = set(_permissions()["deny"])
    assert {"Bash(find * -exec *)", "Bash(find * -execdir *)"} <= deny
    # The smoke-run bypass: `.venv/bin/python -c "from dotenv import load_dotenv ..."` —
    # closed by the dotenv patterns, which match through any interpreter path.
    assert {"Bash(*python*load_dotenv*)", "Bash(*python*dotenv_values*)", "Bash(* .env)", "Bash(*/.env)"} <= deny
    # The venv's interpreter itself stays usable: leads legitimately use it to verify
    # an environment copy (e.g. regenerate each arm's prompts and diff them).
    assert not any(rule.startswith("Bash(.venv/bin/python") or "/.venv/bin/python" in rule for rule in deny)
    # The sanctioned replacement is allowed in its documented form.
    allow = set(_permissions()["allow"])
    assert "Bash(uv run --no-project python scripts/check_keys.py)" in allow
    # The sub-agent launch line carries `python-dotenv` and must stay allowed:
    # the deny patterns are on the function names, not the package.
    launch = "Bash(uv run --with claude-agent-sdk==0.2.165 --with python-dotenv python subagents/*)"
    assert launch in allow and "Bash(*dotenv*)" not in deny
