"""Provider ranking and pin construction in `scripts/openrouter_endpoints.py`.

The pin chosen here becomes the `-M provider=…` argument on every eval
command, and a wrong one either hard-fails every sample (bad slug with
fallbacks off) or silently confounds provider with condition. The selection
runs against a canned catalogue — `_get_json` is stubbed — so the ranking
rules are pinned without the network.
"""

from __future__ import annotations

import pytest

import openrouter_endpoints as oe

MODEL = "openrouter/meta-llama/llama-3.3-70b-instruct:free"
BASE = "meta-llama/llama-3.3-70b-instruct"


def _endpoint(name, quant, *, uptime=99.9, prompt="0.0000002", params=("temperature",), max_completion=8192):
    """One record shaped like `GET /models/<id>/endpoints` → `data.endpoints[]`."""
    return {
        "provider_name": name,
        "quantization": quant,
        "uptime_last_30m": uptime,
        "pricing": {"prompt": prompt, "completion": prompt},
        "supported_parameters": list(params),
        "max_completion_tokens": max_completion,
        "context_length": 131072,
    }


# The author (Meta) serves fp8, which fixes native precision at fp8: the bf16
# endpoint is an upcast and must be excluded; "Mystery Host" has no slug in the
# providers list and cannot be pinned; DeepInfra is native-precision and
# cheapest; Novita is quantized below native.
CATALOGUE = {
    f"{oe.OPENROUTER_API}/models/{BASE}/endpoints": {
        "data": {
            "endpoints": [
                _endpoint("Together", "bf16", prompt="0.0000009"),
                _endpoint("Novita", "fp4", prompt="0.0000001"),
                _endpoint("DeepInfra", "fp8", prompt="0.00000015", uptime=99.0),
                _endpoint("Mystery Host", "fp8", prompt="0.00000001"),
                _endpoint("Meta", "fp8", prompt="0.0000005", params=("temperature", "top_p")),
            ]
        }
    },
    f"{oe.OPENROUTER_API}/providers": {
        "data": [
            {"name": "Together", "slug": "together"},
            {"name": "Novita", "slug": "novitaai"},
            {"name": "DeepInfra", "slug": "deepinfra"},
            {"name": "Meta", "slug": "meta"},
        ]
    },
}


@pytest.fixture
def catalogue(monkeypatch):
    """Serve `CATALOGUE` in place of the network; returns the URLs fetched."""
    fetched: list[str] = []

    def fake_get_json(url, timeout):
        fetched.append(url)
        return CATALOGUE[url]

    monkeypatch.setattr(oe, "_get_json", fake_get_json)
    return fetched


def test_fidelity_pins_the_author_and_excludes_upcasts_and_unpinnables(catalogue):
    report = oe.select(MODEL, "fidelity", timeout=1)
    assert report["model"] == f"openrouter/{BASE}"  # prefix and :free variant stripped
    assert report["native_precision_basis"] == "author's own endpoint"
    assert report["recommended"]["provider_slug"] == "meta" and report["recommended"]["is_author"]

    order = [(e["provider_name"], e["excluded"]) for e in report["endpoints"]]
    # Author first; then closest-to-native (DeepInfra fp8) before quantized (Novita fp4);
    # excluded records last, kept visible.
    assert order[:3] == [("Meta", False), ("DeepInfra", False), ("Novita", False)]
    excluded = {e["provider_name"]: e["exclusion_reason"] for e in report["endpoints"] if e["excluded"]}
    assert excluded["Together"].startswith("upcast: serves bf16")
    assert excluded["Mystery Host"] == "no resolvable provider slug to pin to"

    # The pin is strict and verifiable.
    assert report["recommended_model_args"] == {
        "provider": {"order": ["meta"], "allow_fallbacks": False, "require_parameters": True},
        "default_headers": {"X-OpenRouter-Metadata": "enabled"},
    }
    # The endpoints disagree on honoured parameters: surfaced as a warning.
    assert any("disagree on which sampling parameters" in w for w in report["warnings"])


def test_cost_picks_cheapest_pinnable_never_an_upcast_or_unpinnable(catalogue):
    report = oe.select(MODEL, "cost", timeout=1)
    # Mystery Host is cheaper still, but has no slug; Together is an upcast.
    assert report["recommended"]["provider_slug"] == "novitaai"
    assert report["recommended_model_args"]["provider"]["order"] == ["novitaai"]
    # Below native precision: the caveat is carried.
    assert any("below the inferred native precision" in w for w in report["warnings"])


def test_no_pinnable_endpoint_falls_back_to_unpinned_with_a_warning(monkeypatch):
    payload = {
        f"{oe.OPENROUTER_API}/models/{BASE}/endpoints": {"data": {"endpoints": [_endpoint("Mystery Host", "fp8")]}},
        f"{oe.OPENROUTER_API}/providers": {"data": []},
    }
    monkeypatch.setattr(oe, "_get_json", lambda url, timeout: payload[url])
    report = oe.select(MODEL, "fidelity", timeout=1)
    assert report["recommended"] is None
    assert report["recommended_model_args"] == oe.unpinned_model_args() == {"default_headers": {"X-OpenRouter-Metadata": "enabled"}}
    assert any("No pinnable endpoint" in w for w in report["warnings"])


def test_duplicate_slugs_keep_the_best_ranked_record():
    records = oe.normalise_endpoints(
        [_endpoint("DeepInfra", "fp8", uptime=90.0), _endpoint("DeepInfra", "fp8", uptime=99.9)],
        "meta-llama",
        {"deepinfra": "deepinfra"},
    )
    ranked = oe.rank_endpoints(records, native_rank=None, prefer="fidelity")
    assert [r["uptime_last_30m"] for r in ranked] == [99.9, 90.0]
    assert [r["duplicate_of_slug"] for r in ranked] == [None, "deepinfra"]


@pytest.mark.parametrize("bad", ["llama-3.3-70b", "openrouter/llama"])
def test_model_id_must_name_an_author(bad):
    with pytest.raises(RuntimeError):
        oe.resolve_model_id(bad)


# --- `--list`: the live catalogue in place of a written Critic shortlist ---

MODELS = {
    f"{oe.OPENROUTER_API}/models": {
        "data": [
            {"id": "google/gemini-2.5-pro", "created": 1740000000, "context_length": 1048576,
             "pricing": {"prompt": "0.00000125", "completion": "0.00001"}},
            {"id": "google/gemini-3-pro", "created": 1780000000, "context_length": 1048576,
             "pricing": {"prompt": "0.000002", "completion": "0.000012"}},
            {"id": "openai/gpt-5", "created": 1760000000, "context_length": 400000,
             "pricing": {"prompt": "0.00000125", "completion": "0.00001"}},
            {"id": "google/gemma-3-27b-it", "created": 1750000000, "context_length": 131072, "pricing": {}},
        ]
    }
}


def test_list_filters_by_prefix_newest_first(monkeypatch, capsys):
    fetched: list[str] = []
    monkeypatch.setattr(oe, "_get_json", lambda url, timeout: fetched.append(url) or MODELS[url])
    models = oe.list_models("openrouter/Google/Gemini", 5.0)
    assert [m["id"] for m in models] == ["google/gemini-3-pro", "google/gemini-2.5-pro"]
    assert models[0]["prompt_per_m"] == 2.0 and models[0]["completion_per_m"] == 12.0
    assert fetched == [f"{oe.OPENROUTER_API}/models"]  # one public, key-free request

    oe.main(["--list", "google/gem"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("google/gemini-3-pro") and "$2.00/M in" in out[0] and "1048576 ctx" in out[0]
    gemma = next(ln for ln in out if ln.startswith("google/gemma-3-27b-it"))
    assert "? in" in gemma and len(out) == 3


def test_list_reports_no_match_and_rejects_ambiguous_use(monkeypatch, capsys):
    monkeypatch.setattr(oe, "_get_json", lambda url, timeout: MODELS[url])
    oe.main(["--list", "x-ai/"])
    assert "no catalogue model id starts with" in capsys.readouterr().out
    for argv in ([], ["openai/gpt-5", "--list", "openai/"]):
        with pytest.raises(SystemExit):
            oe.main(argv)
