"""Tests for `scripts/check_blinding.py` (deficiency D32).

The defect: R02's opaque `condition_A…G` labels pointed at directories named
after their conditions, so the Analyst knew the mapping. A condition's real name
also reaches the Analyst through the header's `task_file`, `tags` and
`metadata`, so each channel gets its own leaky fixture. Corpus anchors: R02's
iteration-1 Analyst request (blocks on the paths and the headers) and its
iteration-2 staged symlinks (staging hides the path but not the symlink target
or the headers, so the fallback still blocks).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import check_blinding as cb
from conftest import case_path, write_fixture_log

R02 = "case-2-email-agent/investigations/blackmail_gpt41"

# Two opaque ids and the real names a label map records for them.
VAR, CTL = "c_3f9a", "c_8b21"
REAL = {VAR: "leverage_prohibition", CTL: "concurrent_control"}


def _map(tmp_path: Path, conditions: dict | None = None) -> str:
    """Write a current-shape label map and return its path.

    Parameters
    ----------
    tmp_path : Path
        Investigation directory.
    conditions : dict | None
        Id → real name (defaults to `REAL`).

    Returns
    -------
    str
    """
    conditions = REAL if conditions is None else conditions
    data = {
        "iteration": 1,
        "conditions": {cid: {"name": name} for cid, name in conditions.items()},
        "analyst_labels": {"iter1_analyst_1": {"condition_K": VAR, "condition_B": CTL}},
    }
    p = tmp_path / "CONDITION_LABEL_MAP_iter1.json"
    p.write_text(json.dumps(data))
    return str(p)


def _arm(tmp_path: Path, dirname: str, cond: str, **header) -> str:
    """Write one condition's log directory with a crafted header.

    Parameters
    ----------
    tmp_path : Path
        Investigation directory.
    dirname : str
        The condition's directory name under `iterations/01_round1/logs/`.
    cond : str
        The condition name the runner would have recorded.
    **header
        Overrides for `write_fixture_log` (`task_file`, `tags`, `metadata`,
        `task_args`, `task`).

    Returns
    -------
    str
        The log directory.
    """
    d = tmp_path / "iterations" / "01_round1" / "logs" / dirname
    kw = {
        "task_file": f"{cond}/task.py",
        "tags": ["exp:iter_01", f"cond:{cond}"],
        "metadata": {"condition": cond, "model": "mockllm/model", "route": "direct"},
    }
    kw.update(header)
    write_fixture_log(str(d / "2026-10-08T00-00-00-00-00_fixture-task_AbC123.eval"), epochs=1, **kw)
    return str(d)


def _source(tmp_path: Path, mapping: dict[str, str], whole_request: bool = False) -> str:
    """Write a transcript-source JSON (bare mapping or whole Analyst request).

    Parameters
    ----------
    tmp_path : Path
        Where to write it.
    mapping : dict[str, str]
        Label → directory.
    whole_request : bool
        Wrap the mapping in an Analyst request.

    Returns
    -------
    str
    """
    data = {"topic": "t", "transcript_source": mapping} if whole_request else mapping
    p = tmp_path / "source.json"
    p.write_text(json.dumps(data))
    return str(p)


def _opaque_layout(tmp_path: Path) -> dict[str, str]:
    """The compliant layout: every arm named by its id, the control's included."""
    return {"condition_K": _arm(tmp_path, VAR, VAR), "condition_B": _arm(tmp_path, CTL, CTL)}


def _run(capsys, label_map: str, source: str) -> tuple[int, str]:
    """Run the CLI in-process and return (exit code, stdout)."""
    code = cb.main([label_map, source])
    return code, capsys.readouterr().out


# --- Clean layout ---


@pytest.mark.parametrize("whole_request", [False, True])
def test_opaque_layout_passes(tmp_path, capsys, whole_request):
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, _opaque_layout(tmp_path), whole_request))
    assert code == 0, out
    assert "BLOCK" not in out and "WARN" not in out
    assert "clean (0 warning(s))" in out


# --- One leaky fixture per channel ---


def test_path_channel_blocks(tmp_path, capsys):
    # R02's defect: the directory is named after the condition.
    src = {"condition_K": _arm(tmp_path, "leverage_prohibition", VAR), "condition_B": _arm(tmp_path, CTL, CTL)}
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "[path] condition_K: token 'leverage_prohibition'" in out


def test_task_file_channel_blocks(tmp_path, capsys):
    # The variant's environment copy named descriptively (case 3's `env_v6_inert_control/task.py`).
    src = {
        "condition_K": _arm(tmp_path, VAR, VAR, task_file="env_leverage_prohibition/task.py"),
        "condition_B": _arm(tmp_path, CTL, CTL),
    }
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "[task_file] condition_K: token 'leverage_prohibition'" in out


def test_tags_channel_blocks_on_role_word(tmp_path, capsys):
    src = {
        "condition_K": _arm(tmp_path, VAR, VAR),
        "condition_B": _arm(tmp_path, CTL, CTL, tags=["exp:iter_01", "cond:control"]),
    }
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "[tags] condition_B: token 'control'" in out


def test_metadata_channel_blocks(tmp_path, capsys):
    src = {
        "condition_K": _arm(tmp_path, VAR, VAR, metadata={"condition": VAR, "arm": "treatment"}),
        "condition_B": _arm(tmp_path, CTL, CTL),
    }
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "[metadata] condition_K: token 'treatment'" in out


def test_label_channel_blocks(tmp_path, capsys):
    arms = _opaque_layout(tmp_path)
    src = {"control": arms["condition_B"], "condition_K": arms["condition_K"]}
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "[label] control: token 'control'" in out


def test_symlink_staging_exposes_target(tmp_path, capsys):
    # Neutral staging names do not hide a descriptive symlink target.
    real = _arm(tmp_path, "baseline", CTL)
    staged = tmp_path / "staged"
    staged.mkdir()
    os.symlink(real, staged / "set_A")
    var = _arm(tmp_path, VAR, VAR)
    os.symlink(var, staged / "set_B")
    src = {"condition_A": str(staged / "set_A"), "condition_B": str(staged / "set_B")}
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "[symlink_target] condition_A: token 'baseline'" in out
    assert "[path]" not in out  # the staged names themselves are clean


def test_unmapped_condition_name_blocks(tmp_path, capsys):
    # A descriptive name the map never recorded cannot be vouched for.
    src = {"condition_K": _arm(tmp_path, VAR, "goal_framing_strong", task_file=f"{VAR}/task.py"), "condition_B": _arm(tmp_path, CTL, CTL)}
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "goal_framing_strong" in out and "not one of the label map's opaque ids" in out


def test_file_symlink_staging_exposes_target(tmp_path, capsys):
    # Staging can link files as well as directories; the target leaks the same way.
    real = Path(_arm(tmp_path, "leverage_prohibition", VAR))
    staged = tmp_path / "staged" / "set_A"
    staged.mkdir(parents=True)
    for f in real.iterdir():
        os.symlink(f, staged / f.name)
    src = {"condition_A": str(staged), "condition_B": _arm(tmp_path, CTL, CTL)}
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 1
    assert "[symlink_target] condition_A: token 'leverage_prohibition'" in out


def test_filename_channel_blocks(tmp_path, capsys):
    # A file under the source named after the condition (a stray copy, a note).
    arms = _opaque_layout(tmp_path)
    (Path(arms["condition_K"]) / "notes_leverage_prohibition.txt").write_text("x")
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, arms))
    assert code == 1
    assert "[filename] condition_K: token 'leverage_prohibition'" in out


def test_real_name_in_request_blocks(tmp_path, capsys):
    data = {"topic": "Whether the leverage-prohibition edit matters", "transcript_source": _opaque_layout(tmp_path)}
    p = tmp_path / "req.json"
    p.write_text(json.dumps(data))
    code, out = _run(capsys, _map(tmp_path), str(p))
    assert code == 1
    assert "[request]" in out and "request field topic" in out


def test_single_source_is_checked_from_the_investigation_dir(tmp_path, capsys):
    # The 1c shape: one pool, alone. Nothing to compare against, so the
    # iteration directory and the runner's tags are judged on their own.
    d = tmp_path / "iterations" / "00_resample" / "logs" / "c_0e4d"
    write_fixture_log(str(d / "f.eval"), epochs=1, task_file="c_0e4d/task.py",
                      tags=["exp:mf_x_resample", "cond:c_0e4d"], metadata={"condition": "c_0e4d"})
    code, out = _run(capsys, _map(tmp_path, {"c_0e4d": "frozen pool"}), _source(tmp_path, {"condition_A": str(d)}))
    assert code == 1
    assert "[path] condition_A: token 'resample'" in out
    assert "[tags] condition_A: token 'resample'" in out


def test_legacy_map_and_name_matching(tmp_path, capsys):
    # R06's legacy shape; separator, case, camelCase and inflection tolerance.
    legacy = tmp_path / "CONDITION_LABEL_MAP_iter1.json"
    legacy.write_text(json.dumps({"A": {"condition_Y": "BASELINE(frozen 1c pool)", "condition_Q": "pull_weak"}}))
    src = {
        "condition_K": _arm(tmp_path, "c_0001", "c_0001", task_file="Pull-Weak/task.py"),
        "condition_B": _arm(tmp_path, "c_0002", "c_0002", metadata={"condition": "c_0002", "arm": "TreatmentArm"}),
        "condition_C": _arm(tmp_path, "resampled_pool", "c_0003"),
    }
    code, out = _run(capsys, str(legacy), _source(tmp_path, src))
    assert code == 1
    assert "[task_file] condition_K: token 'pull_weak'" in out
    assert "[metadata] condition_B: token 'treatment'" in out
    assert "[path] condition_C: token 'resample'" in out


def test_real_name_boundaries(tmp_path, capsys):
    # `v1` must not fire inside `v10`.
    src = {"condition_K": _arm(tmp_path, VAR, VAR, task_file="v10/task.py"), "condition_B": _arm(tmp_path, CTL, CTL, task_file="v10/task.py")}
    code, out = _run(capsys, _map(tmp_path, {VAR: "v1", CTL: "v2"}), _source(tmp_path, src))
    assert code == 0, out


def test_unreadable_headers_fail_closed(tmp_path, capsys):
    arms = _opaque_layout(tmp_path)
    for f in Path(arms["condition_K"]).glob("*.eval"):
        f.write_bytes(b"not a zip")
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, arms))
    assert code == 1
    assert "could not read header" in out and "[header] condition_K" in out


def test_empty_source_warns(tmp_path, capsys):
    arms = _opaque_layout(tmp_path)
    empty = tmp_path / "iterations" / "01_round1" / "logs" / "c_51d0"
    empty.mkdir(parents=True)
    arms["condition_Z"] = str(empty)
    code, out = _run(capsys, _map(tmp_path, dict(REAL, c_51d0="extra")), _source(tmp_path, arms))
    assert code == 0
    assert "condition_Z: no .eval files" in out


# --- Warnings that never block ---


def test_real_name_in_shared_task_args_warns(tmp_path, capsys):
    # Identical on every arm, so not a difference — but the Analyst can read it.
    args = {"prompt": "leverage_prohibition.txt"}
    src = {"condition_K": _arm(tmp_path, VAR, VAR, task_args=args), "condition_B": _arm(tmp_path, CTL, CTL, task_args=args)}
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 0
    assert "contains real name 'leverage_prohibition'" in out


def test_task_args_difference_warns_but_passes(tmp_path, capsys):
    src = {
        "condition_K": _arm(tmp_path, VAR, VAR, task_args={"goal_type": "explicit", "n": 1}),
        "condition_B": _arm(tmp_path, CTL, CTL, task_args={"goal_type": "none", "n": 1}),
    }
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 0, out
    assert "WARN   task_args differ between conditions on: goal_type." in out
    assert "file-edit variants" in out and "caveats" in out


def test_role_word_common_to_every_condition_only_warns(tmp_path, capsys):
    # A control-research eval's own task name cannot reveal the mapping.
    src = {
        "condition_K": _arm(tmp_path, VAR, VAR, task="control_arena_apps"),
        "condition_B": _arm(tmp_path, CTL, CTL, task="control_arena_apps"),
    }
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 0, out
    assert "identical in every condition" in out


def test_task_file_shape_difference_warns(tmp_path, capsys):
    # The control on the unprefixed task file is the odd one out.
    src = {"condition_K": _arm(tmp_path, VAR, VAR), "condition_B": _arm(tmp_path, CTL, CTL, task_file="task.py")}
    code, out = _run(capsys, _map(tmp_path), _source(tmp_path, src))
    assert code == 0
    assert "task_file paths differ in shape" in out


def test_mixed_iteration_sources_warn(tmp_path, capsys):
    arms = _opaque_layout(tmp_path)
    frozen = tmp_path / "iterations" / "00_round0" / "logs" / "c_0e4d"
    write_fixture_log(str(frozen / "f.eval"), epochs=1, task_file="c_0e4d/task.py",
                      tags=["exp:iter_00", "cond:c_0e4d"], metadata={"condition": "c_0e4d"})
    conds = dict(REAL, c_0e4d="frozen baseline")
    arms["condition_Q"] = str(frozen)
    code, out = _run(capsys, _map(tmp_path, conds), _source(tmp_path, arms))
    # Every name is opaque, so nothing blocks; but the frozen pool sits in a
    # different iteration directory, which the Analyst can see.
    assert code == 0, out
    assert "differently shaped directories" in out


# --- Usage errors ---


def test_usage_errors_exit_2(tmp_path, capsys):
    assert cb.main([]) == 2
    assert cb.main([str(tmp_path / "missing.json"), str(tmp_path / "also_missing.json")]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cb.main([_map(tmp_path), str(bad)]) == 2
    src = _source(tmp_path, {"condition_A": str(tmp_path / "nope")})
    assert cb.main([_map(tmp_path), src]) == 2


# --- The real R02 layouts ---


def _r02_map(tmp_path: Path, names: list[str]) -> str:
    """A label map for R02 with opaque ids assigned to its real names."""
    conds = {f"c_{i:04x}": n for i, n in enumerate(names)}
    p = tmp_path / "CONDITION_LABEL_MAP_iter1.json"
    p.write_text(json.dumps({"iteration": 1, "conditions": conds}))
    return str(p)


def test_r02_iteration1_layout_blocks(tmp_path, capsys):
    request = case_path(f"{R02}/artefacts/requests/analyst_iter1.json")
    sources = json.loads(request.read_text())["transcript_source"]
    names = [os.path.basename(p) for p in sources.values()]
    code, out = _run(capsys, _r02_map(tmp_path, names), str(request))
    assert code == 1
    # Channel 1 (the directory) for every condition, and the role word on the control.
    for name in names:
        assert f"token '{name}'" in out
    assert "[path] condition_C: token 'control'" in out
    # Channels 3-4 inside the .eval headers carry the same names.
    assert "[tags]" in out and "[metadata]" in out


def test_r02_iteration2_staging_still_blocks(tmp_path, capsys):
    # The documented fallback: neutral symlink names hide channel 1, but not
    # the symlink target and not the names recorded inside each .eval.
    request = case_path(f"{R02}/artefacts/requests/analyst_iter2.json")
    mapping = json.loads(case_path(f"{R02}/artefacts/requests/iter2_label_mapping.json").read_text())
    code, out = _run(capsys, _r02_map(tmp_path, list(mapping)), str(request))
    assert code == 1
    assert "[path]" not in out
    assert "[symlink_target] set_E: token 'control'" in out
    assert "[tags]" in out and "[metadata]" in out


# --- The Analyst can never read the map ---


@pytest.mark.parametrize("name,blocked", [
    ("CONDITION_LABEL_MAP_iter1.json", True),
    ("CONDITION_LABEL_MAP_iter0.json", True),
    ("CONDITION_LABEL_MAP_iter03.json", True),
    ("CONDITION_LABEL_MAP_iter42.json", True),
    ("CONDITION_LABEL_MAP_iter137.json", True),  # past the old 0–99 enumeration
    ("CONDITION_LABEL_MAP.json", True),
    ("NOT_A_CONDITION_LABEL_MAP_iter1.json", False),  # basename match, not substring
    ("iterations/01_round1/logs/c_3f9a/x.eval", False),
])
def test_analyst_read_hook_denies_label_map(name, blocked):
    # Exercise the runner's real read hook with the Analyst's RESTRICTED_FILES,
    # so a matcher change that stops covering the map fails here.
    import asyncio

    from subagents.runner import _build_hooks
    from subagents.transcript_analyst.agent import RESTRICTED_FILES

    hooks = _build_hooks("analyst", RESTRICTED_FILES, None, False)
    read = next(m for m in hooks["PreToolUse"] if m.matcher == "Read").hooks[0]
    path = f"/ws/investigations/inv/{name}"
    result = asyncio.run(read({"tool_input": {"file_path": path}}, "t", {}))
    assert (result.get("decision") == "block") is blocked
