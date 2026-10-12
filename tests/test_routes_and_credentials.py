"""Offline tests for WP2: non-Claude route ergonomics (D27) and credential
exhaustion as its own failure class (D05's suspension trigger).

Network calls are mocked; the credential strings are the archive's own,
quoted verbatim from the six runs' stderr logs and investigation logs (see
`subagents/runner.py` CREDENTIAL_PATTERNS for provenance), and re-read from
the corpus where a saved stderr log holds them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

import execute_evals as ee
import launch_subagent as ls
from conftest import LEAK_TOKENS, case_path, make_request, substitute_fake_eval

runner = pytest.importorskip("subagents.runner")

# --- Mined strings (verbatim from the archive) --------------------------------

# R05 executor stderr (and R04 executor_stderr.txt), the full line the CLI printed.
R05_STDERR = (
    "Agent error: Experiment executor: process error: Claude Code returned an error "
    "result: API Error: 400 You have reached your specified workspace API usage limits. "
    "You will regain access on 2026-10-01 at 00:00 UTC. (exit code: 1)"
)
# R02 log L620: the OpenRouter workspace budget, prefixed as an auth failure.
R02_OPENROUTER = "Failed to authenticate. API Error: 403 Workspace lifetime budget of $150.00 exceeded."
# R01 log L981.
R01_OPENROUTER = 'HTTP 403 — "Workspace lifetime budget of $150.00 exceeded"'

CREDENTIAL_STRINGS = [R05_STDERR, R02_OPENROUTER, R01_OPENROUTER]

# Failures that must NOT be read as credential exhaustion.
NOT_CREDENTIALS = [
    "API Error: 429 rate_limit_error: Number of request tokens has exceeded your per-minute rate limit",
    "Failed to authenticate. API Error: 401 invalid x-api-key",
    "RuntimeError: sample 402 errored after 3 retries",
    "sha256 e57acc10f53c55bd65c8462f1402834edd30e4f41203988a0cac5aff6122a614",
    "No OPENROUTER_API_KEY defined in the environment",
]


# --- Part 1: context window for non-Claude models on OpenRouter -------------


@pytest.fixture
def catalogue(monkeypatch):
    """Mock OpenRouter's endpoints API; returns the list of URLs fetched.

    The payload mimics `GET /api/v1/models/<id>/endpoints`: two endpoints with
    different windows, so the chosen one is distinguishable from the first
    (with n=2 the upper median is the larger of the two).
    """
    runner._openrouter_context_length.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv(runner.MAX_CONTEXT_ENV, raising=False)
    calls: list[str] = []
    state = {"payload": {"data": {"endpoints": [{"context_length": 131072}, {"context_length": 1048576}]}}}

    def fake_get_json(url, timeout):
        calls.append(url)
        if isinstance(state["payload"], Exception):
            raise state["payload"]
        return state["payload"]

    monkeypatch.setattr(runner._load_openrouter_endpoints(), "_get_json", fake_get_json)
    yield calls, state
    runner._openrouter_context_length.cache_clear()


def test_non_claude_openrouter_route_sets_window_from_catalogue(catalogue) -> None:
    calls, _ = catalogue
    env = runner._resolve_route_env("openrouter", "google/gemini-3-pro")
    assert env["ANTHROPIC_BASE_URL"] == runner.OPENROUTER_ANTHROPIC_BASE_URL
    assert env[runner.WINDOW_ENFORCEMENT_ENV] == "1"
    assert env[runner.MAX_CONTEXT_ENV] == "1048576"  # n=2: the larger window is "half"
    assert calls == ["https://openrouter.ai/api/v1/models/google/gemini-3-pro/endpoints"]
    # Cached per process: a second invocation does not refetch.
    runner._resolve_route_env("openrouter", "google/gemini-3-pro")
    assert len(calls) == 1


# Catalogue shapes from the public endpoints API (survey 2026-10-09). Frontier
# models report one window everywhere; open-weight models have low outliers.
LLAMA_33_70B = [131072] * 6 + [128000, 65536, 32768, 16384, 12288]  # 11 endpoints
DEEPSEEK_V32 = [163840] * 7 + [131072, 131072, 128000, 65536, 64000, 32768]  # 13


@pytest.mark.parametrize(
    "lengths, expected",
    [
        ([1048576], 1048576),  # n=1
        ([131072, 1048576], 1048576),  # n=2: one endpoint is half
        ([400000] * 4, 400000),  # frontier: identical on every endpoint
        (LLAMA_33_70B, 131072),
        (DEEPSEEK_V32, 163840),
        # Majority small: the rare *large* host is ignored too.
        ([32768, 32768, 32768, 131072], 32768),
        ([], None),
    ],
)
def test_window_is_largest_that_at_least_half_the_endpoints_serve(lengths, expected) -> None:
    orx = runner._load_openrouter_endpoints()
    assert orx.majority_context_length(lengths) == expected
    # Order-independent: the catalogue lists endpoints in no particular order.
    assert orx.majority_context_length(list(reversed(lengths))) == expected


def test_small_window_outlier_does_not_shrink_the_env_window(catalogue) -> None:
    _, state = catalogue
    state["payload"] = {"data": {"endpoints": [{"context_length": n} for n in LLAMA_33_70B]}}
    env = runner._resolve_route_env("openrouter", "meta-llama/llama-3.3-70b-instruct")
    assert env[runner.MAX_CONTEXT_ENV] == "131072"


def test_claude_models_and_direct_route_are_untouched(catalogue) -> None:
    calls, _ = catalogue
    env = runner._resolve_route_env("openrouter", "anthropic/claude-opus-4-8")
    assert runner.WINDOW_ENFORCEMENT_ENV not in env and runner.MAX_CONTEXT_ENV not in env
    assert runner._resolve_route_env(None, "google/gemini-3-pro") == {}
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        RuntimeError("Network error fetching …: [Errno -3] Temporary failure in name resolution"),
        RuntimeError("HTTP 404 fetching …: Not Found"),  # unlisted model
        {"data": {"endpoints": []}},  # listed with no usable window
    ],
)
def test_offline_or_unlisted_leaves_cli_default_and_logs_once(catalogue, capsys, payload) -> None:
    _, state = catalogue
    state["payload"] = payload
    env = runner._resolve_route_env("openrouter", "x-ai/grok-4")
    # The invocation proceeds: auth env present, window enforcement at default.
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-test"
    assert runner.WINDOW_ENFORCEMENT_ENV not in env and runner.MAX_CONTEXT_ENV not in env
    runner._resolve_route_env("openrouter", "x-ai/grok-4")
    err = capsys.readouterr().err
    assert err.count("[route]") == 1 and "default window enforcement" in err


def test_user_exported_window_wins_without_a_fetch(catalogue, monkeypatch) -> None:
    calls, _ = catalogue
    monkeypatch.setenv(runner.MAX_CONTEXT_ENV, "400000")
    env = runner._resolve_route_env("openrouter", "openai/gpt-5")
    assert env[runner.MAX_CONTEXT_ENV] == "400000" and env[runner.WINDOW_ENFORCEMENT_ENV] == "1"
    assert calls == []


# --- Part 3: credential exhaustion classifiers ---------------------------------


def test_classifier_pattern_lists_are_identical() -> None:
    """runner.py and execute_evals.py must recognise the same failures."""
    assert tuple(ee._CREDENTIAL_PATTERNS) == runner.CREDENTIAL_PATTERNS
    assert ee._CREDENTIAL_STATUS_RE.pattern == runner.CREDENTIAL_STATUS_RE.pattern


@pytest.mark.parametrize("text", CREDENTIAL_STRINGS + ["APIStatusError: status_code=402 body=..."])
def test_runner_classifies_mined_strings(text: str) -> None:
    assert runner.classify_failure(text) == "credentials_exhausted"


@pytest.mark.parametrize("text", NOT_CREDENTIALS)
def test_runner_does_not_overmatch(text: str) -> None:
    assert runner.classify_failure(text) is None


@pytest.mark.parametrize(
    "relative",
    [
        "case-5-tool-agent-compliance/investigations/agentharm_disinfo_compliance/artefacts/executor/iter2_stderr.log",
        "case-4-spec-tests-conflict/investigations/test_conflict_special_casing/iterations/01_round1/executor_stderr.txt",
    ],
)
def test_saved_corpus_stderr_is_classified(relative: str) -> None:
    text = case_path(relative).read_text()
    assert runner.classify_failure(text) == "credentials_exhausted"
    assert ee.is_credential_exhaustion(text)


# Inspect wraps the provider's message in its SDK's exception text; the
# provider message is the same string, so the same pattern catches it.
INSPECT_ANTHROPIC = (
    "anthropic.BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'You have reached your specified workspace API "
    "usage limits. You will regain access on 2026-10-01 at 00:00 UTC.'}}"
)
INSPECT_OPENROUTER_BUDGET = (
    "openai.PermissionDeniedError: Error code: 403 - {'error': {'message': "
    "'Workspace lifetime budget of $150.00 exceeded.', 'code': 403}}"
)
INSPECT_OPENROUTER_402 = (
    "openai.APIStatusError: Error code: 402 - {'error': {'message': 'This request requires "
    "more credits, or fewer max_tokens.', 'code': 402}}"
)


@pytest.mark.parametrize("text", CREDENTIAL_STRINGS + [INSPECT_ANTHROPIC, INSPECT_OPENROUTER_BUDGET, INSPECT_OPENROUTER_402])
def test_execute_evals_keeps_credentials_structural(text: str) -> None:
    assert ee.classify_error(1, text) == "structural"
    assert ee.is_credential_exhaustion(text)


def test_routing_and_other_classes_unchanged() -> None:
    assert ee.classify_error(1, "No allowed providers are available for the selected model") == "routing"
    assert ee.classify_error(1, "RuntimeError: sample 402 errored") == "transient"
    assert not ee.is_credential_exhaustion("RuntimeError: sample 402 errored")
    for text in NOT_CREDENTIALS:
        assert not ee.is_credential_exhaustion(text), text


def test_credentials_failure_end_to_end_is_tagged_and_score_free(tmp_path, monkeypatch) -> None:
    """A target-model cap mid-run: structural, `[credentials]`, scrubbed tail."""
    fake = tmp_path / "fake_capped_eval.py"
    fake.write_text(
        "import sys\n"
        "print('Steps:   0/10   0% Samples:   0/2 accuracy: 0.7317')\n"
        f"print({INSPECT_ANTHROPIC!r})\n"
        "sys.exit(1)\n"
    )
    path, _ = make_request(tmp_path, conditions={"baseline": {"task": "task.py", "args": {}}})
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    substitute_fake_eval(run_dir, str(fake))
    ee.run_worker(run_dir)

    data = ee.Status.load(run_dir).data
    pf = data["preflight"]["pairs"]["baseline__openai-gpt-5"]
    assert pf["outcome"] == "excluded-structural"  # no new taxonomy category
    assert pf["evidence"]["output_tail"].startswith("[structural] [credentials] ")
    report = ee.render_report(data)
    assert "**[credentials] Credentials exhausted**" in report
    assert "- **Reason**: `structural`" in report
    for tok in LEAK_TOKENS:
        assert tok not in report, f"scorer token {tok!r} leaked into the report"


def test_mid_run_cap_is_tagged_at_sample_level_and_score_free(tmp_path) -> None:
    """A cap reached mid-eval under --no-fail-on-error: exit 0, samples error.

    The fake eval writes a scored log whose last 2 of 4 samples carry Inspect's
    wrapper around the archive's Anthropic workspace-cap message, plus one
    unrelated sample error in a second pair, so the count is per-message.
    """
    fake = tmp_path / "fake_midrun_cap_eval.py"
    fake.write_text(
        "import sys, os\n"
        f"sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})\n"
        "from conftest import write_fixture_log\n"
        "args = sys.argv[1:]\n"
        "log_dir = args[args.index('--log-dir') + 1]\n"
        "capped = 'treatment' in log_dir\n"
        "write_fixture_log(os.path.join(log_dir, 'f.eval'), epochs=4, with_error=True,\n"
        f"                  errored=2 if capped else 1, error_message={INSPECT_ANTHROPIC!r} if capped else 'boom')\n"
        "print('Steps:  10/10 100% Samples:   4/4 accuracy: 0.73')\n"
    )
    path, _ = make_request(tmp_path, overrides={"skip_preflight": True, "epochs": 4})
    run_dir, problems = ee.prepare_run(path)
    assert not problems
    substitute_fake_eval(run_dir, str(fake))
    assert ee.run_worker(run_dir) == 0

    # --- status.json: the capped pair carries the count; the other does not --
    data = ee.Status.load(run_dir).data
    capped = data["pairs"]["treatment__openai-gpt-5"]
    plain = data["pairs"]["baseline__openai-gpt-5"]
    assert capped["state"] == plain["state"] == "sample-level-errors"
    assert capped["error_class"] is None  # the process itself succeeded
    assert (capped["sample_errors"], capped["credential_sample_errors"]) == (2, 2)
    assert (plain["sample_errors"], plain["credential_sample_errors"]) == (1, 0)
    assert "[credentials] 2 sample error(s)" in ee.render_status(data)

    # --- Draft report: tagged under Sample-level, with the count -------------
    report = ee.render_report(data)
    assert "**[credentials] Credentials exhausted** (2 of the 2 errored samples)" in report
    assert report.count("Credentials exhausted") == 1
    assert "treatment / openai/gpt-5 2 of 4 (2 [credentials])" in report

    # --- Blackout: no scorer output, and no raw error text, anywhere readable -
    status_text = Path(run_dir, "status.json").read_text()
    for text, where in ((report, "report"), (status_text, "status.json"), (ee.render_status(data), "status")):
        for tok in LEAK_TOKENS:
            assert tok not in text, f"scorer token {tok!r} leaked into {where}"
        assert "workspace API usage limits" not in text  # only the count is kept


# --- Part 3: runner surfaces the class; launcher records it ---------------------


class _FakeOptions:
    def __init__(self, **kw):
        self.max_turns = kw.get("max_turns")


def _error_result(text: str):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="success", duration_ms=5, duration_api_ms=4, is_error=True, num_turns=1,
        session_id="s", stop_reason=None, total_cost_usd=0.0, usage={}, result=text,
    )


def test_runner_process_error_becomes_credentials_exhausted(tmp_path, monkeypatch) -> None:
    from claude_agent_sdk import ProcessError

    async def fake_query(prompt, options):
        yield _error_result("API Error: 400 You have reached your specified workspace API usage limits.")
        raise ProcessError(
            "Claude Code returned an error result: API Error: 400 You have reached your "
            "specified workspace API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.",
            exit_code=1,
        )

    cost = tmp_path / "cost.json"
    monkeypatch.setenv(runner.COST_FILE_ENV, str(cost))
    monkeypatch.setattr(runner, "ClaudeAgentOptions", _FakeOptions)
    monkeypatch.setattr(runner, "query", fake_query)
    with pytest.raises(RuntimeError, match=r"credentials_exhausted \(route=direct, model=claude-opus-4-8\)"):
        asyncio.run(runner.run_agent("p", "s", ["Bash"], [], "Experiment executor"))
    record = json.loads(Path(cost).read_text())
    assert record["failure_class"] == "credentials_exhausted" and record["route"] == "direct"


def test_runner_generic_process_error_uses_the_error_result_text(tmp_path, monkeypatch) -> None:
    """Older SDKs raise a bare "exit code 1" ProcessError; the text is in the result."""
    from claude_agent_sdk import ProcessError

    async def fake_query(prompt, options):
        yield _error_result("API Error: 400 You have reached your specified workspace API usage limits.")
        raise ProcessError("Command failed with exit code 1", exit_code=1, stderr="Check stderr output for details")

    monkeypatch.setenv(runner.COST_FILE_ENV, str(tmp_path / "cost.json"))
    monkeypatch.setattr(runner, "ClaudeAgentOptions", _FakeOptions)
    monkeypatch.setattr(runner, "query", fake_query)
    with pytest.raises(RuntimeError, match=r"credentials_exhausted \(route=direct"):
        asyncio.run(runner.run_agent("p", "s", ["Bash"], [], "Mapper"))


def test_runner_error_result_without_exception_is_not_a_report(tmp_path, monkeypatch) -> None:
    """The provider's text arriving as `result` must not be returned as the report."""
    async def fake_query(prompt, options):
        yield _error_result(R02_OPENROUTER)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv(runner.COST_FILE_ENV, str(tmp_path / "cost.json"))
    monkeypatch.setattr(runner, "ClaudeAgentOptions", _FakeOptions)
    monkeypatch.setattr(runner, "query", fake_query)
    with pytest.raises(RuntimeError, match=r"credentials_exhausted \(route=openrouter"):
        asyncio.run(runner.run_agent("p", "s", ["Bash"], [], "Analyst", model="claude-opus-4-8", route="openrouter"))


def test_runner_sidecar_without_result_message(tmp_path, monkeypatch) -> None:
    """A cap hit before any result still leaves a tagged sidecar."""
    path = tmp_path / "cost.json"
    monkeypatch.setenv(runner.COST_FILE_ENV, str(path))
    runner._write_cost_sidecar("Critic", "google/gemini-3-pro", "openrouter", None, failure_class="credentials_exhausted")
    record = json.loads(Path(path).read_text())
    assert record["failure_class"] == "credentials_exhausted"
    assert record["total_cost_usd"] is None and record["is_error"] is True


def _stub_capped(tmp_path, *, sidecar: bool):
    """A stand-in sub-agent that dies the way the runner reports a cap."""
    script = tmp_path / "stub_capped.py"
    script.write_text(
        "import os, sys, json\n"
        "p = os.environ.get('PRISM_COST_FILE')\n"
        f"if p and {sidecar}:\n"
        "    json.dump({'prism_cost': 1, 'failure_class': 'credentials_exhausted', 'route': 'openrouter'}, open(p, 'w'))\n"
        "print('Agent error: Analyst: credentials_exhausted (route=direct, model=claude-opus-4-8): ...', file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    return [sys.executable, str(script)]


@pytest.mark.parametrize("sidecar, route", [(True, "openrouter"), (False, "direct")])
def test_launcher_receipt_records_failure_class(tmp_path, sidecar: bool, route: str) -> None:
    """Sidecar preferred; the stderr marker is the fallback."""
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"topic": "t"}))
    r = ls.launch("analyst", str(req), str(tmp_path / "inv"), "iter2", entry=_stub_capped(tmp_path, sidecar=sidecar))
    final = ls.wait_for(r["receipt"], timeout=60, interval=0.2)
    assert final["exit_code"] == 2
    assert final["failure_class"] == "credentials_exhausted" and final["failure_route"] == route
    assert f"credentials_exhausted (route={route})" in ls.describe(final)


def test_list_unfinished_gives_relaunch_commands_and_skips_superseded(tmp_path) -> None:
    inv = tmp_path / "inv"
    art = inv / "iterations" / "02" / "artefacts"
    req_a = tmp_path / "a.json"
    req_a.write_text(json.dumps({"topic": "t", "subagent_route": "direct"}))
    req_b = tmp_path / "b.json"
    req_b.write_text(json.dumps({"topic": "other"}))
    ok = [sys.executable, "-c", "print('### Summary')"]

    # A failed, then recovered by a route substitution of the same work.
    r1 = ls.launch("analyst", str(req_a), str(inv), "iter2", str(art), entry=_stub_capped(tmp_path, sidecar=True))
    ls.wait_for(r1["receipt"], timeout=60, interval=0.2)
    req_a2 = tmp_path / "a2.json"
    req_a2.write_text(json.dumps({"topic": "t", "subagent_route": "openrouter", "subagent_model": "claude-opus-4-8"}))
    r2 = ls.launch("analyst", str(req_a2), str(inv), "iter2", str(art), entry=ok)
    ls.wait_for(r2["receipt"], timeout=60, interval=0.2)
    # B failed and was never recovered.
    r3 = ls.launch("analyst", str(req_b), str(inv), "iter2", str(art), entry=_stub_capped(tmp_path, sidecar=True))
    ls.wait_for(r3["receipt"], timeout=60, interval=0.2)

    entries = ls.unfinished(str(inv))
    assert [e["receipt"]["receipt"] for e in entries] == [r3["receipt"]]
    cmd = entries[0]["relaunch"]
    assert cmd[:6] == ["uv", "run", "python", "scripts/launch_subagent.py", "launch", "analyst"]
    assert cmd[6] == r3["request"]  # the persisted copy, not the scratch original
    assert cmd[cmd.index("--investigation") + 1] == str(inv)
    assert cmd[cmd.index("--artefacts-dir") + 1] == str(art)
    assert cmd[cmd.index("--tag") + 1] == "iter2"

    # The CLI prints the same, shell-quoted.
    assert ls.main(["list", str(inv), "--unfinished"]) == 0

    # An unreadable request is never treated as superseded by another.
    for r in (r1, r2):
        Path(r["request"]).write_text("{not json")
    assert [e["receipt"]["receipt"] for e in ls.unfinished(str(inv))] == [r1["receipt"], r3["receipt"]]
