"""Running spend total for an investigation.

Sums four telemetry sources:

- **Sub-agent cost sidecars** — ``cost_*.json`` files written by
  ``subagents/runner.py`` from the Agent SDK's result message (USD, turns,
  tokens) for every invocation launched through ``scripts/launch_subagent.py``.
- **Lead-agent sessions** — the Claude Code session files of the lead agent
  (Orchestrator or Diagnostician) that ran this investigation, priced at API
  rates from the token usage of each model call. A session counts when it was
  started with ``/orchestrate`` or ``/diagnose`` and wrote or edited a file
  inside this investigation's directory, so a resumed lead counts and a session
  that only read the results does not. The figure is what the lead would cost
  on the API, which over-states real spend when the lead ran on a subscription.
- **Eval token usage** — per condition-model pair, recorded in each batch
  run's ``status.json`` by ``scripts/execute_evals.py`` from the eval log
  header's ``model_usage``: every attempt's (``usage_attempts`` — preflight
  probe, retest, and each full-run attempt including retried ones), or the
  final attempt's alone in a status file written before that field existed.
- **The Analyst's own model calls** — ``model_calls_*.jsonl`` files, one line
  per call that the Transcript Analyst's scanner and validator scripts make,
  written by ``subagents/transcript_analyst/call_log/sitecustomize.py``. These
  never pass through the SDK result the sidecar records.

Eval usage and the Analyst's calls are priced per token. Claude models use
``CLAUDE_PRICES_PER_MTOK``; every other model uses OpenRouter's public price
list (no key needed), at the pinned provider's own rates when the eval ran
through a pinned OpenRouter route. A call whose usage reports its own cost
(OpenRouter's ``usage.cost``) is taken at that figure. Prices are today's, not
the run's. A model with no known price adds nothing to the total and is listed
as unpriced, so a nonzero unpriced list means the total under-counts.

Both lead agents consume these figures: the Diagnostician's budget exit
(``diagnostician_responsibilities.md`` §Stopping rule) and the Orchestrator's
spend check before each Decision (``orchestrator_responsibilities.md`` Step
2j). The headline total is sub-agents plus lead plus evals plus the Analyst's
calls; every part is shown.

Usage
-----
    uv run python scripts/spend.py investigations/<name> [--json] [--offline]

``--offline`` never goes online: Claude models are priced from the table below,
others from the price lists an earlier total cached (see `PRICE_CACHE_DIR_ENV`).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from typing import Any, Callable

# API prices for the Claude models Prism's agents and scanners run on, in USD
# per million tokens: (input, output, cache read). Cache writes are priced from
# input: 1.25x for the 5-minute cache, 2x for the 1-hour cache Claude Code uses
# for a main session. Haiku's rate is its rate for prompts under 100K tokens,
# which scanner calls stay well within. A Claude model missing here falls back
# to OpenRouter's price list, then to tokens with no dollar figure, never a
# guess. Update with each model release.
CLAUDE_PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
# Fast mode doubles the per-token price (Opus 5.5: $8 / $40); assumed to apply
# to cache reads and writes alike.
FAST_MODE_MULTIPLIER = 2.0

# What marks a session as an investigation's lead: the skill that started it,
# and a file it wrote inside the investigation directory
_LEAD_SKILL_MARKERS = ("<command-name>/orchestrate</command-name>", "<command-name>/diagnose</command-name>")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit")


def _load(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def default_projects_dir() -> str:
    """Where Claude Code keeps per-project session files.

    Returns
    -------
    str
        ``$CLAUDE_CONFIG_DIR/projects``, or ``~/.claude/projects`` when unset.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(config, "projects")


def _project_slug(path: str) -> str:
    """Claude Code's folder name for a project path.

    Parameters
    ----------
    path : str
        Absolute project directory.

    Returns
    -------
    str
        The path with every non-alphanumeric character replaced by ``-``,
        e.g. ``/workspaces/mf-demos`` -> ``-workspaces-mf-demos``.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _price_call(model: str, usage: dict[str, Any]) -> float | None:
    """API price of one model call from its usage block.

    Parameters
    ----------
    model : str
        The model the call ran on.
    usage : dict
        The message's ``usage`` (input, output, cache read and write tokens,
        with the write split by cache lifetime when the CLI recorded it).

    Returns
    -------
    float | None
        USD, or None for a model missing from ``CLAUDE_PRICES_PER_MTOK``.
    """
    prices = CLAUDE_PRICES_PER_MTOK.get(_normalise_id(model))
    if prices is None:
        return None
    inp, out, read = prices

    # Cache writes split by lifetime; an unsplit figure is taken as 5-minute
    writes = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation") or {}
    w1h = split.get("ephemeral_1h_input_tokens") or 0
    w5m = split.get("ephemeral_5m_input_tokens") or 0
    if w1h + w5m == 0:
        w5m = writes

    cost = (
        (usage.get("input_tokens") or 0) * inp
        + (usage.get("output_tokens") or 0) * out
        + (usage.get("cache_read_input_tokens") or 0) * read
        + w5m * inp * CACHE_WRITE_5M_MULTIPLIER
        + w1h * inp * CACHE_WRITE_1H_MULTIPLIER
    ) / 1e6
    if usage.get("speed") == "fast":
        cost *= FAST_MODE_MULTIPLIER
    return cost


# ---------- Prices for any model: Claude table, then OpenRouter's list ----------

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"
CATALOGUE_TIMEOUT = 15.0

# Which OpenRouter vendor prefix a direct API's models are listed under
_HOST_VENDORS = {
    "api.anthropic.com": "anthropic",
    "api.openai.com": "openai",
    "generativelanguage.googleapis.com": "google",
}

# Token counts in one shape for every source: uncached input, output, cache
# reads, and cache writes by lifetime
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def _fetch_json(url: str) -> Any | None:
    """GET a public JSON document, or None on any failure (offline, timeout, bad JSON).

    Parameters
    ----------
    url : str
        The document's address.

    Returns
    -------
    Any | None
        The parsed JSON.
    """
    try:
        with urllib.request.urlopen(url, timeout=CATALOGUE_TIMEOUT) as r:
            return json.loads(r.read())
    except Exception:
        return None


# Price lists fetched once are kept on disk, so the panel's status poll (which
# never goes online) and later totals can price from them. Prices change
# rarely; a day-old list is refetched when the network is allowed.
PRICE_CACHE_DIR_ENV = "PRISM_PRICE_CACHE_DIR"
PRICE_CACHE_MAX_AGE = 24 * 3600.0


def _price_cache_path(url: str) -> str:
    """Where the cached copy of one price-list URL lives.

    Parameters
    ----------
    url : str
        The price list's address.

    Returns
    -------
    str
        e.g. ``~/.cache/prism/prices/openrouter.ai_api_v1_models.json``;
        ``$PRISM_PRICE_CACHE_DIR`` replaces the folder when set.
    """
    folder = os.environ.get(PRICE_CACHE_DIR_ENV) or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "prism", "prices"
    )
    name = re.sub(r"[^A-Za-z0-9.-]+", "_", url.split("://", 1)[-1])
    return os.path.join(folder, name + ".json")


def _read_price_cache(url: str) -> tuple[Any, float] | None:
    """The cached copy of a price list and its age in seconds, or None if there is none.

    Parameters
    ----------
    url : str
        The price list's address.

    Returns
    -------
    tuple[Any, float] | None
        ``(parsed JSON, seconds since it was fetched)``.
    """
    path = _price_cache_path(url)
    try:
        with open(path) as f:
            data = json.load(f)
        return data, time.time() - os.path.getmtime(path)
    except (OSError, json.JSONDecodeError):
        return None


def _write_price_cache(url: str, data: Any) -> None:
    """Keep a freshly fetched price list on disk; a failure to write is ignored.

    Parameters
    ----------
    url : str
        The price list's address.
    data : Any
        Its parsed JSON.
    """
    path = _price_cache_path(url)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Written beside and renamed, so a reader never sees half a file
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _normalise_id(model: str) -> str:
    """Model id in a form that matches across providers' spellings.

    Lower-cased, dots as dashes, and any trailing release date dropped, so
    ``claude-sonnet-5.5``, ``claude-sonnet-5-5`` and ``gpt-5.2-2025-12-11``
    match the catalogue's ``claude-sonnet-5-5`` and ``gpt-5-2``.

    Parameters
    ----------
    model : str
        A model id as some provider or log spells it.

    Returns
    -------
    str
        The normalised id.
    """
    m = model.lower().replace(".", "-")
    return re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", m)


def _openrouter_prices(pricing: dict[str, Any]) -> dict[str, float] | None:
    """USD-per-token prices from an OpenRouter ``pricing`` block.

    A missing cache price falls back to the input price, which over- rather
    than under-states spend.

    Parameters
    ----------
    pricing : dict
        The block's string prices: ``prompt``, ``completion`` and the cache ones.

    Returns
    -------
    dict | None
        USD per token for each of `TOKEN_KINDS`; None without both main prices.
    """
    try:
        inp = float(pricing["prompt"])
        out = float(pricing["completion"])
    except (KeyError, TypeError, ValueError):
        return None
    write = float(pricing.get("input_cache_write") or inp)
    return {
        "input": inp,
        "output": out,
        "cache_read": float(pricing.get("input_cache_read") or inp),
        "cache_write_5m": write,
        "cache_write_1h": float(pricing.get("input_cache_write_1h") or write),
    }


class PriceBook:
    """Per-token prices for any model, each looked up at most once per run.

    Parameters
    ----------
    fetch : Callable[[str], Any] | None
        Fetches a URL's JSON, or returns None; ``_fetch_json`` when None.
        Tests pass a stub.
    offline : bool
        If True, never fetch: ``CLAUDE_PRICES_PER_MTOK`` and whatever price
        lists an earlier, online total left in the cache price everything.
    """

    def __init__(self, fetch: Callable[[str], Any] | None = None, offline: bool = False):
        self._fetch = fetch
        self._offline = offline
        self._catalogue: dict[str, dict[str, float]] | None = None
        self._endpoints: dict[str, list[dict[str, Any]]] = {}

    def _get(self, url: str) -> Any | None:
        """A price list: from the cache while fresh (or whenever offline), else fetched.

        Parameters
        ----------
        url : str
            The price list's address.

        Returns
        -------
        Any | None
            Its parsed JSON; a stale cached copy when a fetch fails; None
            when neither is to be had.
        """
        cached = _read_price_cache(url)
        if cached is not None and (self._offline or cached[1] < PRICE_CACHE_MAX_AGE):
            return cached[0]
        if self._offline:
            return None
        data = (self._fetch or _fetch_json)(url)
        if data is not None:
            _write_price_cache(url, data)
            return data
        return cached[0] if cached is not None else None

    def claude(self, model: str) -> dict[str, float] | None:
        """A Claude model's price from Prism's own table.

        Parameters
        ----------
        model : str
            The bare Claude id in any spelling, e.g. ``claude-opus-5.5``.

        Returns
        -------
        dict | None
            Per-token prices; None for a model missing from `CLAUDE_PRICES_PER_MTOK`.
        """
        p = CLAUDE_PRICES_PER_MTOK.get(_normalise_id(model))
        if p is None:
            return None
        inp, out, read = (x / 1e6 for x in p)
        return {
            "input": inp,
            "output": out,
            "cache_read": read,
            "cache_write_5m": inp * CACHE_WRITE_5M_MULTIPLIER,
            "cache_write_1h": inp * CACHE_WRITE_1H_MULTIPLIER,
        }

    def catalogue(self, slug: str) -> dict[str, float] | None:
        """OpenRouter's listed price for a model, matched on the normalised id.

        Parameters
        ----------
        slug : str
            ``vendor/model``, e.g. ``openai/gpt-5.2``.

        Returns
        -------
        dict | None
            Per-token prices; None when the list lacks the model or could not be had.
        """
        if self._catalogue is None:
            data = self._get(OPENROUTER_MODELS_URL)
            self._catalogue = {}
            for m in (data or {}).get("data") or []:
                prices = _openrouter_prices(m.get("pricing") or {})
                if prices and isinstance(m.get("id"), str):
                    self._catalogue.setdefault(_normalise_id(m["id"]), prices)
        return self._catalogue.get(_normalise_id(slug))

    def endpoint(self, slug: str, provider: str) -> dict[str, float] | None:
        """One OpenRouter provider's own price for a model.

        Parameters
        ----------
        slug : str
            OpenRouter's model id, e.g. ``deepseek/deepseek-r1-0528``.
        provider : str
            The pinned provider, e.g. ``siliconflow``.

        Returns
        -------
        dict | None
            Per-token prices; None when the provider does not serve the model
            or the list could not be had.
        """
        if slug not in self._endpoints:
            data = self._get(OPENROUTER_ENDPOINTS_URL.format(slug=slug))
            self._endpoints[slug] = ((data or {}).get("data") or {}).get("endpoints") or []
        want = provider.lower()
        for e in self._endpoints[slug]:
            tag = str(e.get("tag") or "").lower()
            if want in (tag, tag.split("/")[0], str(e.get("provider_name") or "").lower()):
                return _openrouter_prices(e.get("pricing") or {})
        return None

    def for_eval_model(self, model: str, route: str | None) -> dict[str, float] | None:
        """Price for an Inspect model string used in an eval.

        Parameters
        ----------
        model : str
            Inspect's name, e.g. ``openrouter/deepseek/deepseek-r1-0528`` or
            ``anthropic/claude-sonnet-5-5``.
        route : str | None
            The pair's route label when ``model`` is the pair's target, e.g.
            ``openrouter:siliconflow``; None for any other model in the eval
            (a grader, say).

        Returns
        -------
        dict | None
            Per-token prices, or None when no source knows the model.
        """
        vendor, _, rest = model.partition("/")
        if vendor == "openrouter":
            provider = (route or "").partition(":")[2]
            if provider and provider != "unpinned":
                pinned = self.endpoint(rest, provider)
                if pinned:
                    return pinned
            return self.catalogue(rest)
        if vendor == "anthropic":
            return self.claude(rest) or self.catalogue(model)
        return self.catalogue(model)

    def for_call(self, host: str, model: str) -> dict[str, float] | None:
        """Price for one logged API call, from the host it went to and the model it reports.

        Parameters
        ----------
        host : str
            The API's host, e.g. ``api.anthropic.com`` or ``openrouter.ai``.
        model : str
            The model the response names, e.g. ``claude-haiku-5-5`` or
            ``anthropic/claude-haiku-5.5``.

        Returns
        -------
        dict | None
            Per-token prices, or None when no source knows the model.
        """
        if "/" in model:
            # OpenRouter reports the vendor-prefixed id
            return self.claude(model.partition("/")[2]) or self.catalogue(model)
        vendor = _HOST_VENDORS.get(host)
        return self.claude(model) or (self.catalogue(f"{vendor}/{model}") if vendor else None)


def _tokens_from_inspect(mu: dict[str, Any]) -> dict[str, int]:
    """Inspect's ``ModelUsage`` as `TOKEN_KINDS`; its input count already excludes cached tokens.

    Parameters
    ----------
    mu : dict
        One model's usage from an eval log's stats.

    Returns
    -------
    dict[str, int]
        Tokens of each of `TOKEN_KINDS`; Inspect does not split cache writes
        by lifetime, so all count as 5-minute.
    """
    return {
        "input": int(mu.get("input_tokens") or 0),
        "output": int(mu.get("output_tokens") or 0),
        "cache_read": int(mu.get("input_tokens_cache_read") or 0),
        "cache_write_5m": int(mu.get("input_tokens_cache_write") or 0),
        "cache_write_1h": 0,
    }


def _tokens_from_call(usage: dict[str, Any]) -> dict[str, int]:
    """A raw API ``usage`` block as `TOKEN_KINDS`, whichever API shape it has.

    OpenAI's chat (``prompt_tokens``) and Responses (``input_tokens_details``)
    shapes count cached tokens inside the input total, so they are subtracted;
    Anthropic's shape counts them separately and may split writes by lifetime.

    Parameters
    ----------
    usage : dict
        The ``usage`` block of one API response.

    Returns
    -------
    dict[str, int]
        e.g. ``{"input": 900, "output": 50, "cache_read": 100, "cache_write_5m": 0,
        "cache_write_1h": 0}`` for ``{"prompt_tokens": 1000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 100}}``.
    """
    def n(d: Any, key: str) -> int:
        return int((d or {}).get(key) or 0) if isinstance(d, dict) else 0

    if "prompt_tokens" in usage or "input_tokens_details" in usage or "output_tokens_details" in usage:
        chat = "prompt_tokens" in usage
        details = usage.get("prompt_tokens_details" if chat else "input_tokens_details")
        read, write = n(details, "cached_tokens"), n(details, "cache_write_tokens")
        total_in = n(usage, "prompt_tokens" if chat else "input_tokens")
        return {
            "input": max(total_in - read - write, 0),
            "output": n(usage, "completion_tokens" if chat else "output_tokens"),
            "cache_read": read,
            "cache_write_5m": write,
            "cache_write_1h": 0,
        }
    split = usage.get("cache_creation")
    w1h, w5m = n(split, "ephemeral_1h_input_tokens"), n(split, "ephemeral_5m_input_tokens")
    if w1h + w5m == 0:
        w5m = n(usage, "cache_creation_input_tokens")
    return {
        "input": n(usage, "input_tokens"),
        "output": n(usage, "output_tokens"),
        "cache_read": n(usage, "cache_read_input_tokens"),
        "cache_write_5m": w5m,
        "cache_write_1h": w1h,
    }


def _cost(prices: dict[str, float], tokens: dict[str, int]) -> float:
    """USD for a token count at per-token prices.

    Parameters
    ----------
    prices : dict[str, float]
        USD per token for each of `TOKEN_KINDS`.
    tokens : dict[str, int]
        Tokens of each of `TOKEN_KINDS`.

    Returns
    -------
    float
        e.g. 4.0 for a million input tokens at ``{"input": 4e-6, ...}``.
    """
    return sum(tokens[k] * prices[k] for k in TOKEN_KINDS)


def collect_model_calls(root: str, prices: PriceBook) -> dict[str, Any]:
    """Price the Analyst's logged model calls under an investigation.

    Parameters
    ----------
    root : str
        ``investigations/<name>``, walked for ``model_calls_*.jsonl``.
    prices : PriceBook
        Where per-token prices come from.

    Returns
    -------
    dict
        ``{"total_cost_usd": x, "calls": n, "files": k, "unpriced_calls": u,
        "unpriced_models": [...], "by_model": {model: {"calls": n,
        "cost_usd": x, "input": ..., "output": ...}}}``. A call whose usage
        carries its own ``cost`` (OpenRouter) is taken at that figure.
    """
    by_model: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, **{k: 0 for k in TOKEN_KINDS}})
    total = 0.0
    calls = files = unpriced_calls = 0
    unpriced: set[str] = set()
    for dirpath, _dirs, names in os.walk(os.path.abspath(root)):
        for fn in names:
            if not (fn.startswith("model_calls_") and fn.endswith(".jsonl")):
                continue
            files += 1
            try:
                with open(os.path.join(dirpath, fn)) as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in lines:
                try:
                    rec = json.loads(line)
                    model, usage = str(rec["model"]), dict(rec["usage"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                calls += 1
                tokens = _tokens_from_call(usage)
                m = by_model[model]
                m["calls"] += 1
                for k in TOKEN_KINDS:
                    m[k] += tokens[k]
                if isinstance(usage.get("cost"), (int, float)):
                    cost = float(usage["cost"])
                else:
                    p = prices.for_call(str(rec.get("host") or ""), model)
                    if p is None:
                        unpriced_calls += 1
                        unpriced.add(model)
                        continue
                    cost = _cost(p, tokens)
                m["cost_usd"] += cost
                total += cost
    return {
        "total_cost_usd": round(total, 4),
        "calls": calls,
        "files": files,
        "unpriced_calls": unpriced_calls,
        "unpriced_models": sorted(unpriced),
        "by_model": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in sorted(by_model.items())},
    }


def _investigation_written(entry: dict[str, Any]) -> str | None:
    """The investigation one session line wrote a file into, if any.

    Parameters
    ----------
    entry : dict
        One parsed line of a Claude Code session file.

    Returns
    -------
    str | None
        The investigation's folder name, e.g. ``"sandbagging_r2"`` for an Edit
        of ``…/investigations/sandbagging_r2/INVESTIGATION-LOG.md``; None for a
        line that wrote nothing there. Lines from Agent SDK sessions (a
        helper's, never the lead's) are ignored.
    """
    if entry.get("type") != "assistant" or str(entry.get("entrypoint") or "").startswith("sdk"):
        return None
    written = None
    for block in (entry.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in _WRITE_TOOLS:
            m = re.search(r"investigations/([^/]+)/", str((block.get("input") or {}).get("file_path") or ""))
            if m:
                written = m.group(1)
    return written


def _read_lead_session(path: str, investigation: str, seen: set[str] | None = None) -> dict[str, Any] | None:
    """Total one session file's usage if it is this investigation's lead.

    A session that worked on several investigations is split between them:
    each call goes to the investigation most recently written into, and the
    calls before its first write to the first one it wrote into.

    Parameters
    ----------
    path : str
        A Claude Code ``<session>.jsonl`` file.
    investigation : str
        Absolute path of the investigation directory.
    seen : set[str] | None
        Message ids already counted from other files, updated in place. A
        resumed session's new file repeats the earlier messages with their
        ids, so each call is counted once across all of the files.

    Returns
    -------
    dict | None
        ``{"session": id, "path": path, "calls": n, "cost_usd": x,
        "unpriced_calls": k, "tokens_by_model": {...}}``, where ``cost_usd``
        covers the priced calls only; or None when the session is not a lead
        session that wrote into the investigation.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None

    # ---------- Is this the investigation's lead? ----------
    name = os.path.basename(investigation)
    is_lead_skill = any(m in line for line in lines for m in _LEAD_SKILL_MARKERS)
    if not is_lead_skill:
        return None
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # ---------- Which investigation each call worked for ----------
    # Claude Code writes one line per content block, each carrying the whole
    # message's usage, so calls are keyed by message id
    calls: dict[str, tuple[str, dict[str, Any], str | None]] = {}
    current = None
    first_written = None
    for e in entries:
        current = _investigation_written(e) or current
        first_written = first_written or current
        msg = e.get("message") or {}
        # Claude Code's own placeholder replies (an error, an interrupted
        # turn) carry a usage block but were never an API call
        if e.get("type") == "assistant" and msg.get("usage") and msg.get("id") and msg.get("model") != "<synthetic>":
            calls[msg["id"]] = (msg.get("model") or "?", msg["usage"], current)
    if first_written is None:
        return None
    calls = {
        mid: (model, usage)
        for mid, (model, usage, inv) in calls.items()
        if (inv or first_written) == name and (seen is None or mid not in seen)
    }
    if not calls:
        return None
    if seen is not None:
        seen.update(calls)

    tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cost = 0.0
    unpriced_calls = 0
    for model, usage in calls.values():
        t = tokens[model]
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            t[k] += int(usage.get(k) or 0)
        c = _price_call(model, usage)
        if c is None:
            unpriced_calls += 1
        else:
            cost += c
    return {
        "session": os.path.splitext(os.path.basename(path))[0],
        "path": path,
        "calls": len(calls),
        "cost_usd": round(cost, 4),
        "unpriced_calls": unpriced_calls,
        "tokens_by_model": {m: dict(v) for m, v in sorted(tokens.items())},
    }


def collect_lead(root: str, projects_dir: str | None = None) -> dict[str, Any]:
    """Find and price the lead-agent sessions of one investigation.

    Parameters
    ----------
    root : str
        ``<repo>/investigations/<name>``; the sessions are looked for under the
        Claude Code project folder of ``<repo>``.
    projects_dir : str | None
        Claude Code's projects folder; ``default_projects_dir()`` when None.

    Returns
    -------
    dict
        ``{"total_cost_usd": x, "sessions": [...], "unpriced_models": [...]}``;
        calls on an unpriced model add nothing to the total and their model
        is listed instead.
    """
    investigation = os.path.abspath(root)
    repo = os.path.dirname(os.path.dirname(investigation))
    folder = os.path.join(projects_dir or default_projects_dir(), _project_slug(repo))
    sessions = []
    seen: set[str] = set()
    # Oldest first, so a resumed session's repeated calls stay with the file
    # that first made them
    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl")), key=lambda p: (os.path.getmtime(p), p)):
        s = _read_lead_session(path, investigation, seen)
        if s is not None:
            sessions.append(s)
    total = sum(s["cost_usd"] for s in sessions)
    unpriced = sorted({m for s in sessions for m in s["tokens_by_model"]
                       if _normalise_id(m) not in CLAUDE_PRICES_PER_MTOK})
    return {"total_cost_usd": round(total, 4), "sessions": sessions, "unpriced_models": unpriced}


def collect(root: str, projects_dir: str | None = None, prices: PriceBook | None = None) -> dict[str, Any]:
    """Walk an investigation directory and total its telemetry.

    Parameters
    ----------
    root : str
        ``investigations/<name>`` (any directory; walked recursively).
    projects_dir : str | None
        Claude Code's projects folder, where the lead sessions are found;
        ``default_projects_dir()`` when None.
    prices : PriceBook | None
        Source of per-token prices for evals and the Analyst's calls; a live
        ``PriceBook()`` when None.

    Returns
    -------
    dict
        ``total_cost_usd``: sub-agents plus lead plus evals plus the
        Analyst's calls; ``subagents``: total USD, count, per-role totals and
        the list of sidecars; ``lead``: see `collect_lead`; ``evals``: USD,
        per-model token totals and costs, and per-run summaries;
        ``analyst_calls``: see `collect_model_calls`. Example::

            {"total_cost_usd": 52.84,
             "subagents": {"total_cost_usd": 12.34, "invocations": 7,
                           "by_agent": {"Transcript analyst": {"cost_usd": 9.1, "invocations": 3}}},
             "lead": {"total_cost_usd": 28.0, "sessions": [...], "unpriced_models": []},
             "evals": {"total_cost_usd": 11.3, "runs": 4, "pairs": 9, "unpriced_models": [],
                       "cost_by_model": {"openai/gpt-5": 11.3},
                       "tokens_by_model": {"openai/gpt-5": {"input_tokens": 1e6, ...}}},
             "analyst_calls": {"total_cost_usd": 1.2, "calls": 840, ...}}
    """
    prices = prices or PriceBook()
    sidecars: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for dirpath, _dirs, files in os.walk(os.path.abspath(root)):
        for fn in files:
            path = os.path.join(dirpath, fn)
            if fn.startswith("cost_") and fn.endswith(".json"):
                data = _load(path)
                if data and data.get("prism_cost") == 1:
                    data["_path"] = path
                    sidecars.append(data)
            elif fn == "status.json":
                data = _load(path)
                if data and data.get("schema") and "pairs" in data:
                    data["_path"] = path
                    runs.append(data)

    by_agent: dict[str, dict[str, Any]] = defaultdict(lambda: {"cost_usd": 0.0, "invocations": 0, "turns": 0})
    total = 0.0
    for s in sidecars:
        cost = float(s.get("total_cost_usd") or 0.0)
        total += cost
        a = by_agent[s.get("agent", "?")]
        a["cost_usd"] += cost
        a["invocations"] += 1
        a["turns"] += int(s.get("num_turns") or 0)

    tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cost_by_model: dict[str, float] = defaultdict(float)
    unpriced_evals: set[str] = set()
    pair_count = 0
    run_summaries = []
    for r in runs:
        run_tokens = 0
        run_cost = 0.0
        for e in r.get("pairs", {}).values():
            pair_count += 1
            # All attempts where recorded; older status files kept only the
            # final attempt's usage, which is then the best figure there is.
            attempts = e.get("usage_attempts")
            if attempts is None:
                attempts = [e.get("model_usage") or {}]
            for usage in attempts:
                for model, mu in (usage or {}).items():
                    for k in ("input_tokens", "output_tokens", "total_tokens", "input_tokens_cache_read", "input_tokens_cache_write", "reasoning_tokens"):
                        v = mu.get(k)
                        if v:
                            tokens[model][k] += int(v)
                    run_tokens += int(mu.get("total_tokens") or 0)

                    # The pair's route applies to its target model only, not
                    # to a grader or other model the eval also called
                    route = e.get("route") if model == e.get("model") else None
                    p = prices.for_eval_model(model, route)
                    if p is None:
                        unpriced_evals.add(model)
                        continue
                    c = _cost(p, _tokens_from_inspect(mu))
                    cost_by_model[model] += c
                    run_cost += c
        run_summaries.append({"run_id": r.get("run_id"), "state": r.get("state"), "pairs": len(r.get("pairs", {})), "total_tokens": run_tokens, "cost_usd": round(run_cost, 4), "path": r["_path"]})
    eval_total = sum(cost_by_model.values())

    lead = collect_lead(root, projects_dir)
    calls = collect_model_calls(root, prices)
    return {
        "total_cost_usd": round(total + lead["total_cost_usd"] + eval_total + calls["total_cost_usd"], 4),
        "subagents": {
            "total_cost_usd": round(total, 4),
            "invocations": len(sidecars),
            "by_agent": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in sorted(by_agent.items())},
            "sidecars": [{"agent": s.get("agent"), "model": s.get("model"), "cost_usd": s.get("total_cost_usd"), "turns": s.get("num_turns"), "subtype": s.get("subtype"), "path": s["_path"]} for s in sidecars],
        },
        "lead": lead,
        "evals": {
            "total_cost_usd": round(eval_total, 4),
            "runs": len(runs),
            "pairs": pair_count,
            "unpriced_models": sorted(unpriced_evals),
            "cost_by_model": {m: round(v, 4) for m, v in sorted(cost_by_model.items())},
            "tokens_by_model": {m: dict(v) for m, v in sorted(tokens.items())},
            "run_summaries": run_summaries,
        },
        "analyst_calls": calls,
    }


def render(summary: dict[str, Any]) -> str:
    """Human-readable rendering of `collect`'s output."""
    s, e, ld, ac = summary["subagents"], summary["evals"], summary["lead"], summary["analyst_calls"]
    lines = [
        f"total spend: ${summary['total_cost_usd']:.2f} (sub-agents ${s['total_cost_usd']:.2f} + lead ${ld['total_cost_usd']:.2f}"
        f" + evals ${e['total_cost_usd']:.2f} + analyst scans ${ac['total_cost_usd']:.2f})"
    ]
    unpriced = sorted(set(ld["unpriced_models"]) | set(e["unpriced_models"]) | set(ac["unpriced_models"]))
    if unpriced:
        lines.append(f"  under-counts: no price for {', '.join(unpriced)}; their usage is in tokens below")
    lines.append(f"sub-agent spend: ${s['total_cost_usd']:.2f} over {s['invocations']} invocation(s)")
    for agent, v in s["by_agent"].items():
        lines.append(f"  {agent:<24} ${v['cost_usd']:>8.2f}  {v['invocations']} invocation(s), {v['turns']} turns")
    lines.append(f"lead spend (at API prices): ${ld['total_cost_usd']:.2f} over {len(ld['sessions'])} session(s)")
    for sess in ld["sessions"]:
        unpriced = f" ({sess['unpriced_calls']} unpriced)" if sess["unpriced_calls"] else ""
        lines.append(f"  {sess['session']}  ${sess['cost_usd']:.2f}  {sess['calls']} model calls{unpriced}")
    if ld["unpriced_models"]:
        lines.append(f"  no price for: {', '.join(ld['unpriced_models'])} (tokens only; add to CLAUDE_PRICES_PER_MTOK)")
    lines.append(f"eval spend: ${e['total_cost_usd']:.2f} over {e['runs']} run(s), {e['pairs']} condition-model pair(s)")
    for model, t in e["tokens_by_model"].items():
        cost = e["cost_by_model"].get(model)
        price = f"${cost:>8.2f}" if cost is not None else " unpriced"
        lines.append(f"  {model:<40} {price}  in={t.get('input_tokens', 0):>10,} out={t.get('output_tokens', 0):>9,} total={t.get('total_tokens', 0):>11,}")
    if not e["tokens_by_model"]:
        lines.append("  (no eval usage recorded)")
    lines.append(f"analyst scan spend: ${ac['total_cost_usd']:.2f} over {ac['calls']} model call(s)")
    for model, m in ac["by_model"].items():
        price = " unpriced" if model in ac["unpriced_models"] else f"${m['cost_usd']:>8.2f}"
        lines.append(f"  {model:<40} {price}  {m['calls']} call(s), in={m['input']:>10,} out={m['output']:>9,}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Total an investigation's spend: sub-agents, lead, evals and the Analyst's scans.")
    parser.add_argument("investigation", help="investigations/<name> directory")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--offline", action="store_true", help="never go online; price non-Claude models from cached price lists only")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.investigation):
        print(f"not a directory: {args.investigation}", file=sys.stderr)
        return 1
    summary = collect(args.investigation, prices=PriceBook(offline=args.offline))
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
