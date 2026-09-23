"""
Data Commons MCP tools for the host agent -- world statistics (countries,
regions, indicators, time series) from https://api.datacommons.org/mcp.

The same MCP tools reach both backends:

  * hermes      -- registered into Hermes' global tool registry under the
                   `datacommons` toolset (`register_hermes_tools`);
  * hermes_lite -- as LangChain tools (`as_langchain_tools`).

Tool names get a `dc_` prefix so they never collide with Hermes built-ins.

Enabled when `DC_API_KEY` is set. Startup never fails on the MCP server: if it
is unreachable the host runs without these tools and `ensure_loaded()` retries
on a later request, at most once per `DATACOMMONS_RETRY_SECONDS`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

from agents.datacommons_mcp import DEFAULT_URL, MAIN_PLAYBOOK_URI, DataCommonsMCP, MCPError

logger = logging.getLogger("datacommons_tools")

TOOLSET = "datacommons"
TOOL_PREFIX = "dc_"
# One MCP answer can be large; keep a single result from flooding the context.
RESULT_LIMIT = 12_000

_lock = threading.Lock()
_client: Optional[DataCommonsMCP] = None
_tools: list[dict] = []
_playbook = ""
_error: Optional[str] = None
_last_attempt = 0.0
_hermes_registered = False


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def enabled() -> bool:
    return bool(_env("DC_API_KEY"))


def loaded() -> bool:
    return bool(_tools)


def ensure_loaded() -> bool:
    """Connect and fetch the tool list once; retry after a cooldown on failure."""
    global _last_attempt
    if not enabled():
        return False
    if _tools:
        return True
    # Non-blocking: while one request is attempting the connection, others
    # carry on without the tools instead of queueing behind a network timeout.
    if not _lock.acquire(blocking=False):
        return False
    try:
        if _tools:
            return True
        retry = float(_env("DATACOMMONS_RETRY_SECONDS", "300") or 300)
        if _last_attempt and time.monotonic() - _last_attempt < retry:
            return False
        _last_attempt = time.monotonic()
        return _connect()
    finally:
        _lock.release()


def _connect() -> bool:
    """One connection attempt: handshake, tool list, playbook. Caller holds `_lock`."""
    global _client, _tools, _playbook, _error
    try:
        client = DataCommonsMCP(
            api_key=_env("DC_API_KEY"),
            url=_env("DATACOMMONS_MCP_URL", DEFAULT_URL) or DEFAULT_URL,
            timeout=float(_env("DATACOMMONS_TIMEOUT", "60") or 60),
        )
        info = client.connect()
        tools = client.list_tools()
    except (MCPError, ValueError) as exc:
        _error = f"Data Commons MCP unavailable: {_redact(str(exc))}"
        logger.warning("%s", _error)
        return False

    playbook = ""
    if _env("DATACOMMONS_PLAYBOOK", "true").lower() in {"1", "true", "yes", "on"}:
        try:
            playbook = client.read_resource(MAIN_PLAYBOOK_URI)
        except MCPError as exc:
            # The tools still work; answers are just less well-steered.
            logger.warning("Data Commons playbook unavailable: %s", client.redact(str(exc)))

    # _client before _tools: `loaded()` reads _tools, `call()` needs _client.
    _client, _playbook, _error = client, playbook, None
    _tools = tools
    logger.info(
        "Data Commons MCP connected (%s %s): %d tools, playbook %d chars",
        info.get("name", "?"),
        info.get("version", "?"),
        len(tools),
        len(playbook),
    )
    return True


def status() -> dict[str, Any]:
    return {
        "enabled": enabled(),
        "loaded": loaded(),
        "url": _env("DATACOMMONS_MCP_URL", DEFAULT_URL) or DEFAULT_URL,
        "tools": [TOOL_PREFIX + t["name"] for t in _tools],
        "playbook_chars": len(_playbook),
        "error": _error,
    }


def prompt_section() -> str:
    """Server playbook appended to the system prompt; empty when not loaded."""
    if not _playbook:
        return ""
    return (
        "\n\n---\n\n## Data Commons playbook (served by the MCP server)\n\n"
        "Follow this when using the `dc_` tools.\n\n" + _playbook
    )


def call(name: str, arguments: Optional[dict]) -> str:
    """Run one MCP tool; never raises -- errors go back to the model as text."""
    if _client is None:
        return "[Data Commons MCP error] not connected"
    real = name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name
    try:
        text = _client.call_tool(real, arguments or {})
    except MCPError as exc:
        logger.warning("dc tool %s failed: %s", real, _client.redact(str(exc)))
        return f"[Data Commons MCP error] {_client.redact(str(exc))}"
    if len(text) > RESULT_LIMIT:
        text = text[:RESULT_LIMIT] + f"\n\n[... {len(text) - RESULT_LIMIT} chars truncated ...]"
    return text


def _redact(text: str) -> str:
    key = _env("DC_API_KEY")
    return text.replace(key, "<DC_API_KEY>") if key else text


def _schema(tool: dict) -> dict:
    return tool.get("inputSchema") or {"type": "object", "properties": {}}


def _description(tool: dict) -> str:
    return (tool.get("description") or "").strip()[:1024] or f"Data Commons {tool['name']}"


# ---------------------------------------------------------------------------
# hermes backend
# ---------------------------------------------------------------------------


def register_hermes_tools() -> int:
    """
    Register the MCP tools in Hermes' global registry (toolset `datacommons`).

    Global scope on purpose: every per-user profile sees them. Returns how
    many were registered; 0 when Hermes or the MCP server is unavailable.
    """
    global _hermes_registered
    if _hermes_registered:
        return len(_tools)
    if not ensure_loaded():
        return 0
    try:
        from tools.registry import registry  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Hermes registry unavailable: %s", exc)
        return 0

    for tool in _tools:
        name = TOOL_PREFIX + tool["name"]
        registry.register(
            name=name,
            toolset=TOOLSET,
            schema={
                "name": name,
                "description": _description(tool),
                "parameters": _schema(tool),
            },
            handler=_hermes_handler(name),
            emoji="🌐",
        )
    _hermes_registered = True
    logger.info("Registered %d Data Commons tools in toolset %r", len(_tools), TOOLSET)
    return len(_tools)


def _hermes_handler(name: str):
    def handler(args: dict, **_kwargs: Any) -> str:
        return call(name, args)

    return handler


# ---------------------------------------------------------------------------
# hermes_lite backend
# ---------------------------------------------------------------------------


def as_langchain_tools() -> list:
    """Tools for hermes_lite. Never connects -- `ensure_loaded()` does that --
    because `/ready` lists tools through here."""
    if not _tools:
        return []
    from langchain_core.tools import StructuredTool

    out = []
    for tool in _tools:
        name = TOOL_PREFIX + tool["name"]
        out.append(
            StructuredTool.from_function(
                func=_langchain_func(name),
                name=name,
                description=_description(tool),
                # A JSON-schema dict: arguments reach the function as kwargs
                # without a pydantic model per tool.
                args_schema=_schema(tool),
            )
        )
    return out


def _langchain_func(name: str):
    def run(**kwargs: Any) -> str:
        return call(name, kwargs)

    return run


def _reset_for_tests() -> None:
    global _client, _tools, _playbook, _error, _last_attempt, _hermes_registered
    with _lock:
        _client, _tools, _playbook, _error = None, [], "", None
        _last_attempt, _hermes_registered = 0.0, False
