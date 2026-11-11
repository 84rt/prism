"""Dollar pricing of eval usage and of the Analyst's own model calls (scripts/spend.py),
and the call log that records those calls (subagents/transcript_analyst/call_log)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import spend
from subagents import runner

ROOT = Path(__file__).resolve().parents[1]
HOOK_DIR = ROOT / "subagents" / "transcript_analyst" / "call_log"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches OpenRouter: an unstubbed lookup finds nothing."""
    monkeypatch.setattr(spend, "_fetch_json", lambda url: None)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# Per-token prices as OpenRouter lists them (strings, USD per token)
CATALOGUE = {
    "data": [
        {"id": "deepseek/deepseek-r1-0528", "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
        {"id": "openai/gpt-5.2", "pricing": {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000002"}},
    ]
}
ENDPOINTS = {
    "data": {
        "endpoints": [
            {"provider_name": "DeepInfra", "tag": "deepinfra/fp4", "pricing": {"prompt": "0.0000005", "completion": "0.000002"}},
            {"provider_name": "SiliconFlow", "tag": "siliconflow/fp8", "pricing": {"prompt": "0.0000005", "completion": "0.000003"}},
        ]
    }
}


def stub_fetch(url: str):
    if url == spend.OPENROUTER_MODELS_URL:
        return CATALOGUE
    if url == spend.OPENROUTER_ENDPOINTS_URL.format(slug="deepseek/deepseek-r1-0528"):
        return ENDPOINTS
    return None


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def test_eval_models_priced_by_route_table_and_catalogue():
    """A pinned route uses its provider's rate; an unpinned one, the listed rate; Claude, the table."""
    book = spend.PriceBook(fetch=stub_fetch)
    pinned = book.for_eval_model("openrouter/deepseek/deepseek-r1-0528", "openrouter:siliconflow")
    assert pinned["output"] == pytest.approx(3e-6)
    unpinned = book.for_eval_model("openrouter/deepseek/deepseek-r1-0528", "openrouter:unpinned")
    assert unpinned["output"] == pytest.approx(4e-6)
    # A missing cache price falls back to the input price
    assert unpinned["cache_read"] == pytest.approx(1e-6)
    grader = book.for_eval_model("anthropic/claude-sonnet-5-5", None)
    assert grader["input"] == pytest.approx(2e-6) and grader["cache_write_5m"] == pytest.approx(2.5e-6)
    assert book.for_eval_model("openai/gpt-5.2", None)["input"] == pytest.approx(2e-6)
    assert book.for_eval_model("mystery/model", None) is None


def test_logged_calls_match_catalogue_ids_across_spellings():
    """A dated OpenAI id and a dotted OpenRouter Claude id both find their price."""
    book = spend.PriceBook(fetch=stub_fetch)
    assert book.for_call("api.openai.com", "gpt-5.2-2025-12-11")["input"] == pytest.approx(2e-6)
    assert book.for_call("openrouter.ai", "anthropic/claude-haiku-5.5")["output"] == pytest.approx(0.5e-6)
    assert book.for_call("api.anthropic.com", "claude-haiku-5-5")["input"] == pytest.approx(0.1e-6)
    assert book.for_call("example.com", "gpt-5.2") is None


def test_offline_prices_claude_only():
    book = spend.PriceBook(fetch=stub_fetch, offline=True)
    assert book.for_eval_model("openrouter/deepseek/deepseek-r1-0528", "openrouter:siliconflow") is None
    assert book.for_eval_model("anthropic/claude-opus-5-5", None)["input"] == pytest.approx(4e-6)


def test_price_lists_fetched_once_serve_offline_books_and_failed_fetches():
    slug = "openai/gpt-5.2"
    fetched = []

    def counting_fetch(url):
        fetched.append(url)
        return stub_fetch(url)

    # An online book fetches the list and keeps it on disk
    assert spend.PriceBook(fetch=counting_fetch).catalogue(slug)["input"] == pytest.approx(2e-6)
    assert len(fetched) == 1
    # A second book within the day reads the copy instead of fetching
    assert spend.PriceBook(fetch=counting_fetch).catalogue(slug) is not None
    assert len(fetched) == 1
    # The panel's offline book prices from the copy and never fetches
    assert spend.PriceBook(fetch=counting_fetch, offline=True).catalogue(slug) is not None
    assert len(fetched) == 1
    # Once the copy is a day old a fetch is tried; if it fails the old copy still serves
    path = spend._price_cache_path(spend.OPENROUTER_MODELS_URL)
    os.utime(path, (1, 1))
    assert spend.PriceBook(fetch=lambda url: None).catalogue(slug) is not None


def test_usage_shapes_count_cached_tokens_once():
    """OpenAI shapes include cached tokens in the input total; Anthropic's does not."""
    chat = {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 600}}
    assert spend._tokens_from_call(chat) == {"input": 400, "output": 50, "cache_read": 600, "cache_write_5m": 0, "cache_write_1h": 0}
    responses = {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 100}}
    assert spend._tokens_from_call(responses)["input"] == 900
    anthropic = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 300}
    assert spend._tokens_from_call(anthropic) == {"input": 10, "output": 5, "cache_read": 700, "cache_write_5m": 300, "cache_write_1h": 0}


def test_anthropic_usage_with_thinking_details_keeps_its_cache_tokens():
    """Anthropic usage now carries ``output_tokens_details``, which OpenAI's Responses shape also has."""
    # A Haiku scanner call logged by a real tampering run's Analyst
    scan = {
        "input_tokens": 4, "cache_creation_input_tokens": 429, "cache_read_input_tokens": 2769,
        "cache_creation": {"ephemeral_5m_input_tokens": 429, "ephemeral_1h_input_tokens": 0},
        "output_tokens": 307, "service_tier": "standard", "inference_geo": "global", "speed": "standard",
        "output_tokens_details": {"thinking_tokens": 134},
    }
    assert spend._tokens_from_call(scan) == {"input": 4, "output": 307, "cache_read": 2769, "cache_write_5m": 429, "cache_write_1h": 0}
    # OpenAI's Responses shape with reasoning details still subtracts its cached tokens
    responses = {"input_tokens": 1000, "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 20}}
    assert spend._tokens_from_call(responses)["input"] == 1000


# ---------------------------------------------------------------------------
# collect / render
# ---------------------------------------------------------------------------


def test_collect_prices_evals_and_analyst_calls_into_the_total(tmp_path):
    inv = tmp_path / "investigations" / "demo"
    run = inv / "iterations" / "01" / "artefacts" / "executor" / "runs" / "r1"
    target = "openrouter/deepseek/deepseek-r1-0528"
    write_json(
        run / "status.json",
        {
            "schema": 1,
            "run_id": "r1",
            "pairs": {
                "a": {
                    "model": target,
                    "route": "openrouter:siliconflow",
                    "usage_attempts": [
                        # The target at SiliconFlow's rate, the grader at Sonnet's
                        {target: {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "total_tokens": 2_000_000},
                         "anthropic/claude-sonnet-5-5": {"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000}},
                    ],
                },
                "b": {"model": "mystery/model", "route": "direct", "model_usage": {"mystery/model": {"input_tokens": 5, "total_tokens": 5}}},
            },
        },
    )
    analyst = inv / "iterations" / "01" / "artefacts" / "analyst"
    lines = [
        {"host": "api.anthropic.com", "model": "claude-haiku-5-5", "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}},
        # OpenRouter's own figure wins over any price lookup
        {"host": "openrouter.ai", "model": "openai/gpt-5.2", "usage": {"prompt_tokens": 9, "completion_tokens": 9, "cost": 0.25}},
        {"host": "api.example.com", "model": "unknown-1", "usage": {"input_tokens": 3, "output_tokens": 3}},
    ]
    analyst.mkdir(parents=True)
    (analyst / "model_calls_iter_01_analyst_1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")

    s = spend.collect(str(inv), projects_dir=str(tmp_path / "none"), prices=spend.PriceBook(fetch=stub_fetch))
    e, ac = s["evals"], s["analyst_calls"]
    assert e["cost_by_model"] == {target: 3.5, "anthropic/claude-sonnet-5-5": 2.0}  # 0.5 + 3.0; 2.0
    assert e["total_cost_usd"] == 5.5
    assert e["unpriced_models"] == ["mystery/model"]
    assert e["run_summaries"][0]["cost_usd"] == 5.5
    assert ac["calls"] == 3 and ac["files"] == 1
    assert ac["total_cost_usd"] == pytest.approx(0.6 + 0.25)  # Haiku 0.10 + 0.50, plus OpenRouter's 0.25
    assert ac["unpriced_models"] == ["unknown-1"] and ac["unpriced_calls"] == 1
    assert s["total_cost_usd"] == pytest.approx(5.5 + 0.85)

    text = spend.render(s)
    assert "+ evals $5.50 + analyst scans $0.85)" in text
    assert "under-counts: no price for mystery/model, unknown-1" in text
    assert "analyst scan spend: $0.85 over 3 model call(s)" in text


# ---------------------------------------------------------------------------
# The call log
# ---------------------------------------------------------------------------

# Run inside a fresh Python with the hook on its path: one JSON call, one
# streamed Anthropic-style call, and one async OpenAI-style stream, all served
# by httpx's mock transport
CLIENT_SCRIPT = r'''
import asyncio, json
import LIB as httpx

def handler(request):
    if request.url.path == "/not-a-model":
        return httpx.Response(200, text="hello")
    if request.url.path == "/json":
        body = {"model": "claude-haiku-5-5", "usage": {"input_tokens": 11, "output_tokens": 7}}
        return httpx.Response(200, json=body)
    if request.url.path == "/anthropic-stream":
        events = [
            {"type": "message_start", "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 20, "cache_read_input_tokens": 5, "output_tokens": 1}}},
            {"type": "message_delta", "usage": {"output_tokens": 42}},
        ]
    else:
        events = [{"model": "gpt-5.2", "usage": None}, {"model": "gpt-5.2", "usage": {"prompt_tokens": 30, "completion_tokens": 9}}]
    text = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text.encode())

with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com") as c:
    c.get("/json")
    with c.stream("GET", "/anthropic-stream") as r:
        for _ in r.iter_lines():
            pass
    c.get("/not-a-model")  # plain text: no line

async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.openai.com") as c:
        async with c.stream("GET", "/openai-stream") as r:
            async for _ in r.aiter_bytes():
                pass
asyncio.run(main())
'''


def run_with_hook(tmp_path: Path, log: Path | None, lib: str = "httpx") -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(HOOK_DIR)}
    env.pop(runner.MODEL_CALLS_FILE_ENV, None)
    if log is not None:
        env[runner.MODEL_CALLS_FILE_ENV] = str(log)
    script = tmp_path / "client.py"
    script.write_text(CLIENT_SCRIPT.replace("LIB", lib))
    return subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)


# httpx2 is the renamed copy current Anthropic and OpenAI libraries use
@pytest.mark.parametrize("lib", ["httpx", "httpx2"])
def test_call_log_records_json_and_streamed_calls(tmp_path, lib):
    pytest.importorskip(lib)
    log = tmp_path / "model_calls_x.jsonl"
    proc = run_with_hook(tmp_path, log, lib)
    assert proc.returncode == 0, proc.stderr
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(r["host"], r["model"]) for r in records] == [
        ("api.anthropic.com", "claude-haiku-5-5"),
        ("api.anthropic.com", "claude-haiku-5-5"),
        ("api.openai.com", "gpt-5.2"),
    ]
    assert records[0]["usage"] == {"input_tokens": 11, "output_tokens": 7}
    # The stream's input count from its first event, its final output count from the last
    assert records[1]["usage"] == {"input_tokens": 20, "cache_read_input_tokens": 5, "output_tokens": 42}
    assert records[2]["usage"] == {"prompt_tokens": 30, "completion_tokens": 9}


def test_call_log_is_inert_without_its_variable(tmp_path):
    proc = run_with_hook(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert not list(tmp_path.glob("*.jsonl"))


def test_model_calls_env_sits_beside_the_cost_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv(runner.COST_FILE_ENV, str(tmp_path / "cost_iter_01_analyst_4.json"))
    monkeypatch.setenv("PYTHONPATH", "/existing")
    env = runner._model_calls_env()
    assert env[runner.MODEL_CALLS_FILE_ENV] == str(tmp_path / "model_calls_iter_01_analyst_4.jsonl")
    assert env["PYTHONPATH"].split(os.pathsep) == [str(HOOK_DIR), "/existing"]
    monkeypatch.delenv(runner.COST_FILE_ENV)
    assert runner._model_calls_env() == {}
