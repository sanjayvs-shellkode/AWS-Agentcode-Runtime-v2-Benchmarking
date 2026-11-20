from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

import weather
from weather import fetch_weather

app = BedrockAgentCoreApp()


@tool
def geocode_city(city: str) -> dict:
    """Find the latitude and longitude of a city."""
    return weather.geocode_city(city)


@tool
def get_forecast(latitude: float, longitude: float) -> dict:
    """Get current weather for a latitude and longitude."""
    return weather.get_forecast(latitude, longitude)


# Created lazily so /ping passes before any model setup.
_agent = None


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent(
            system_prompt=(
                "You are a weather assistant. For any weather question, first call "
                "geocode_city, then call get_forecast with the returned coordinates."
            ),
            tools=[geocode_city, get_forecast],
        )
    return _agent


@app.entrypoint
def invoke(payload):
    # mode=direct skips the LLM and calls the tool, for the benchmark.
    city = payload.get("city") or payload.get("prompt", "")
    if payload.get("mode") == "direct":
        return fetch_weather(city)
    return {"result": str(_get_agent()(payload.get("prompt", "")))}


if __name__ == "__main__":
    app.run()
