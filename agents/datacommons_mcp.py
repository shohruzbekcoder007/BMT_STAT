"""
Minimal MCP client for the Data Commons MCP server (Streamable HTTP).

Neither backend speaks MCP on its own here: the Hermes backend only discovers
MCP servers from its own CLI/gateway entry points, and hermes_lite is plain
LangChain. So the protocol runs in-process -- `initialize` → `tools/list` →
`tools/call` -- over the standard library, and `agents/datacommons_tools.py`
exposes the result to both backends.

The server answers either plain JSON or SSE (`text/event-stream`); both are
handled. The API key travels in the `X-API-Key` header: Data Commons rejects
`Authorization: Bearer` with 401, and a `?key=` query string would end up in
logs.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("datacommons_mcp")

DEFAULT_URL = "https://api.datacommons.org/mcp"
PROTOCOL_VERSION = "2025-06-18"
# The server's tool descriptions require reading this playbook before the
# first call in a session; without it the model picks wrong parameters.
MAIN_PLAYBOOK_URI = "skill://data-commons-researcher/SKILL.md"


class MCPError(RuntimeError):
    """Transport or protocol failure talking to the MCP server."""


def _parse_body(raw: str) -> dict:
    """Parse a plain-JSON or SSE response body into the JSON-RPC envelope."""
    if "data:" not in raw:
        return json.loads(raw)
    # An SSE stream may carry several `data:` lines; the last complete JSON
    # object is the response.
    payload: Optional[dict] = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
    if payload is None:
        raise MCPError("could not extract JSON from the response")
    return payload


class DataCommonsMCP:
    """
    One logical session with the MCP server, shared by every request.

    Thread-safe: the session handshake is serialized, calls run concurrently.
    An expired session (HTTP 404 on a known session id) is re-initialized once
    transparently.
    """

    def __init__(
        self,
        api_key: str,
        url: str = DEFAULT_URL,
        timeout: float = 60.0,
        client_name: str = "unstat",
        client_version: str = "0.3.0",
    ) -> None:
        if not api_key:
            raise MCPError("DC_API_KEY is not set")
        self._api_key = api_key
        self.url = url
        self.timeout = timeout
        self._client_info = {"name": client_name, "version": client_version}
        self._session_id: Optional[str] = None
        self._initialized = False
        self._id = 0
        # Re-entrant: the handshake runs under the lock and posts requests,
        # which take it again for the request id.
        self._lock = threading.RLock()
        self.server_info: dict[str, Any] = {}

    # -- transport -----------------------------------------------------------

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _post(self, method: str, params: Optional[dict]) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-API-Key": self._api_key,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }).encode("utf-8")

        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            hint = " (invalid DC_API_KEY?)" if exc.code == 401 else ""
            raise _HTTPStatusError(exc.code, f"HTTP {exc.code}{hint}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MCPError(f"cannot reach {self.url}: {exc}") from exc

        payload = _parse_body(raw)
        if "error" in payload:
            err = payload["error"] or {}
            raise MCPError(f"{err.get('code')}: {err.get('message')}")
        return payload.get("result") or {}

    def _handshake(self) -> None:
        self._session_id = None
        result = self._post("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": self._client_info,
        })
        self.server_info = result.get("serverInfo") or {}
        # Streamable HTTP: the client confirms the handshake before any
        # other request. Servers that do not need it ignore it.
        try:
            self._post("notifications/initialized", None)
        except MCPError as exc:
            logger.debug("notifications/initialized ignored: %s", exc)
        self._initialized = True

    def connect(self) -> dict:
        with self._lock:
            if not self._initialized:
                self._handshake()
        return self.server_info

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        self.connect()
        try:
            return self._post(method, params)
        except _HTTPStatusError as exc:
            # 404 on a known session means the server dropped it.
            if exc.status != 404:
                raise
            logger.info("Data Commons MCP session expired; re-initializing")
            with self._lock:
                self._initialized = False
                self._handshake()
            return self._post(method, params)

    # -- MCP methods ---------------------------------------------------------

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: Optional[str] = None
        while True:
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def read_resource(self, uri: str) -> str:
        result = self._rpc("resources/read", {"uri": uri})
        return "".join(c.get("text", "") for c in result.get("contents") or [])

    def call_tool(self, name: str, arguments: Optional[dict]) -> str:
        """Call a tool and flatten its content blocks to text for the model."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})

        parts: list[str] = []
        for block in result.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        structured = result.get("structuredContent")
        if structured is not None and not parts:
            parts.append(json.dumps(structured, ensure_ascii=False))

        text = "\n".join(p for p in parts if p).strip()
        if result.get("isError"):
            return f"[tool error] {text or 'no details'}"
        return text or "(empty result)"

    def redact(self, text: str) -> str:
        """Strip the API key from text headed for logs or the model."""
        return text.replace(self._api_key, "<DC_API_KEY>") if self._api_key else text


class _HTTPStatusError(MCPError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
