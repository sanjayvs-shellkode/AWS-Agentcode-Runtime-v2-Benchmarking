"""Lambda target for AgentCore Gateway. One function serves both weather tools."""
from weather import geocode_city, get_forecast


def handler(event, context):
    # Gateway names each tool "<target>___<tool>", so keep only the tool part.
    tool = context.client_context.custom["bedrockAgentCoreToolName"].split("___", 1)[1]
    if tool == "geocode_city":
        return geocode_city(str(event["city"]))
    if tool == "get_forecast":
        return get_forecast(float(event["latitude"]), float(event["longitude"]))
    raise ValueError(f"unknown tool: {tool}")
