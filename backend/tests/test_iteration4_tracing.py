"""LangSmith tracing (stretch goal): the plain-code parts, with NO AI call and NO key needed.

Tracing is done by the LangChain / LangGraph libraries from environment settings. What NetraCare adds, and what is checked here:
  * it is OFF unless both LANGSMITH_TRACING=true and a LANGSMITH_API_KEY are set
  * the log sentence about it never contains the key
  * every chat turn carries labels (tags and metadata) so traces can be filtered, and the labels hold no patient name, number or message text
  * the tests themselves can never be traced by accident
The real check that a trace reaches LangSmith is test_iteration4_tracing_live.py (it needs your key and runs only on purpose).
"""
import os
from pathlib import Path

import pytest

from app import agent, llm, staff_agent

KEY_VALUE = "lsv2_pt_THIS_IS_A_MADE_UP_VALUE_0123456789"


@pytest.fixture()
def clean_env(monkeypatch):
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY", "LANGSMITH_PROJECT",
                 "LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_OUTPUTS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_tracing_is_off_by_default(clean_env):
    assert llm.tracing_enabled() is False
    assert llm.tracing_summary() == "LangSmith tracing is OFF."


def test_tracing_needs_both_the_switch_and_a_key(clean_env):
    clean_env.setenv("LANGSMITH_TRACING", "true")
    assert llm.tracing_enabled() is False                       # the switch alone is not enough
    clean_env.delenv("LANGSMITH_TRACING")
    clean_env.setenv("LANGSMITH_API_KEY", KEY_VALUE)
    assert llm.tracing_enabled() is False                       # a key alone is not enough
    clean_env.setenv("LANGSMITH_TRACING", "true")
    assert llm.tracing_enabled() is True
    clean_env.setenv("LANGSMITH_TRACING", "false")
    assert llm.tracing_enabled() is False


def test_the_older_langchain_setting_names_work_too(clean_env):
    clean_env.setenv("LANGCHAIN_TRACING_V2", "true")
    clean_env.setenv("LANGCHAIN_API_KEY", KEY_VALUE)
    assert llm.tracing_enabled() is True


def test_the_log_sentence_says_where_it_goes_and_never_shows_the_key(clean_env):
    clean_env.setenv("LANGSMITH_TRACING", "true")
    clean_env.setenv("LANGSMITH_API_KEY", KEY_VALUE)
    clean_env.setenv("LANGSMITH_PROJECT", "netracare-iteration4")
    said = llm.tracing_summary()
    assert "ON" in said and "netracare-iteration4" in said and "sent to LangSmith" in said
    assert KEY_VALUE not in said and "lsv2" not in said
    clean_env.setenv("LANGSMITH_HIDE_INPUTS", "true")
    clean_env.setenv("LANGSMITH_HIDE_OUTPUTS", "true")
    assert "hidden" in llm.tracing_summary()


def test_every_chat_turn_carries_labels_with_no_patient_data():
    patient = agent._cfg("P01", "thread-1", "supervisor")
    staff = staff_agent._cfg("Front Desk", "thread-2")
    assert patient["tags"] == ["netracare", "patient-chat"] and patient["metadata"] == {"app": "netracare-iteration4", "chat": "patient", "mode": "supervisor"}
    assert staff["tags"] == ["netracare", "staff-chat"] and staff["metadata"]["chat"] == "staff"
    for cfg in (patient, staff):
        labels = " ".join(cfg["tags"]) + " " + str(cfg["metadata"])
        assert "P01" not in labels and "Front Desk" not in labels and "thread" not in labels


def test_the_tests_can_never_be_traced_by_accident():
    assert os.environ.get("NETRACARE_TEST_TRACING") == "1" or os.environ.get("LANGSMITH_TRACING") == "false"


def test_the_env_example_explains_tracing_and_holds_no_real_key():
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "LANGSMITH_TRACING" in text and "LANGSMITH_API_KEY" in text and "LANGSMITH_HIDE_INPUTS" in text
    assert "lsv2_..." in text and "lsv2_pt_" not in text
