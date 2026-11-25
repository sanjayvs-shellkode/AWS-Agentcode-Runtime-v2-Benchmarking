"""Weather agent served by AgentCore Runtime.

Request payloads:
  {"prompt": "...", "user_id": "..."}  a Strands agent answers using the Gateway's weather tools;
                                       user_id is optional
  {"mode": "direct", "city": "..."}    benchmark path: the same Gateway tool calls, without the model

Conversation history is kept in AgentCore Memory, keyed by the runtime session ID and the user, so
follow-up questions in the same session have context.

V2 Optimization:
  - /warmup endpoint implements memory and disk warmup phases
  - Memory phase: Pre-load boto3 client and import heavy modules into snapshot
  - Disk phase: Prime SSL certificates and HTTP connection paths
  - Credentials, MCP connections and tool lists are still fetched per request for safety

Constraints implemented for V2 snapshot safety:
  - Random values (uuid4) generated inside request handler
  - No timestamps at module level
  - Fresh credentials per request via GatewayAuth
  - Fresh tool list per request via list_tools_sync()
"""
import json
import os
from uuid import uuid4

import boto3
import requests as http_requests  # For warmup disk phase

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from gateway_client import get_gateway_client

DEFAULT_USER = "default-user"

# Global clients pre-loaded during warmup memory phase
# These survive the snapshot and are ready for all sessions
_bedrock_client = None
_warmup_complete = False

SYSTEM_PROMPT = """You are a weather assistant. Answer with live data from your tools:
- weather___geocode_city turns a city name into coordinates.
- weather___get_forecast returns current conditions for coordinates: temperature in Celsius, humidity,
  wind in km/h, sky conditions and the local time.
- weather___convert_temperature converts between Celsius and Fahrenheit.

Rules:
- For each city, call geocode_city, then get_forecast with the coordinates it returns. Never guess numbers.
- When the user wants Fahrenheit, or gives a temperature in Fahrenheit, convert it with convert_temperature
  instead of doing the math yourself.
- You only have current conditions. If asked about later days, say so and give the current conditions.
- If a tool returns an error, say briefly what failed and answer with what you have.
- Lead with the direct answer, then the key numbers with units. Keep it short."""

app = BedrockAgentCoreApp()


# =============================================================================
# WARMUP HOOK - V2 Optimization
# =============================================================================

async def warmup_handler(request):
    """
    Build warmup hook for V2 snapshot optimization.
    
    Called twice by AgentCore Runtime before taking the snapshot:
    1. Memory phase: Load heavy objects into memory (included in snapshot)
    2. Disk phase: Read files that sessions will need (primes OS cache)
    
    Both phases share a 120-second budget.
    """
    global _bedrock_client, _warmup_complete
    
    warmup_type = request.headers.get(
        "X-Amzn-Bedrock-AgentCore-Runtime-Warmup-Type", ""
    ).lower()
    
    if warmup_type == "memory":
        # =================================================================
        # MEMORY PHASE: Load objects that survive into the snapshot
        # =================================================================
        
        # Pre-create boto3 client - socket will reconnect after restore,
        # but client setup (endpoint resolution, etc.) is cached
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "ap-south-1")
        )
        
        # Pre-import heavy modules so they're in memory
        # (Already imported at top, but this ensures they're fully loaded)
        from strands import Agent
        from strands.models import BedrockModel
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
        
        # Import MCP client dependencies
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient
        
        _warmup_complete = True
        
        from starlette.responses import JSONResponse
        return JSONResponse({"status": "ok", "phase": "memory", "message": "Loaded boto3 client and modules"})
    
    elif warmup_type == "disk":
        # =================================================================
        # DISK PHASE: Read files that sessions will need
        # =================================================================
        
        # Make a test HTTPS request to prime:
        # - SSL certificates
        # - CA bundle files
        # - HTTP library native code
        try:
            http_requests.get("https://example.com", timeout=5)
        except Exception:
            pass  # Network issues during warmup are OK
        
        # Prime the Gateway connection path (but don't keep the connection)
        # This loads SSL certs for the Gateway endpoint
        gateway_url = os.environ.get("GATEWAY_URL", "")
        if gateway_url:
            try:
                # Just do a HEAD request to prime SSL
                http_requests.head(gateway_url.replace("/mcp", ""), timeout=5)
            except Exception:
                pass  # Gateway may not respond to HEAD, that's OK
        
        from starlette.responses import JSONResponse
        return JSONResponse({"status": "ok", "phase": "disk", "message": "Primed SSL and HTTP paths"})
    
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "phase": "unknown"})


# Register the warmup route
app.add_route("/warmup", warmup_handler, methods=["POST"])


def _tool_result(result: dict) -> dict:
    """Unwrap an MCP tool result into the Lambda's JSON payload."""
    if result.get("status") == "error":
        raise RuntimeError(f"Gateway tool failed: {result.get('content')}")
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def answer_direct(city: str) -> dict:
    with get_gateway_client() as mcp:
        def call(tool: str, args: dict) -> dict:
            return _tool_result(mcp.call_tool_sync(str(uuid4()), f"weather___{tool}", args))

        place = call("geocode_city", {"city": city})
        if "error" in place:
            return place
        return {**place, **call("get_forecast", {"latitude": place["latitude"], "longitude": place["longitude"]})}


def memory_session(session_id: str | None, user_id: str) -> AgentCoreMemorySessionManager | None:
    """Conversation history in AgentCore Memory for this session and user."""
    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id or not session_id:
        return None  # local runs without Memory: every request starts a new conversation
    config = AgentCoreMemoryConfig(memory_id=memory_id, session_id=session_id, actor_id=user_id)
    return AgentCoreMemorySessionManager(config, region_name=os.environ.get("AWS_REGION"))


def answer_with_agent(prompt: str, session: AgentCoreMemorySessionManager | None = None) -> dict:
    model_id = os.environ.get("MODEL_ID")
    if not model_id:
        raise RuntimeError("MODEL_ID is not set; it must name a Bedrock model or inference profile")
    # An inference-profile ARN carries its Region; a plain model ID uses the runtime's Region.
    region = model_id.split(":")[3] if model_id.startswith("arn:") else os.environ.get("AWS_REGION")
    with get_gateway_client() as mcp:
        agent = Agent(
            model=BedrockModel(model_id=model_id, region_name=region),
            system_prompt=SYSTEM_PROMPT,
            tools=mcp.list_tools_sync(),
            session_manager=session,  # loads earlier turns and saves this one
            callback_handler=None,  # no token streaming to stdout; spans record every step
        )
        return {"result": str(agent(prompt))}


@app.entrypoint
def invoke(payload: dict, context) -> dict:
    if payload.get("mode") == "direct":
        city = str(payload.get("city") or "").strip()
        return answer_direct(city) if city else {"error": "direct mode needs a 'city'"}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return {"error": "send a 'prompt' with your question"}
    user_id = str(payload.get("user_id") or DEFAULT_USER)
    return answer_with_agent(prompt, memory_session(context.session_id, user_id))


if __name__ == "__main__":
    app.run()
