from unittest.mock import patch, MagicMock

import weather


def _resp(data):
    m = MagicMock()
    m.json.return_value = data
    return m


GEO = {"results": [{"name": "Paris", "country": "France", "latitude": 48.8, "longitude": 2.3}]}
FC = {"current": {"temperature_2m": 15}, "current_units": {"temperature_2m": "°C"}}


def test_geocode_city():
    with patch("weather.requests.get", return_value=_resp(GEO)):
        assert weather.geocode_city("Paris")["latitude"] == 48.8


def test_get_forecast():
    with patch("weather.requests.get", return_value=_resp(FC)):
        assert weather.get_forecast(48.8, 2.3)["current"]["temperature_2m"] == 15


def test_fetch_weather_chains_both():
    with patch("weather.requests.get", side_effect=[_resp(GEO), _resp(FC)]):
        out = weather.fetch_weather("Paris")
    assert out["city"] == "Paris" and out["current"]["temperature_2m"] == 15


def test_unknown_city():
    with patch("weather.requests.get", return_value=_resp({})):
        assert "error" in weather.fetch_weather("zzzz")
