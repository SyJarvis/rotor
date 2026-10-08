import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.responses as endpoint
from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.responses import responses_required_capabilities
from rotor.core.exceptions import UpstreamProtocolError
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.responses import ResponsesRequest
from rotor.services.session_leases import SessionLeasePreference

from tests.test_openai_codex_adapter import channel


@pytest.mark.parametrize("fields", [{"store": True}, {"background": True},
                                     {"previous_response_id": "resp-old"}, {"conversation": "conv-old"}])
def test_persistence_selects_regular_responses_channel(fields):
    request = ResponsesRequest(model="model", input="hi", **fields)
    required = responses_required_capabilities(request)
    codex = channel(priority=100)
    ordinary = channel(id=2, type="openai", priority=1)
    assert RoutingEngine().route([codex, ordinary], "model", required_capabilities=required).candidates == [ordinary]


@pytest.mark.parametrize("operation", ["input_tokens", "compact"])
def test_codex_only_collection_resource_returns_400_before_upstream(monkeypatch, operation):
    monkeypatch.setattr(endpoint, "get_available_channels", AsyncMock(return_value=[channel()]))
    monkeypatch.setattr(endpoint, "routing_engine", RoutingEngine())
    client = Mock(side_effect=AssertionError("must not create an upstream client"))
    monkeypatch.setattr(endpoint, "AsyncClient", client)
    with pytest.raises(HTTPException) as captured:
        asyncio.run(endpoint._native_collection_request(
            body={"model": "model", "input": "hi"}, operation=operation,
            token=SimpleNamespace(id=1), db=AsyncMock(),
        ))
    assert captured.value.status_code == 400
    client.assert_not_called()


@pytest.mark.parametrize("operation,path", [("input_tokens", "/input_tokens"), ("compact", "/compact")])
def test_collection_resources_skip_codex_and_reach_regular_responses(monkeypatch, operation, path):
    codex = channel(priority=100)
    ordinary = channel(id=2, type="openai", key="api-key", base_url="https://ordinary.invalid/v1")
    monkeypatch.setattr(endpoint, "get_available_channels", AsyncMock(return_value=[codex, ordinary]))
    monkeypatch.setattr(endpoint, "routing_engine", RoutingEngine())
    captured = []

    def upstream(request):
        captured.append(request)
        return httpx.Response(200, json={"result": "supported"})

    monkeypatch.setattr(endpoint, "AsyncClient", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    response = asyncio.run(endpoint._native_collection_request(
        body={"model": "model", "input": "hi"}, operation=operation,
        token=SimpleNamespace(id=1), db=AsyncMock(),
    ))
    assert response.status_code == 200
    assert len(captured) == 1
    assert str(captured[0].url) == f"https://ordinary.invalid/v1/responses{path}"
    assert captured[0].headers["authorization"] == "Bearer api-key"
    assert json.loads(captured[0].content)["model"] == "provider-model"


def test_saved_codex_response_cannot_be_retrieved_or_deleted(monkeypatch):
    monkeypatch.setattr(endpoint, "get_response_route", AsyncMock(return_value=SimpleNamespace(channel_id=1)))
    db = AsyncMock()
    db.get.return_value = channel()
    with pytest.raises(HTTPException) as captured:
        asyncio.run(endpoint._resolve_response_channel(db, "resp-codex", SimpleNamespace(id=1, allowed_channels=None)))
    assert captured.value.status_code == 404


@pytest.mark.parametrize("fields", [{"store": True}, {"background": True}, {"conversation": "conv-old"},
                                     {"previous_response_id": "resp-codex"}])
def test_codex_only_stateful_creation_returns_400_before_upstream(monkeypatch, fields):
    config = channel()
    monkeypatch.setattr(endpoint, "get_available_channels", AsyncMock(return_value=[config]))
    monkeypatch.setattr(endpoint, "get_response_route", AsyncMock(return_value=SimpleNamespace(channel_id=1)))
    monkeypatch.setattr(endpoint, "get_session_lease_preference", AsyncMock(return_value=SessionLeasePreference(None)))
    monkeypatch.setattr(endpoint, "routing_engine", RoutingEngine())
    monkeypatch.setattr(endpoint, "accounting_service", SimpleNamespace(record_routing_decision=Mock()))
    client = Mock(side_effect=AssertionError("must not create an upstream client"))
    monkeypatch.setattr(endpoint, "AsyncClient", client)
    db = AsyncMock()
    db.get.return_value = config
    http_request = Request({"type": "http", "method": "POST", "path": "/v1/responses",
                            "headers": [], "client": ("127.0.0.1", 1)})
    with pytest.raises(HTTPException) as captured:
        asyncio.run(endpoint.create_response(
            ResponsesRequest(model="model", input="hi", **fields), http_request, Response(),
            db=db, token=SimpleNamespace(id=1, allowed_channels=None),
        ))
    assert captured.value.status_code == 400
    client.assert_not_called()


def test_adapter_resource_rejects_before_oauth_or_network():
    adapter = AdapterFactory.create_adapter(channel(), None)
    with pytest.raises(UpstreamProtocolError) as captured:
        asyncio.run(adapter.retrieve_response("resp-codex"))
    assert captured.value.status_code == 400
