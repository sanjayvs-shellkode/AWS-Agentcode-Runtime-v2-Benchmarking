from unittest.mock import patch

import httpx
import pytest
from botocore.credentials import Credentials

import gateway_client

URL = "https://abc123.gateway.bedrock-agentcore.ap-south-1.amazonaws.com/mcp"


def test_request_is_sigv4_signed_for_bedrock_agentcore():
    creds = Credentials("AKIDEXAMPLE", "secret")
    with patch("gateway_client.Session") as session:
        session.return_value.get_credentials.return_value = creds
        auth = gateway_client.GatewayAuth("ap-south-1")
    req = httpx.Request("POST", URL, content=b'{"jsonrpc":"2.0"}', headers={"content-type": "application/json"})
    signed = next(auth.auth_flow(req))
    scope = signed.headers["authorization"]
    assert scope.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/ap-south-1/bedrock-agentcore/aws4_request" in scope
    assert "x-amz-date" in signed.headers


def test_region_is_parsed_from_url(monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", URL)
    with patch("gateway_client.Session") as session, \
            patch("gateway_client.streamablehttp_client") as transport, \
            patch("gateway_client.MCPClient") as client:
        session.return_value.get_credentials.return_value = Credentials("AKIDEXAMPLE", "secret")
        gateway_client.get_gateway_client()
        transport_fn = client.call_args.args[0]
        transport_fn()
    assert transport.call_args.kwargs["auth"]._region == "ap-south-1"


def test_missing_gateway_url_fails_clearly(monkeypatch):
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    with pytest.raises(RuntimeError, match="GATEWAY_URL is not set"):
        gateway_client.get_gateway_client()
