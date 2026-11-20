import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# The build copies agent/weather.py next to handler.py; mirror that for local runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import handler  # noqa: E402


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
