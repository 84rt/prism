"""Log the token usage of every model call the Transcript Analyst's Python makes.

The Analyst writes its own scanner and validator scripts, which call models
directly (the Anthropic or OpenAI library, Inspect Scout, Inspect's
``get_model``) rather than through anything Prism records. Python imports a
module named ``sitecustomize`` at startup from anywhere on its path, so the
runner puts this file's folder on ``PYTHONPATH`` for the Analyst alone, and
every Python process the Analyst starts loads it.

All of those libraries reach the provider through ``httpx`` or its renamed
copy ``httpx2`` (which current Anthropic and OpenAI libraries use), so the hook
wraps the one place every request passes in each, ``Client.send`` and
``AsyncClient.send``. For each JSON or streamed (server-sent events)
response that carries a ``usage`` block and a ``model``, it appends one line to
the file named by ``PRISM_MODEL_CALLS_FILE``::

    {"t": "2026-11-10T14:02:11Z", "host": "api.anthropic.com",
     "model": "claude-haiku-5-5", "usage": {"input_tokens": 812, ...}}

``scripts/spend.py`` prices those lines. The hook never changes a request or a
response, and any failure inside it is swallowed, so a logging fault can cost a
line in the log but never a scan. Python started with ``-I`` or ``-S`` skips
``sitecustomize``, so those calls go unlogged.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

CALLS_FILE_ENV = "PRISM_MODEL_CALLS_FILE"


def _find_usage(event: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Pull the model name and usage block out of one response or stream event.

    Parameters
    ----------
    event : Any
        A decoded JSON body or SSE ``data:`` payload. The usage sits at the top
        level (Anthropic and OpenAI responses, OpenAI's final stream chunk),
        under ``message`` (Anthropic's ``message_start``) or under ``response``
        (OpenAI's Responses API ``response.completed``).

    Returns
    -------
    tuple[str | None, dict | None]
        ``(model, usage)``, either part None when absent.
    """
    if not isinstance(event, dict):
        return None, None
    for holder in (event, event.get("message"), event.get("response")):
        if isinstance(holder, dict) and isinstance(holder.get("usage"), dict):
            model = holder.get("model") if isinstance(holder.get("model"), str) else None
            return model, holder["usage"]
    return None, None


def _write(path: str, host: str, model: str, usage: dict[str, Any]) -> None:
    """Append one call's record; a single ``O_APPEND`` write keeps lines whole across processes.

    Parameters
    ----------
    path : str
        The call log, ``$PRISM_MODEL_CALLS_FILE``.
    host : str
        The API's host, e.g. ``api.anthropic.com``.
    model : str
        The model the response names.
    usage : dict
        The response's usage block, as the API sent it.
    """
    record = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": host, "model": model, "usage": usage}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)


def _record_body(path: str, host: str, body: bytes) -> None:
    """Log a complete JSON response body that carries usage.

    Parameters
    ----------
    path : str
        The call log.
    host : str
        The API's host.
    body : bytes
        The whole response body.
    """
    model, usage = _find_usage(json.loads(body))
    if model and usage:
        _write(path, host, model, usage)


def _record_stream(path: str, host: str, text: str) -> None:
    """Log a finished event stream, merging the usage spread over its events.

    Anthropic sends input and cache counts in ``message_start`` and the final
    output count in ``message_delta``; OpenAI sends one usage block on the last
    chunk. Merging every block in order covers both: a later count replaces an
    earlier one, and a null never overwrites a number.

    Parameters
    ----------
    path : str
        The call log.
    host : str
        The API's host.
    text : str
        The whole stream, ``data:`` lines and all.
    """
    model: str | None = None
    usage: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue  # e.g. OpenAI's closing "data: [DONE]"
        m, u = _find_usage(event)
        model = model or m
        if u:
            usage.update({k: v for k, v in u.items() if v is not None})
    if model and usage:
        _write(path, host, model, usage)


def _install(path: str, httpx: Any) -> None:
    """Wrap one HTTP library's send methods so each model response is logged to ``path``.

    Parameters
    ----------
    path : str
        The log file.
    httpx : module
        ``httpx`` or ``httpx2``; the two share this API.
    """

    def kind(response: httpx.Response) -> str | None:
        ctype = response.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return "stream"
        if "json" in ctype:
            return "json"
        return None

    def record(k: str, host: str, body: bytes) -> None:
        try:
            if k == "stream":
                _record_stream(path, host, body.decode("utf-8", "replace"))
            else:
                _record_body(path, host, body)
        except Exception:
            pass

    def tee_sync(response: httpx.Response, k: str, host: str) -> None:
        # Copy the decoded bytes as the caller reads them, and log once the
        # body has been read to the end (``read()`` goes through here too)
        original = response.iter_bytes
        chunks: list[bytes] = []

        def iter_bytes(*args: Any, **kwargs: Any):
            try:
                for chunk in original(*args, **kwargs):
                    chunks.append(chunk)
                    yield chunk
            finally:
                record(k, host, b"".join(chunks))

        response.iter_bytes = iter_bytes  # type: ignore[method-assign]

    def tee_async(response: httpx.Response, k: str, host: str) -> None:
        original = response.aiter_bytes
        chunks: list[bytes] = []

        async def aiter_bytes(*args: Any, **kwargs: Any):
            try:
                async for chunk in original(*args, **kwargs):
                    chunks.append(chunk)
                    yield chunk
            finally:
                record(k, host, b"".join(chunks))

        response.aiter_bytes = aiter_bytes  # type: ignore[method-assign]

    def after_send(response: httpx.Response, streamed: bool, asynchronous: bool) -> None:
        try:
            k = kind(response)
            if k is None:
                return
            host = response.request.url.host
            if streamed:
                # Body not read yet: log when the caller finishes reading it
                (tee_async if asynchronous else tee_sync)(response, k, host)
            else:
                record(k, host, response.content)
        except Exception:
            pass

    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send

    def send(self: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        response = original_send(self, request, *args, **kwargs)
        after_send(response, bool(kwargs.get("stream")), asynchronous=False)
        return response

    async def async_send(self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        response = await original_async_send(self, request, *args, **kwargs)
        after_send(response, bool(kwargs.get("stream")), asynchronous=True)
        return response

    httpx.Client.send = send  # type: ignore[method-assign]
    httpx.AsyncClient.send = async_send  # type: ignore[method-assign]


# Only the Analyst's processes have the variable set; anything else that finds
# this file on its path is left untouched
if os.environ.get(CALLS_FILE_ENV):
    import importlib

    for _name in ("httpx", "httpx2"):
        try:
            _install(os.environ[CALLS_FILE_ENV], importlib.import_module(_name))
        except Exception:
            pass  # not installed in this environment, or an unexpected API
