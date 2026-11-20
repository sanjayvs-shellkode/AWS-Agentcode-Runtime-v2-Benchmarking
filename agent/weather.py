"""Real weather lookup via Open-Meteo (no API key). Two calls, reused later by the Lambda."""
import requests

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 5


def geocode_city(city: str) -> dict:
    res = requests.get(GEO_URL, params={"name": city, "count": 1}, timeout=TIMEOUT)
    res.raise_for_status()
    results = res.json().get("results")
    if not results:
        return {"error": f"City not found: {city}"}
    p = results[0]
    return {"city": p["name"], "country": p.get("country"), "latitude": p["latitude"], "longitude": p["longitude"]}


def get_forecast(latitude: float, longitude: float) -> dict:
    res = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
        },
        timeout=TIMEOUT,
    )
    res.raise_for_status()
    data = res.json()
    return {"current": data["current"], "units": data["current_units"]}


def fetch_weather(city: str) -> dict:
    """Both steps in sequence; used by mode=direct."""
    place = geocode_city(city)
    if "error" in place:
        return place
    return {**place, **get_forecast(place["latitude"], place["longitude"])}
