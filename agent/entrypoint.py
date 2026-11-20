import json
import os
from uuid import uuid4

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from gateway_client import get_gateway_client

# Application inference profile "runtime-v1-v2-benchmark" (Claude Haiku 4.5), set by the stack.
MODEL_ID = os.environ.get(
    "MODEL_ID",
    "arn:aws:bedrock:ap-south-1:106611079163:application-inference-profile/46u84t9z5klt",
)
MODEL_REGION = "ap-south-1"

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = (
    "You are a weather assistant. For any weather question, first call weather___geocode_city "
    "to get the coordinates, then call weather___get_forecast with them."
)


def _parse(result: dict) -> dict:
    if result.get("status") == "error":
        raise RuntimeError(f"Gateway tool failed: {result.get('content')}")
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _direct(city: str) -> dict:
    # Same two Gateway calls as the LLM path, without the model.
    with get_gateway_client() as mcp:
        place = _parse(mcp.call_tool_sync(str(uuid4()), "weather___geocode_city", {"city": city}))
        if "error" in place:
            return place
        forecast = _parse(mcp.call_tool_sync(
            str(uuid4()),
            "weather___get_forecast",
            {"latitude": place["latitude"], "longitude": place["longitude"]},
        ))
    return {**place, **forecast}


def _llm(prompt: str) -> dict:
    with get_gateway_client() as mcp:
        model = BedrockModel(model_id=MODEL_ID, region_name=MODEL_REGION)
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=mcp.list_tools_sync())
        return {"result": str(agent(prompt))}


@app.entrypoint
def invoke(payload):
    if payload.get("mode") == "direct":
        return _direct(payload.get("city") or payload.get("prompt", ""))
    return _llm(payload.get("prompt", ""))


if __name__ == "__main__":
    app.run()
