"""The transport decision and the auth guard must read the same setting."""
import importlib
import sys

import pytest


def _reload(monkeypatch, **env):
    for k in ("MCP_TRANSPORT", "MCP_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from mcp_lexoffice import config
    config.get_settings.cache_clear()
    sys.modules.pop("mcp_lexoffice.server", None)
    return importlib.import_module("mcp_lexoffice.server")


def test_unset_transport_without_key_runs_stdio_not_http(monkeypatch):
    server = _reload(monkeypatch)
    calls = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.update(kw))
    server.main()
    assert calls["transport"] == "stdio"


def test_http_without_key_refuses_to_start(monkeypatch):
    with pytest.raises(SystemExit):
        _reload(monkeypatch, MCP_TRANSPORT="streamable-http")


def test_http_with_key_runs_stateless_http(monkeypatch):
    server = _reload(monkeypatch, MCP_TRANSPORT="http", MCP_API_KEY="x" * 32)
    calls = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.update(kw))
    server.main()
    assert calls["transport"] == "streamable-http" and calls["stateless_http"] is True
