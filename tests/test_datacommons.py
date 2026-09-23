"""Data Commons MCP client and its wiring into the host — no network."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from agents import datacommons_mcp, datacommons_tools as dc
from agents.datacommons_mcp import DataCommonsMCP, MCPError, _parse_body

TOOLS = [
    {
        "name": "get_observations",
        "description": "Time series for a variable and a place.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "variable_dcid": {"type": "string"},
                "place_dcid": {"type": "string"},
            },
            "required": ["variable_dcid"],
        },
    },
]


class FakeServer:
    """Stands in for urllib.request.urlopen; records every request."""

    def __init__(self, *, sse: bool = True, expire_once: bool = False, down: bool = False):
        self.sse = sse
        self.expire_once = expire_once
        self.down = down
        self.requests: list[dict] = []

    def __call__(self, req, timeout=None):
        if self.down:
            raise urllib.error.URLError("connection refused")
        body = json.loads(req.data)
        headers = {k.lower(): v for k, v in req.header_items()}
        self.requests.append({"body": body, "headers": headers})
        method = body["method"]

        if self.expire_once and method == "tools/call" and headers.get("mcp-session-id") == "s1":
            self.expire_once = False
            raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, io.BytesIO(b"session"))

        session = "s2" if any(r["body"]["method"] == "initialize" for r in self.requests[:-1]) else "s1"
        if method == "initialize":
            result = {"serverInfo": {"name": "dc", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/read":
            result = {"contents": [{"text": "PLAYBOOK"}]}
        elif method == "tools/call":
            args = body["params"]["arguments"]
            result = {"content": [{"type": "text", "text": f"obs {args}"}]}
        else:
            result = {}
        envelope = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        raw = f"event: message\ndata: {envelope}\n\n" if self.sse else envelope
        return _Resp(raw, session if method == "initialize" else None)


class _Resp:
    def __init__(self, raw: str, session: str | None):
        self._raw = raw.encode()
        self.headers = {"Mcp-Session-Id": session} if session else {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("DC_API_KEY", "DATACOMMONS_MCP_URL", "DATACOMMONS_PLAYBOOK", "DATACOMMONS_RETRY_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    dc._reset_for_tests()
    yield
    dc._reset_for_tests()


def _serve(monkeypatch, server: FakeServer) -> FakeServer:
    monkeypatch.setattr(datacommons_mcp.urllib.request, "urlopen", server)
    return server


# --- client ----------------------------------------------------------------

def test_parse_body_plain_and_sse():
    assert _parse_body('{"result": 1}') == {"result": 1}
    sse = 'event: message\ndata: {"a": 1}\n\ndata: {"result": 2}\n'
    assert _parse_body(sse) == {"result": 2}
    with pytest.raises(MCPError):
        _parse_body("data: not json\n")


def test_key_goes_in_header_never_bearer(monkeypatch):
    server = _serve(monkeypatch, FakeServer())
    DataCommonsMCP(api_key="secret").list_tools()
    for req in server.requests:
        assert req["headers"]["x-api-key"] == "secret"
        assert "authorization" not in req["headers"]


def test_session_id_is_reused(monkeypatch):
    server = _serve(monkeypatch, FakeServer())
    client = DataCommonsMCP(api_key="k")
    client.call_tool("get_observations", {"variable_dcid": "Count_Person"})
    last = server.requests[-1]
    assert last["body"]["method"] == "tools/call"
    assert last["headers"]["mcp-session-id"] == "s1"


def test_expired_session_is_reinitialized_once(monkeypatch):
    server = _serve(monkeypatch, FakeServer(expire_once=True))
    client = DataCommonsMCP(api_key="k")
    assert client.call_tool("get_observations", {"variable_dcid": "X"}).startswith("obs")
    methods = [r["body"]["method"] for r in server.requests]
    assert methods.count("initialize") == 2


def test_tool_error_is_returned_as_text(monkeypatch):
    client = DataCommonsMCP(api_key="k")
    monkeypatch.setattr(client, "_rpc", lambda *a, **k: {"isError": True, "content": [{"type": "text", "text": "bad dcid"}]})
    assert client.call_tool("x", {}) == "[tool error] bad dcid"


def test_missing_key_is_refused():
    with pytest.raises(MCPError):
        DataCommonsMCP(api_key="")


# --- tools module ----------------------------------------------------------

def test_disabled_without_key(monkeypatch):
    _serve(monkeypatch, FakeServer())
    assert not dc.enabled()
    assert not dc.ensure_loaded()
    assert dc.as_langchain_tools() == []


def test_load_exposes_prefixed_langchain_tools(monkeypatch):
    monkeypatch.setenv("DC_API_KEY", "k")
    _serve(monkeypatch, FakeServer(sse=False))
    assert dc.ensure_loaded()

    tools = dc.as_langchain_tools()
    assert [t.name for t in tools] == ["dc_get_observations"]
    out = tools[0].invoke({"variable_dcid": "Count_Person", "place_dcid": "country/UZB"})
    assert "Count_Person" in out and "country/UZB" in out
    assert "PLAYBOOK" in dc.prompt_section()


def test_unreachable_server_backs_off(monkeypatch):
    monkeypatch.setenv("DC_API_KEY", "k")
    monkeypatch.setenv("DATACOMMONS_RETRY_SECONDS", "3600")
    server = _serve(monkeypatch, FakeServer(down=True))
    assert not dc.ensure_loaded()
    assert "unavailable" in dc.status()["error"]

    # Within the cooldown: no second attempt, even once the server is back.
    server.down = False
    assert not dc.ensure_loaded()
    assert server.requests == []


def test_errors_never_leak_the_key(monkeypatch):
    monkeypatch.setenv("DC_API_KEY", "supersecret")

    def boom(req, timeout=None):
        raise urllib.error.URLError(f"failed for {req.full_url}?key=supersecret")

    monkeypatch.setattr(datacommons_mcp.urllib.request, "urlopen", boom)
    assert not dc.ensure_loaded()
    assert "supersecret" not in dc.status()["error"]


def test_long_results_are_capped(monkeypatch):
    monkeypatch.setenv("DC_API_KEY", "k")
    _serve(monkeypatch, FakeServer())
    dc.ensure_loaded()
    monkeypatch.setattr(dc._client, "call_tool", lambda *a: "x" * (dc.RESULT_LIMIT + 50))
    assert dc.call("dc_get_observations", {}).endswith("50 chars truncated ...]")


# --- host wiring -------------------------------------------------------------

def test_host_gets_tools_toolset_and_playbook(monkeypatch):
    from agents.hermes_host import HermesHostService

    monkeypatch.setenv("DC_API_KEY", "k")
    monkeypatch.setenv("HERMES_ENABLED_TOOLSETS", "memory,todo")
    _serve(monkeypatch, FakeServer())

    host = HermesHostService()
    assert host._load_datacommons() is True
    assert "dc_get_observations" in [t.name for t in host._host_langchain_tools()]
    assert host._enabled_toolsets() == ["memory", "todo", "datacommons"]
    assert host.system_prompt.endswith("PLAYBOOK")
    # Second call changes nothing -> no needless hermes_lite rebuild.
    assert host._load_datacommons() is False


def test_host_without_key_is_unchanged(monkeypatch):
    from agents.hermes_host import HermesHostService

    monkeypatch.setenv("HERMES_ENABLED_TOOLSETS", "memory")
    host = HermesHostService()
    assert host._load_datacommons() is False
    assert host._enabled_toolsets() == ["memory"]
    assert host.readiness()["datacommons"]["enabled"] is False
