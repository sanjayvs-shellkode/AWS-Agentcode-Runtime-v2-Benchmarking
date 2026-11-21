from unittest.mock import MagicMock, patch

import weather


def _resp(data):
    m = MagicMock()
    m.json.return_value = data
    return m


GEO = {"results": [{"name": "Paris", "country": "France", "latitude": 48.8, "longitude": 2.3}]}
FC = {
    "timezone": "Europe/Paris",
    "current": {"time": "2026-11-21T11:30", "temperature_2m": 10.5, "relative_humidity_2m": 85,
                "wind_speed_10m": 6.6, "weather_code": 3},
}


def test_geocode_city():
    with patch("weather.requests.get", return_value=_resp(GEO)):
        assert weather.geocode_city("Paris")["latitude"] == 48.8


def test_unknown_city():
    with patch("weather.requests.get", return_value=_resp({})):
        assert "error" in weather.geocode_city("zzzz")


def test_get_forecast_names_units_and_conditions():
    with patch("weather.requests.get", return_value=_resp(FC)) as get:
        out = weather.get_forecast(48.8, 2.3)
    assert get.call_args.kwargs["params"]["timezone"] == "auto"
    assert out == {"local_time": "2026-11-21T11:30", "timezone": "Europe/Paris", "temperature_c": 10.5,
                   "relative_humidity_pct": 85, "wind_speed_kmh": 6.6, "conditions": "overcast"}


def test_convert_temperature():
    assert weather.convert_temperature(100, "celsius", "fahrenheit")["result"] == 212.0
    assert weather.convert_temperature(-40, "F", "c")["result"] == -40.0
    assert weather.convert_temperature(98.6, "Fahrenheit", "Celsius")["result"] == 37.0
    assert weather.convert_temperature(21, "c", "celsius")["result"] == 21
    assert "error" in weather.convert_temperature(300, "kelvin", "celsius")
