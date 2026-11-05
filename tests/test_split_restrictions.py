"""The frozen dev/held-out split denies held-out paths to the roles that get it.

Offline: no model calls. Covers `scripts/freeze_split.py` (shape, refusal to
overwrite, validation) and `subagents.runner.split_restrictions` through the
same denial helpers the PreToolUse hooks use.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import freeze_split
from subagents import runner


def _freeze(tmp_path: Path) -> Path:
    inv = tmp_path / "investigations" / "ctl"
    rc = freeze_split.main([str(inv), "--setting", "linuxarena",
                            "--dev", "web_scraping,clinical_trial",
                            "--test", "slack_clone,tiktok"])
    assert rc == 0
    return inv / "splits.json"


def test_freeze_writes_patterns_and_refuses_overwrite(tmp_path, capsys):
    path = _freeze(tmp_path)
    split = json.loads(path.read_text())
    assert split["schema"] == 1
    assert split["restricted_dirs"] == ["linuxarena/slack_clone/", "linuxarena/tiktok/", "heldout/"]
    out = json.loads(capsys.readouterr().out)
    assert out["sha256"] and out["test"] == ["slack_clone", "tiktok"]
    # Frozen: a second freeze is refused and the file is untouched.
    before = path.read_text()
    rc = freeze_split.main([str(path.parent), "--setting", "linuxarena", "--dev", "a", "--test", "b"])
    assert rc == 2 and path.read_text() == before


@pytest.mark.parametrize("dev,test", [("a", "a"), ("", "b"), ("a,b", "b"), ("x/y", "b"), ("a*", "b")])
def test_freeze_rejects_bad_splits(dev, test):
    with pytest.raises(ValueError):
        freeze_split.build_split("linuxarena", [s for s in dev.split(",") if s],
                                 [s for s in test.split(",") if s], "heldout")


def test_heldout_paths_are_denied_and_dev_paths_are_not(tmp_path):
    path = _freeze(tmp_path)
    restricted = {**runner.ALWAYS_RESTRICTED, **runner.split_restrictions(str(path))}
    cwd = str(tmp_path)
    settings = tmp_path / "ct_env" / "settings"
    denied = [
        settings / "linuxarena" / "slack_clone" / "tasks" / "t1" / "info.yml",
        settings / "linuxarena" / "tiktok",
        tmp_path / "investigations" / "ctl" / "heldout" / "summary.md",
        tmp_path / "investigations" / "ctl" / "heldout" / "logs" / "c_x" / "run.eval",
        path,  # the split file itself
    ]
    allowed = [
        settings / "linuxarena" / "web_scraping" / "tasks" / "t1" / "info.yml",
        tmp_path / "investigations" / "ctl" / "iter_01" / "artefacts" / "report.md",
        settings / "linuxarena" / "slack_clone_notes.md",  # not the directory
    ]
    for p in denied:
        assert runner._first_denial(restricted, str(p), cwd) is not None, p
    for p in allowed:
        assert runner._first_denial(restricted, str(p), cwd) is None, p
    # Bash is checked best-effort on the command's words.
    assert runner._bash_denial(restricted, f"cat {denied[0]}", cwd) is not None
    assert runner._bash_denial(restricted, f"cat {allowed[0]}", cwd) is None


def test_split_restrictions_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        runner.split_restrictions(str(tmp_path / "missing.json"))
    bad = tmp_path / "splits.json"
    bad.write_text(json.dumps({"schema": 1, "restricted_dirs": ["linuxarena/tiktok"]}))
    with pytest.raises(ValueError):  # no trailing slash: not a directory pattern
        runner.split_restrictions(str(bad))
    bad.write_text(json.dumps({"schema": 2, "restricted_dirs": ["x/"]}))
    with pytest.raises(ValueError):
        runner.split_restrictions(str(bad))
