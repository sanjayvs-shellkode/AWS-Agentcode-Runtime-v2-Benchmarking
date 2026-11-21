"""Weather tools served by the Lambda behind AgentCore Gateway. Data comes from Open-Meteo (no API key)."""
import requests

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 5

# WMO weather interpretation codes, as used by Open-Meteo.
WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snowfall", 73: "moderate snowfall", 75: "heavy snowfall", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}

_UNITS = {"c": "celsius", "celsius": "celsius", "f": "fahrenheit", "fahrenheit": "fahrenheit"}


def geocode_city(city: str) -> dict:
    res = requests.get(GEO_URL, params={"name": city, "count": 1}, timeout=TIMEOUT)
    res.raise_for_status()
    results = res.json().get("results")
    if not results:
        return {"error": f"City not found: {city}"}
    p = results[0]
    return {"city": p["name"], "country": p.get("country"), "latitude": p["latitude"], "longitude": p["longitude"]}


def get_forecast(latitude: float, longitude: float) -> dict:
    """Current conditions. Times are local to the location (timezone=auto)."""
    res = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "timezone": "auto",
        },
        timeout=TIMEOUT,
    )
    res.raise_for_status()
    data = res.json()
    now = data["current"]
    code = now["weather_code"]
    return {
        "local_time": now["time"],
        "timezone": data.get("timezone"),
        "temperature_c": now["temperature_2m"],
        "relative_humidity_pct": now["relative_humidity_2m"],
        "wind_speed_kmh": now["wind_speed_10m"],
        "conditions": WMO_CODES.get(code, f"unknown (WMO code {code})"),
    }


def convert_temperature(value: float, from_unit: str, to_unit: str) -> dict:
    src = _UNITS.get(str(from_unit).strip().lower())
    dst = _UNITS.get(str(to_unit).strip().lower())
    if not src or not dst:
        return {"error": "from_unit and to_unit must be celsius or fahrenheit"}
    if src == dst:
        result = value
    elif src == "celsius":
        result = value * 9 / 5 + 32
    else:
        result = (value - 32) * 5 / 9
    return {"value": value, "from_unit": src, "to_unit": dst, "result": round(result, 2)}
