"""Lambda target for AgentCore Gateway: one function serves the three weather tools."""
import logging

import requests

from weather import convert_temperature, geocode_city, get_forecast

log = logging.getLogger()
log.setLevel(logging.INFO)

TOOLS = {
    "geocode_city": lambda e: geocode_city(str(e["city"])),
    "get_forecast": lambda e: get_forecast(float(e["latitude"]), float(e["longitude"])),
    "convert_temperature": lambda e: convert_temperature(float(e["value"]), e["from_unit"], e["to_unit"]),
}


def handler(event, context):
    # Gateway names each tool "<target>___<tool>"; keep only the tool part.
    tool = context.client_context.custom["bedrockAgentCoreToolName"].split("___", 1)[1]
    if tool not in TOOLS:
        raise ValueError(f"unknown tool: {tool}")
    # Errors go back to the model as the tool result, so it can fix its arguments or tell the user.
    try:
        return TOOLS[tool](event)
    except (KeyError, TypeError, ValueError) as e:
        return {"error": f"invalid arguments for {tool}: {e!r}"}
    except requests.RequestException as e:
        log.warning("weather API call failed for %s: %s", tool, e)
        return {"error": "the weather service is unavailable right now"}
