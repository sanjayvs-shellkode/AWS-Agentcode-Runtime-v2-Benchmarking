from types import SimpleNamespace
from unittest.mock import patch

import requests

import handler


def _ctx(tool):
    custom = {"bedrockAgentCoreToolName": f"weather___{tool}"}
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


def test_routes_geocode():
    with patch("handler.geocode_city", return_value={"ok": 1}) as g:
        assert handler.handler({"city": "Paris"}, _ctx("geocode_city")) == {"ok": 1}
    g.assert_called_once_with("Paris")


def test_routes_forecast():
    with patch("handler.get_forecast", return_value={"ok": 2}) as f:
        assert handler.handler({"latitude": "48.8", "longitude": 2.3}, _ctx("get_forecast")) == {"ok": 2}
    f.assert_called_once_with(48.8, 2.3)


def test_routes_convert_temperature():
    event = {"value": "10", "from_unit": "celsius", "to_unit": "fahrenheit"}
    assert handler.handler(event, _ctx("convert_temperature"))["result"] == 50.0


def test_bad_arguments_return_error_to_model():
    out = handler.handler({"latitude": "north"}, _ctx("get_forecast"))
    assert out["error"].startswith("invalid arguments for get_forecast")


def test_weather_api_failure_returns_error():
    with patch("handler.geocode_city", side_effect=requests.Timeout("slow")):
        assert handler.handler({"city": "Paris"}, _ctx("geocode_city")) == {
            "error": "the weather service is unavailable right now"}
