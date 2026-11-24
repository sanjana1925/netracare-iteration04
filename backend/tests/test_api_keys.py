"""Code-logic tests for using more than one API key of the same provider (no model is called here: this is only the choosing of the key)."""
import itertools

from app import llm


def test_one_key_is_used_every_time(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key-a")
    for n in range(2, 10):
        monkeypatch.delenv(f"NVIDIA_API_KEY_{n}", raising=False)
    assert llm.api_keys("NVIDIA_API_KEY") == ["key-a"]
    assert {llm.next_api_key("NVIDIA_API_KEY") for _ in range(5)} == {"key-a"}


def test_several_keys_are_used_in_turn(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "key-a")
    monkeypatch.setenv("NVIDIA_API_KEY_2", "key-b")
    monkeypatch.setenv("NVIDIA_API_KEY_3", "  ")  # blank: ignored
    monkeypatch.setattr(llm, "_key_turn", itertools.count())
    assert llm.api_keys("NVIDIA_API_KEY") == ["key-a", "key-b"]
    assert [llm.next_api_key("NVIDIA_API_KEY") for _ in range(4)] == ["key-a", "key-b", "key-a", "key-b"]


def test_no_key_gives_an_empty_answer_and_the_safe_refusal(monkeypatch):
    for name in ["NVIDIA_API_KEY"] + [f"NVIDIA_API_KEY_{n}" for n in range(2, 10)]:
        monkeypatch.delenv(name, raising=False)
    assert llm.next_api_key("NVIDIA_API_KEY") == ""
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    try:
        llm.get_model(0)
        raised = False
    except llm.LLMUnavailable:
        raised = True
    assert raised


def test_a_429_is_retried_but_other_errors_are_not(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_WAITS", "0,0")
    calls = {"n": 0}

    def busy_twice():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Error code: 429 - Too Many Requests")
        return "ok"

    assert llm.invoke_with_backoff(busy_twice) == "ok" and calls["n"] == 3

    calls["n"] = 0

    def broken():
        calls["n"] += 1
        raise RuntimeError("Error code: 401 - unauthorized")

    try:
        llm.invoke_with_backoff(broken)
    except RuntimeError:
        pass
    assert calls["n"] == 1  # a bad key or a missing model is not retried


def test_a_short_provider_outage_502_is_retried_too(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_WAITS", "0,0")
    calls = {"n": 0}

    def bad_gateway_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Error code: 502 - {'title': 'Bad Gateway', 'detail': 'Upstream request failed.'}")
        return "ok"

    assert llm.invoke_with_backoff(bad_gateway_once) == "ok" and calls["n"] == 2


def test_a_dropped_connection_is_retried_but_a_timeout_is_not(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_WAITS", "0,0")
    calls = {"n": 0}

    def dropped_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Connection error.")
        return "ok"

    assert llm.invoke_with_backoff(dropped_once) == "ok" and calls["n"] == 2
    calls["n"] = 0

    def slow():
        calls["n"] += 1
        raise RuntimeError("Request timed out.")

    try:
        llm.invoke_with_backoff(slow)
    except RuntimeError:
        pass
    assert calls["n"] == 1
