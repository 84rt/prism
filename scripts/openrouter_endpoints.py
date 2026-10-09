"""OpenRouter endpoint enumeration and provider selection for the Prism scaffold.

The Orchestrator uses this to choose an upstream provider to pin before an
investigation begins. Pinning matters because OpenRouter resolves one model id
to one of several upstream providers, and those providers are not
interchangeable measurement instruments: for the same model id they differ in
quantization, in which sampling parameters they honour, and in maximum context
and completion length. If conditions land on different upstreams, upstream
provider is confounded with condition.

A provider slug cannot be guessed. `provider.order` takes slugs, the endpoints
API reports display names, and the mapping is not mechanical ("VoyageAI by
MongoDB" -> "voyageai"). A wrong slug combined with `allow_fallbacks: false`
does not degrade gracefully — it hard-fails every sample. So selection is always
made against live data, never from memory.

Both OpenRouter endpoints used here are public and need no API key, so this runs
before any key check.

Usage:
    uv run --no-project python scripts/openrouter_endpoints.py <model-id> \
        [--prefer fidelity|cost] [--timeout SECONDS]

Example:
    uv run --no-project python scripts/openrouter_endpoints.py \
        meta-llama/llama-3.3-70b-instruct

Output: a JSON report to stdout with the ranked endpoints and a recommended pin,
including a `recommended_model_args` block ready to render as `-M` arguments.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

OPENROUTER_API = "https://openrouter.ai/api/v1"

# ---------------------------------------------------------------------------
# Precision ranking
# ---------------------------------------------------------------------------

# Higher rank == less lossy. Used for two distinct purposes: ordering endpoints
# by fidelity, and detecting *upcasts* (an endpoint serving above the model's
# native precision). An upcast costs more and is no more faithful than native —
# it is a different numerical path from the reference weights, not a better one
# — so upcasts are excluded outright rather than merely deprioritised.
_PRECISION_RANK: dict[str, int] = {
    "fp32": 60,
    "bf16": 50,
    "fp16": 50,
    "fp8": 40,
    "fp6": 35,
    "int8": 30,
    "fp4": 20,
    "int4": 20,
}


def precision_rank(quantization: str | None) -> int | None:
    """Map an OpenRouter quantization label to a fidelity rank.

    Parameters
    ----------
    quantization : str | None
        The endpoint's `quantization` field (e.g. "fp8"), or None/"unknown".

    Returns
    -------
    int | None
        The rank, or None when the label is missing or unrecognised. None is
        deliberately distinct from a low rank: unknown precision is a gap in
        our knowledge, not evidence of heavy quantization.
    """
    if not quantization:
        return None
    return _PRECISION_RANK.get(quantization.strip().lower())


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_json(url: str, timeout: float) -> Any:
    """GET a URL and parse the response as JSON.

    Parameters
    ----------
    url : str
        Fully-qualified URL to fetch.
    timeout : float
        Socket timeout in seconds.

    Returns
    -------
    Any
        The decoded JSON body.

    Raises
    ------
    RuntimeError
        If the request fails or the body is not valid JSON.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} fetching {url}: {exc.reason}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Network error fetching {url}: {exc}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {url}: {exc}")


# ---------------------------------------------------------------------------
# Slug resolution
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    """Reduce a provider or author name to a comparison key.

    Lowercases and strips everything that isn't alphanumeric, so that
    "Meta-Llama", "meta_llama" and "Meta Llama" all collapse to "metallama".

    Parameters
    ----------
    name : str
        A display name, slug, or model-id author segment.

    Returns
    -------
    str
        The normalised comparison key.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def build_slug_map(providers: list[dict[str, Any]]) -> dict[str, str]:
    """Map normalised provider display names to their canonical slugs.

    `provider.order` in a routing request takes slugs, but the endpoints API
    reports display names, and the two do not derive from one another
    mechanically ("VoyageAI by MongoDB" -> "voyageai"). This join is the only
    reliable way to get a slug we can safely pin to.

    Parameters
    ----------
    providers : list[dict]
        The `data` array from `/api/v1/providers`.

    Returns
    -------
    dict[str, str]
        Normalised display name -> slug. Slugs are also indexed under their own
        normalised form so an endpoint already reporting a slug still resolves.
    """
    slug_map: dict[str, str] = {}
    for provider in providers:
        slug = provider.get("slug")
        name = provider.get("name")
        if not slug:
            continue
        slug_map[_normalise(slug)] = slug
        if name:
            slug_map[_normalise(name)] = slug
    return slug_map


# Model-id author namespaces that differ from the training organisation's
# provider name, where prefix matching alone cannot connect them. Deliberately
# partial: a miss is not a correctness failure — selection falls through to
# precision-based ranking, which still yields a valid pin — it only means the
# author's endpoint isn't preferred and native precision stays undetermined.
# Extend as cases are encountered rather than trying to be exhaustive.
_AUTHOR_PROVIDER_ALIASES: dict[str, str] = {
    "qwen": "alibaba",
    "zai": "zhipu",
}


def is_author_endpoint(author: str, provider_name: str, slug: str | None) -> bool:
    """Decide whether an endpoint is served by the model's own author.

    The author's own endpoint is the closest available thing to the reference
    implementation — correct chat template, special tokens, and intended
    sampling defaults — so it is the first preference in fidelity mode.

    Matching is prefix-based in both directions because model-id authors and
    provider names agree only loosely: the model `meta-llama/llama-3.3-70b`
    has author key "metallama" while the provider is "Meta" ("meta"). A
    four-character floor keeps short keys from colliding accidentally.

    Parameters
    ----------
    author : str
        The author segment of the model id (e.g. "meta-llama").
    provider_name : str
        The endpoint's `provider_name` display value.
    slug : str | None
        The endpoint's resolved slug, if known.

    Returns
    -------
    bool
        True if the endpoint appears to be the author's own.
    """
    author_key = _normalise(author)
    # Resolve the author namespace to a provider name where the two diverge
    # (e.g. models published under "qwen" are served by "alibaba").
    author_keys = {author_key}
    if author_key in _AUTHOR_PROVIDER_ALIASES:
        author_keys.add(_normalise(_AUTHOR_PROVIDER_ALIASES[author_key]))

    candidates = [_normalise(provider_name)]
    if slug:
        candidates.append(_normalise(slug))

    for candidate, author_key in (
        (c, a) for c in candidates for a in author_keys
    ):
        if not candidate or not author_key:
            continue
        if candidate == author_key:
            return True
        shorter = min(len(candidate), len(author_key))
        if shorter >= 4 and (
            candidate.startswith(author_key) or author_key.startswith(candidate)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Endpoint normalisation and ranking
# ---------------------------------------------------------------------------


def _price_per_million(pricing: dict[str, Any] | None, key: str) -> float | None:
    """Convert an OpenRouter per-token price string to price per million tokens."""
    if not pricing:
        return None
    raw = pricing.get(key)
    if raw is None:
        return None
    try:
        return round(float(raw) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def normalise_endpoints(
    endpoints: list[dict[str, Any]], author: str, slug_map: dict[str, str]
) -> list[dict[str, Any]]:
    """Reduce raw endpoint records to the fields selection actually depends on.

    Parameters
    ----------
    endpoints : list[dict]
        The `data.endpoints` array from the model endpoints API.
    author : str
        The author segment of the model id.
    slug_map : dict[str, str]
        Normalised-name -> slug map from `build_slug_map`.

    Returns
    -------
    list[dict]
        One record per endpoint, carrying the routing slug plus the fields that
        make endpoints non-interchangeable (precision, honoured sampling
        parameters, context and completion limits, price, uptime).
    """
    records: list[dict[str, Any]] = []
    for endpoint in endpoints:
        provider_name = endpoint.get("provider_name") or endpoint.get("name") or ""
        slug = slug_map.get(_normalise(provider_name))
        quantization = endpoint.get("quantization")

        records.append(
            {
                "provider_name": provider_name,
                "provider_slug": slug,
                "tag": endpoint.get("tag"),
                "quantization": quantization,
                "precision_rank": precision_rank(quantization),
                "is_author": is_author_endpoint(author, provider_name, slug),
                "context_length": endpoint.get("context_length"),
                "max_completion_tokens": endpoint.get("max_completion_tokens"),
                "supported_parameters": endpoint.get("supported_parameters") or [],
                "prompt_usd_per_mtok": _price_per_million(
                    endpoint.get("pricing"), "prompt"
                ),
                "completion_usd_per_mtok": _price_per_million(
                    endpoint.get("pricing"), "completion"
                ),
                "uptime_last_30m": endpoint.get("uptime_last_30m"),
                "status": endpoint.get("status"),
            }
        )
    return records


def infer_native_precision(
    records: list[dict[str, Any]],
) -> tuple[int | None, str]:
    """Infer the model's native serving precision and say how confident we are.

    The endpoints API does not report native precision, so it has to be
    inferred. The author's own endpoint is the best available proxy: an author
    has no incentive to degrade their own flagship, so the precision they serve
    at is effectively the reference. Without an author endpoint there is no
    sound basis for the inference, and we say so rather than guessing — an
    unfounded "native" would silently exclude endpoints as upcasts.

    Parameters
    ----------
    records : list[dict]
        Normalised endpoint records.

    Three cases are distinguished, because they warrant different confidence:
    the author serves and reports a precision; the author serves but reports
    none (typical of closed-weight models, where quantization is not a
    meaningful axis at all); or the author does not serve here.

    Parameters
    ----------
    records : list[dict]
        Normalised endpoint records.

    Returns
    -------
    tuple[int | None, str]
        The inferred native precision rank (None if undetermined), and a
        human-readable description of the basis for it.
    """
    author_records = [r for r in records if r["is_author"]]
    author_ranks = [
        r["precision_rank"] for r in author_records if r["precision_rank"] is not None
    ]
    if author_ranks:
        return max(author_ranks), "author's own endpoint"
    if author_records:
        return None, (
            "not reported by the author's own endpoint (typical of closed-weight "
            "models, where serving precision is not a published axis)"
        )
    return None, "undetermined (author does not serve this model on OpenRouter)"


def rank_endpoints(
    records: list[dict[str, Any]], native_rank: int | None, prefer: str
) -> list[dict[str, Any]]:
    """Order endpoints by the scaffold's selection rule and mark exclusions.

    Fidelity mode orders by: author's own endpoint, then closest-to-native
    precision without exceeding it, then uptime, then price. Cost mode orders
    by price while keeping the upcast exclusion — pinning to the cheapest
    endpoint is exactly as confound-free as pinning to the author's, because
    the confound is removed by pinning at all, not by which endpoint is pinned.

    Parameters
    ----------
    records : list[dict]
        Normalised endpoint records.
    native_rank : int | None
        Inferred native precision rank, or None if undetermined.
    prefer : str
        Either "fidelity" or "cost".

    Returns
    -------
    list[dict]
        The records, each annotated with `excluded` and `exclusion_reason`, and
        sorted best-first. Excluded records sort last so they stay visible in
        the report without ever being recommended.
    """
    for record in records:
        record["excluded"] = False
        record["exclusion_reason"] = None

        # Upcasts: strictly above native precision. More expensive, and a
        # different numerical path from the reference weights rather than a
        # more faithful one. Only detectable when native is known.
        rank = record["precision_rank"]
        if native_rank is not None and rank is not None and rank > native_rank:
            record["excluded"] = True
            record["exclusion_reason"] = (
                f"upcast: serves {record['quantization']} above native precision"
            )
            continue

        # An endpoint with no slug cannot be pinned, which is the whole point
        # of the exercise; recommending it would produce an unpinnable route.
        if not record["provider_slug"]:
            record["excluded"] = True
            record["exclusion_reason"] = "no resolvable provider slug to pin to"

    def fidelity_key(record: dict[str, Any]) -> tuple:
        rank = record["precision_rank"]
        # Distance below native; unknown precision sorts after all known ones.
        if native_rank is not None and rank is not None:
            precision_gap = native_rank - rank
        elif rank is not None:
            precision_gap = -rank  # no native reference: least-quantized first
        else:
            precision_gap = 10_000
        return (
            record["excluded"],
            not record["is_author"],
            precision_gap,
            -(record["uptime_last_30m"] or 0.0),
            record["prompt_usd_per_mtok"] if record["prompt_usd_per_mtok"] else 0.0,
        )

    def cost_key(record: dict[str, Any]) -> tuple:
        return (
            record["excluded"],
            record["prompt_usd_per_mtok"]
            if record["prompt_usd_per_mtok"] is not None
            else float("inf"),
            -(record["uptime_last_30m"] or 0.0),
        )

    ordered = sorted(records, key=cost_key if prefer == "cost" else fidelity_key)

    # A provider can expose several endpoints for one model (regional variants,
    # differing tags) that all collapse to the same slug. Pinning targets the
    # slug, so duplicates cannot be chosen between — keep the best-ranked
    # record per slug and mark the rest, so the report stays readable without
    # hiding what exists.
    seen: set[str] = set()
    for record in ordered:
        slug = record["provider_slug"]
        if not slug:
            continue
        if slug in seen:
            record["duplicate_of_slug"] = slug
        else:
            seen.add(slug)
            record["duplicate_of_slug"] = None
    return ordered


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_model_args(provider_slug: str) -> dict[str, Any]:
    """Build the `-M` arguments that pin routing for an OpenRouter model.

    `require_parameters` is set unconditionally so OpenRouter never selects an
    endpoint that would silently drop sampling parameters the eval specifies.
    The metadata header makes the *served* provider appear in the response body,
    which Inspect stores verbatim in the logged ModelCall — turning the pin from
    something merely requested into something verifiable per sample.

    Parameters
    ----------
    provider_slug : str
        The upstream provider slug to pin to.

    Returns
    -------
    dict
        Model args, one key per `-M` argument.
    """
    return {
        "provider": {
            "order": [provider_slug],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "default_headers": {"X-OpenRouter-Metadata": "enabled"},
    }


def unpinned_model_args() -> dict[str, Any]:
    """Build `-M` arguments for the unpinned fallback route.

    Routing is left to OpenRouter, but the metadata header is still set so the
    served provider is recorded per sample. That is what makes the unpinned tier
    a measured variable rather than an uncontrolled one.

    Returns
    -------
    dict
        Model args, one key per `-M` argument.
    """
    return {"default_headers": {"X-OpenRouter-Metadata": "enabled"}}


def resolve_model_id(model_id: str) -> str:
    """Reduce a model string to the `<author>/<model>` id the catalogue uses.

    Parameters
    ----------
    model_id : str
        e.g. "openrouter/meta-llama/llama-3.3-70b-instruct:free". A leading
        "openrouter/" and any ":free"-style variant suffix are stripped.

    Returns
    -------
    str
        The base id, e.g. "meta-llama/llama-3.3-70b-instruct".

    Raises
    ------
    RuntimeError
        If the result is not of the form `<author>/<model>`.
    """
    resolved = model_id.strip().removeprefix("openrouter/")
    base_id = resolved.split(":")[0]  # drop :free / :nitro / :extended variants
    if "/" not in base_id:
        raise RuntimeError(
            f"Model id must be '<author>/<model>', got: {model_id!r}"
        )
    return base_id


def select(model_id: str, prefer: str, timeout: float) -> dict[str, Any]:
    """Enumerate a model's endpoints and recommend a provider to pin.

    Parameters
    ----------
    model_id : str
        An OpenRouter model id such as "meta-llama/llama-3.3-70b-instruct". A
        leading "openrouter/" and any ":free"-style variant suffix are stripped.
    prefer : str
        "fidelity" (default rule) or "cost".
    timeout : float
        Per-request socket timeout in seconds.

    Returns
    -------
    dict
        The full JSON report: resolved model id, inferred native precision,
        recommendation, ready-to-render model args, ranked endpoints, warnings.
    """
    # --- Resolve the model id ---
    base_id = resolve_model_id(model_id)
    author = base_id.split("/")[0]

    # --- Fetch live data (both endpoints are public) ---
    endpoints_payload = _get_json(f"{OPENROUTER_API}/models/{base_id}/endpoints", timeout)
    providers_payload = _get_json(f"{OPENROUTER_API}/providers", timeout)

    data = endpoints_payload.get("data") or {}
    raw_endpoints = data.get("endpoints") or []
    if not raw_endpoints:
        raise RuntimeError(f"No endpoints returned for model {base_id!r}")

    slug_map = build_slug_map(providers_payload.get("data") or [])
    records = normalise_endpoints(raw_endpoints, author, slug_map)

    native_rank, native_basis = infer_native_precision(records)
    ranked = rank_endpoints(records, native_rank, prefer)

    # --- Assemble recommendation ---
    warnings: list[str] = []
    eligible = [r for r in ranked if not r["excluded"]]

    if not eligible:
        recommendation = None
        model_args = unpinned_model_args()
        warnings.append(
            "No pinnable endpoint found. Falling back to the unpinned route: "
            "routing is uncontrolled and must be reported as a caveat."
        )
    else:
        best = eligible[0]
        recommendation = {
            "provider_slug": best["provider_slug"],
            "provider_name": best["provider_name"],
            "quantization": best["quantization"],
            "is_author": best["is_author"],
            "rationale": _rationale(best, native_rank, native_basis, prefer),
        }
        model_args = build_model_args(best["provider_slug"])

        # Precision warnings only make sense when precision is a live axis for
        # this model. Closed-weight models report no quantization anywhere, and
        # warning about upcasts there is pure noise.
        precision_is_reported = any(r["precision_rank"] is not None for r in ranked)

        if native_rank is None and precision_is_reported:
            fallback_rule = (
                "cheapest-first" if prefer == "cost" else "least-quantized-first"
            )
            warnings.append(
                f"Native precision {native_basis}. Selection used {fallback_rule}; "
                "upcasts could not be ruled out."
            )
        elif native_rank is not None and best["precision_rank"] is not None and best["precision_rank"] < native_rank:
            warnings.append(
                f"Selected endpoint serves {best['quantization']}, below the "
                "inferred native precision. Carry a fidelity caveat into the "
                "final report."
            )
        if (best["uptime_last_30m"] or 100.0) < 95.0:
            warnings.append(
                f"Selected endpoint uptime is {best['uptime_last_30m']:.1f}% over "
                "the last 30m. With allow_fallbacks disabled, an outage hard-fails "
                "the eval rather than silently rerouting."
            )

    # Endpoints disagreeing on honoured parameters or completion limits is the
    # concrete form the confound takes; surface it so the decision is informed.
    if len({tuple(sorted(r["supported_parameters"])) for r in ranked}) > 1:
        warnings.append(
            "Endpoints for this model disagree on which sampling parameters they "
            "honour — unpinned routing would vary the measurement instrument."
        )
    completion_caps = {
        r["max_completion_tokens"] for r in ranked if r["max_completion_tokens"]
    }
    if len(completion_caps) > 1:
        warnings.append(
            "Endpoints for this model disagree on max completion tokens "
            f"({min(completion_caps)}–{max(completion_caps)})."
        )

    return {
        "model": f"openrouter/{base_id}",
        "author": author,
        "preference": prefer,
        "native_precision_rank": native_rank,
        "native_precision_basis": native_basis,
        "recommended": recommendation,
        "recommended_model_args": model_args,
        "endpoints": ranked,
        "warnings": warnings,
    }


def _rationale(
    record: dict[str, Any], native_rank: int | None, native_basis: str, prefer: str
) -> str:
    """Compose a one-line justification for the recommended pin."""
    if prefer == "cost":
        price = record["prompt_usd_per_mtok"]
        return (
            "cheapest pinnable endpoint"
            + (f" at ${price}/Mtok prompt" if price is not None else "")
            + f"; serves {record['quantization'] or 'unknown precision'}"
        )
    if record["is_author"]:
        return f"model author's own endpoint, serving {record['quantization'] or 'unknown precision'}"
    if native_rank is not None and record["precision_rank"] == native_rank:
        return (
            f"native precision ({record['quantization']}) per {native_basis}; "
            "author does not serve this model directly"
        )
    return (
        f"least-quantized pinnable endpoint ({record['quantization'] or 'unknown'}); "
        f"native precision {native_basis}"
    )


# ---------------------------------------------------------------------------
# Context window (for sub-agents routed through OpenRouter)
# ---------------------------------------------------------------------------


def model_context_length(model_id: str, timeout: float = 10.0) -> int | None:
    """Largest context window that at least half of a model's endpoints serve.

    `subagents/runner.py` uses this for a non-Claude sub-agent on the
    `openrouter` route: the Claude Code CLI does not know such a model's
    window, so unless told otherwise it assumes a Claude-sized one and
    compacts a 1M-window model at ~200k tokens. The figure is an upper median
    (see `majority_context_length`): a sub-agent should use as much window as
    the reputable providers give before compacting, while a rare small-window
    host is ignored. A sub-agent's route is not pinned — OpenRouter routes
    each request to an endpoint that can hold it — and the sub-agent model is
    infrastructure, not a measurement instrument, so which upstream serves a
    long request does not matter.

    Parameters
    ----------
    model_id : str
        Any form `resolve_model_id` accepts, e.g. "google/gemini-3-pro".
    timeout : float
        Socket timeout in seconds for the one public, key-free request.

    Returns
    -------
    int | None
        The window in tokens (e.g. 131072 for Llama 3.3 70B, whose 11
        endpoints span 12k–131k), or None when the catalogue lists the model
        with no usable `context_length`.

    Raises
    ------
    RuntimeError
        Malformed id, network failure, HTTP error (an unlisted model is a 404)
        or invalid JSON — callers that must stay offline-safe catch it.
    """
    base_id = resolve_model_id(model_id)
    payload = _get_json(f"{OPENROUTER_API}/models/{base_id}/endpoints", timeout)
    endpoints = (payload.get("data") or {}).get("endpoints") or []
    lengths = [
        e.get("context_length")
        for e in endpoints
        if isinstance(e.get("context_length"), int) and e.get("context_length") > 0
    ]
    return majority_context_length(lengths)


def majority_context_length(lengths: list[int]) -> int | None:
    """Largest window that at least half of the endpoints offer.

    Sorting descending and taking index `ceil(n/2) - 1` yields the largest
    `w` such that at least half the endpoints serve `>= w` tokens. In the
    2026-10 catalogue frontier models (GPT-5.x, Gemini 3.x, Grok 4.x) report
    one window on every endpoint, so the rule only bites for open-weight
    models, whose outliers are small-window hosts.

    Parameters
    ----------
    lengths : list[int]
        Positive per-endpoint `context_length` values, in any order.

    Returns
    -------
    int | None
        The window, or None for an empty list.

    Examples
    --------
    >>> majority_context_length([1048576])
    1048576
    >>> majority_context_length([131072, 1048576])  # n=2: one endpoint is half
    1048576
    >>> majority_context_length([131072, 131072, 65536, 32768])
    131072
    """
    if not lengths:
        return None
    ranked = sorted(lengths, reverse=True)
    # -(-n // 2) is ceil(n/2) in integer arithmetic.
    return ranked[-(-len(ranked) // 2) - 1]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enumerate OpenRouter endpoints and recommend a provider to pin."
    )
    parser.add_argument("model", help="Model id, e.g. meta-llama/llama-3.3-70b-instruct")
    parser.add_argument(
        "--prefer",
        choices=["fidelity", "cost"],
        default="fidelity",
        help="Selection rule: fidelity (default) or cost.",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )
    args = parser.parse_args()

    try:
        report = select(args.model, args.prefer, args.timeout)
    except RuntimeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        sys.exit(1)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
