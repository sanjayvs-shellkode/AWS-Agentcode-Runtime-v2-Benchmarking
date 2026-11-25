from types import SimpleNamespace
from unittest.mock import patch

import pytest

import entrypoint

CTX = SimpleNamespace(session_id="0c7a3f1e-5b2d-4e8a-9f61-2d4b8c1e7a90")


def test_empty_requests_are_rejected_without_calling_the_gateway():
    assert entrypoint.invoke({"prompt": "   "}, CTX) == {"error": "send a 'prompt' with your question"}
    assert entrypoint.invoke({"mode": "direct"}, CTX) == {"error": "direct mode needs a 'city'"}


def test_missing_model_id_fails_clearly(monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_ID is not set"):
        entrypoint.answer_with_agent("weather in Paris?")


def test_tool_result_unwraps_json_and_raises_on_error():
    assert entrypoint._tool_result({"content": [{"text": '{"city": "Paris"}'}]}) == {"city": "Paris"}
    with pytest.raises(RuntimeError, match="Gateway tool failed"):
        entrypoint._tool_result({"status": "error", "content": "boom"})


def test_memory_is_keyed_by_runtime_session_and_user(monkeypatch):
    monkeypatch.setenv("MEMORY_ID", "wxbench_memory-abc")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    with patch("entrypoint.AgentCoreMemorySessionManager") as manager:
        entrypoint.memory_session(CTX.session_id, "sahana")
    config = manager.call_args.args[0]
    assert (config.memory_id, config.session_id, config.actor_id) == ("wxbench_memory-abc", CTX.session_id, "sahana")
    assert manager.call_args.kwargs["region_name"] == "ap-south-1"


def test_no_memory_without_memory_id_or_session(monkeypatch):
    monkeypatch.delenv("MEMORY_ID", raising=False)
    assert entrypoint.memory_session(CTX.session_id, "u") is None
    monkeypatch.setenv("MEMORY_ID", "wxbench_memory-abc")
    assert entrypoint.memory_session(None, "u") is None


def test_prompt_uses_default_user_when_none_given():
    with patch("entrypoint.memory_session", return_value=None) as mem, \
            patch("entrypoint.answer_with_agent", return_value={"result": "ok"}):
        entrypoint.invoke({"prompt": "Paris?"}, CTX)
    mem.assert_called_once_with(CTX.session_id, entrypoint.DEFAULT_USER)
