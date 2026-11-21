"""Weather agent served by AgentCore Runtime.

Request payloads:
  {"prompt": "...", "user_id": "..."}  a Strands agent answers using the Gateway's weather tools;
                                       user_id is optional
  {"mode": "direct", "city": "..."}    benchmark path: the same Gateway tool calls, without the model

Conversation history is kept in AgentCore Memory, keyed by the runtime session ID and the user, so
follow-up questions in the same session have context.

Nothing is created at import time. On platform V2 the process is snapshotted once /ping passes, and
every session starts from that snapshot, so the model client, Memory client, MCP session and tool
list are built per request. That keeps credentials, connections and the Gateway's tool list fresh
in each session.
"""
import json
import os
from uuid import uuid4

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from gateway_client import get_gateway_client

DEFAULT_USER = "default-user"

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
