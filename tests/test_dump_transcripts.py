"""Tests for `scripts/dump_transcripts.py`.

Unit tests build Inspect message objects directly and need no corpus. The
end-to-end tests run the CLI over real `.eval` logs from the forensics case
dirs and are skipped when that corpus is not present (it is container-local).

Run with the self-contained recipe from the fix briefs' Shared Preamble:

    uv run --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" \
        --with claude-agent-sdk==0.2.165 --with agent-client-protocol==0.12.1 \
        --with python-dotenv --with pytest pytest tests/
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.log import EvalSample, read_eval_log
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, ContentReasoning, ContentText
from inspect_ai.scorer import Score
from inspect_ai.tool import ToolCall

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "dump_transcripts.py"
CORPUS = Path("/workspaces/forensics-cases")

# A small, fast log from each of the three reasoning shapes the corpus holds:
# GPT-5 (encrypted reasoning + summary), DeepSeek-R1 (full reasoning text),
# Claude (no reasoning blocks, tool calls). All are 3–15 messages per sample.
SMALL_LOGS = {
    "gpt5_tools": CORPUS / "case-6-quiz-agent-shutdown/logs",
    "deepseek_reasoning": CORPUS / "case-3-email-agent-cot/logs",
    "claude_tools": CORPUS / "case-5-tool-agent-compliance/logs",
}

needs_corpus = pytest.mark.skipif(not CORPUS.is_dir(), reason="forensics-cases corpus not present")


def _load():
    """Import the script as a module by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("dump_transcripts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # register before exec, as any by-path import should
    spec.loader.exec_module(mod)
    return mod


dt = _load()


# ---------------------------------------------------------------------------
# Unit: helpers
# ---------------------------------------------------------------------------


def test_trunc_keeps_both_ends_and_reports_elision():
    text = "A" * 100 + "B" * 100
    out = dt.trunc(text, 50)
    assert out.startswith("A" * 25)
    assert out.endswith("B" * 25)
    assert "[150 chars elided]" in out


def test_trunc_passthrough_when_short_or_disabled():
    assert dt.trunc("short", 50) == "short"
    assert dt.trunc("x" * 500, 0) == "x" * 500
    assert dt.trunc(None, 50) == ""


def test_safe_name_sanitises_ids():
    assert dt.safe_name("blackmail_none-none_replacement") == "blackmail_none-none_replacement"
    assert dt.safe_name("a/b c:d") == "a_b_c_d"
    assert dt.safe_name(7) == "7"


# ---------------------------------------------------------------------------
# Unit: rendering
# ---------------------------------------------------------------------------


def test_render_reasoning_uses_summary_when_redacted():
    block = ContentReasoning(reasoning="ENCRYPTED-BLOB", summary="the readable summary", redacted=True)
    out = dt.render_reasoning(block, 2500)
    assert "<reasoning-summary>" in out
    assert "the readable summary" in out
    assert "ENCRYPTED-BLOB" not in out


def test_render_reasoning_uses_raw_when_readable():
    block = ContentReasoning(reasoning="I think therefore I am", summary=None, redacted=False)
    out = dt.render_reasoning(block, 2500)
    assert out.startswith("<reasoning>")
    assert "I think therefore I am" in out


def test_render_reasoning_placeholder_when_nothing_readable():
    block = ContentReasoning(reasoning="", summary=None, redacted=True)
    assert "[reasoning redacted by provider; no summary]" in dt.render_reasoning(block, 2500)


def test_render_message_assistant_with_tool_call():
    msg = ChatMessageAssistant(
        content=[ContentText(text="Let me look.")],
        tool_calls=[ToolCall(id="c1", function="bash", arguments={"cmd": "ls -la"})],
    )
    out = dt.render_message(3, msg, 2500)
    assert out.splitlines()[0] == "----- [3] ASSISTANT -----"
    assert "Let me look." in out
    assert '>>> TOOL CALL bash({"cmd": "ls -la"})' in out


def test_render_message_tool_result_names_function_and_error():
    msg = ChatMessageTool(content="file not found", tool_call_id="c1", function="bash", error={"type": "unknown", "message": "boom"})
    out = dt.render_message(4, msg, 2500)
    assert out.splitlines()[0] == "----- [4] TOOL (bash) -----"
    assert "[tool error:" in out
    assert "file not found" in out


def _sample_with_score() -> EvalSample:
    return EvalSample(
        id="s1",
        epoch=2,
        input="hello",
        target="",
        messages=[ChatMessageAssistant(content="hi there")],
        scores={"scorer": Score(value=1, explanation="matched the rubric")},
    )


def test_write_sample_omits_scores_by_default(tmp_path):
    """The score blackout: a default dump must carry no scorer output at all."""
    log = SimpleNamespace(eval=SimpleNamespace(task="t", model="m"))
    out = tmp_path / "d.txt"
    with out.open("w") as fh:
        dt.write_sample(_sample_with_score(), log, Path("x.eval"), fh, 2500, with_scores=False)
    text = out.read_text()
    assert "### SAMPLE s1 epoch 2" in text
    assert "### SCORE" not in text
    assert "matched the rubric" not in text
    assert "hi there" in text


def test_write_sample_includes_scores_on_request(tmp_path):
    log = SimpleNamespace(eval=SimpleNamespace(task="t", model="m"))
    out = tmp_path / "d.txt"
    with out.open("w") as fh:
        dt.write_sample(_sample_with_score(), log, Path("x.eval"), fh, 2500, with_scores=True)
    text = out.read_text()
    assert "### SCORE scorer: 1" in text
    assert "### EXPLANATION scorer: matched the rubric" in text


# ---------------------------------------------------------------------------
# Unit: log discovery / layout
# ---------------------------------------------------------------------------


def test_find_logs_mirrors_relative_path_for_dir_input_and_stem_for_file_input(tmp_path):
    (tmp_path / "logs" / "control").mkdir(parents=True)
    (tmp_path / "logs" / "control" / "a.eval").write_bytes(b"")
    (tmp_path / "ref.eval").write_bytes(b"")
    pairs = dt.find_logs([str(tmp_path / "logs"), str(tmp_path / "ref.eval")])
    rel = {log.name: rel_dir for log, rel_dir in pairs}
    assert rel["a.eval"] == Path("control/a")
    assert rel["ref.eval"] == Path("ref")


def test_find_logs_dedups_repeated_inputs(tmp_path):
    f = tmp_path / "ref.eval"
    f.write_bytes(b"")
    assert len(dt.find_logs([str(f), str(f)])) == 1


# ---------------------------------------------------------------------------
# End-to-end over the corpus
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_cli_omits_scores_by_default_on_a_synthetic_log(tmp_path):
    """No corpus needed: the dump of a scored log carries no score unless asked.

    Dumps are what every sub-agent reads instead of the `.eval`, so the
    default must be score-free; `--with-scores` is the explicit opt-in.
    """
    from conftest import LEAK_TOKENS, write_fixture_log

    log = write_fixture_log(str(tmp_path / "logs" / "c_3f9a" / "f.eval"), epochs=2)
    out = tmp_path / "dumps"
    assert dt.main([str(tmp_path / "logs"), "--out", str(out)]) == 0
    texts = [p.read_text() for p in out.rglob("*.txt")]
    assert len(texts) == 2 and all("LEAKY_ANSWER" in t for t in texts)  # the transcript itself is there
    for t in texts:
        assert "### SCORE" not in t
        for tok in LEAK_TOKENS:
            assert tok not in t

    scored = tmp_path / "scored"
    assert dt.main([log, "--out", str(scored), "--with-scores"]) == 0
    assert all("### SCORE" in p.read_text() and "0.9871" in p.read_text() for p in scored.rglob("*.txt"))


@needs_corpus
@pytest.mark.parametrize("name", sorted(SMALL_LOGS))
def test_cli_dump_matches_log_contents(name, tmp_path):
    """Count in == count out, and each dump reflects the sample it came from."""
    log_dir = SMALL_LOGS[name]
    log_path = sorted(log_dir.glob("*.eval"))[0]
    out = tmp_path / "dumps"
    res = _run([str(log_path), "--out", str(out)])
    assert res.returncode == 0, res.stderr

    log = read_eval_log(str(log_path))
    files = sorted((out / log_path.stem).glob("*.txt"))
    assert len(files) == len(log.samples)

    for sample in log.samples:
        text = (out / log_path.stem / f"{dt.safe_name(sample.id)}_epoch_{sample.epoch}.txt").read_text()
        # One rendered header per message, and no scorer output.
        assert text.count("----- [") == len(sample.messages)
        assert "### SCORE" not in text
        # The final assistant text must survive rendering (modulo truncation).
        last = next(m for m in reversed(sample.messages) if m.role == "assistant")
        snippet = (last.text or "").strip()[:80]
        if snippet:
            assert snippet in text


@needs_corpus
def test_cli_with_scores_flag_emits_scores(tmp_path):
    log_path = sorted(SMALL_LOGS["claude_tools"].glob("*.eval"))[0]
    out = tmp_path / "dumps"
    res = _run([str(log_path), "--out", str(out), "--with-scores", "--epoch", "1"])
    assert res.returncode == 0, res.stderr
    texts = [p.read_text() for p in (out / log_path.stem).glob("*.txt")]
    assert texts and all("### SCORE" in t for t in texts)


@needs_corpus
def test_cli_mirrors_condition_layout_and_survives_copied_logs(tmp_path):
    """Two copies of one log under different conditions must both be dumped.

    One copy is a real file, the other sits behind a directory symlink — the
    opaque-label staging pattern (`staged/condition_A -> logs/v3`) — and both
    must land under their own mirrored path rather than overwrite each other.
    """
    src = sorted(SMALL_LOGS["deepseek_reasoning"].glob("*.eval"))[0]
    logs = tmp_path / "logs"
    (logs / "control").mkdir(parents=True)
    (logs / "control" / src.name).write_bytes(src.read_bytes())
    (logs / "condition_A").symlink_to(logs / "control", target_is_directory=True)
    out = tmp_path / "transcripts"
    res = _run([str(logs), "--out", str(out)])
    assert res.returncode == 0, res.stderr
    n = len(read_eval_log(str(src)).samples)
    assert len(list((out / "control" / src.stem).glob("*.txt"))) == n
    assert len(list((out / "condition_A" / src.stem).glob("*.txt"))) == n


@needs_corpus
def test_cli_refuses_to_merge_colliding_file_inputs(tmp_path):
    """Same stem, different files, both given as file inputs -> error, not a mixed dump."""
    src = sorted(SMALL_LOGS["deepseek_reasoning"].glob("*.eval"))[0]
    a = tmp_path / "a" / src.name
    b = tmp_path / "b" / src.name
    for p in (a, b):
        p.parent.mkdir()
        p.write_bytes(src.read_bytes())
    res = _run([str(a), str(b), "--out", str(tmp_path / "out")])
    assert res.returncode == 1
    assert "already written by" in res.stderr


@needs_corpus
def test_cli_reasoning_rendering_per_family(tmp_path):
    ds = sorted(SMALL_LOGS["deepseek_reasoning"].glob("*.eval"))[0]
    gpt = sorted(SMALL_LOGS["gpt5_tools"].glob("*.eval"))[0]
    out = tmp_path / "out"
    assert _run([str(ds), str(gpt), "--out", str(out), "--epoch", "1"]).returncode == 0
    ds_text = "".join(p.read_text() for p in (out / ds.stem).glob("*.txt"))
    gpt_text = "".join(p.read_text() for p in (out / gpt.stem).glob("*.txt"))
    assert "<reasoning>" in ds_text  # DeepSeek exposes the raw trace
    assert "<reasoning-summary>" in gpt_text  # GPT-5's is encrypted; summary only
