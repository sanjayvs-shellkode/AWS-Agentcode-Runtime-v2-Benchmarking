"""MCP client for AgentCore Gateway. Each request is signed with the runtime's AWS credentials."""
import os
import re

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

_REGION_RE = re.compile(r"\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com")


class GatewayAuth(httpx.Auth):
    requires_request_body = True

    def __init__(self, region: str):
        credentials = Session().get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials found for signing Gateway requests")
        self._credentials = credentials.get_frozen_credentials()
        self._region = region

    def auth_flow(self, request: httpx.Request):
        signed = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={
                "host": request.url.host,
                "content-type": request.headers.get("content-type", ""),
            },
        )
        SigV4Auth(self._credentials, "bedrock-agentcore", self._region).add_auth(signed)
        request.headers.update(dict(signed.headers))
        yield request


def get_gateway_client() -> MCPClient:
    url = os.environ.get("GATEWAY_URL")
    if not url:
        raise RuntimeError("GATEWAY_URL is not set; the agent needs an AgentCore Gateway")
    match = _REGION_RE.search(url)
    if not match:
        raise RuntimeError(f"Cannot read the region from GATEWAY_URL: {url}")
    region = match.group(1)
    # A new auth object per session picks up refreshed credentials.
    return MCPClient(lambda: streamablehttp_client(url, auth=GatewayAuth(region)))
