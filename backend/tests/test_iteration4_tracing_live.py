"""The real LangSmith check: one chat turn is traced and the trace can be found in your LangSmith project.

It runs ONLY on purpose, because it needs your LangSmith key and it sends one chat turn (a handful of AI calls) to LangSmith:

    cd backend
    set NETRACARE_TEST_TRACING=1       (Windows)   /   NETRACARE_TEST_TRACING=1 python -m pytest ...   (Mac, Linux)
    python -m pytest tests/test_iteration4_tracing_live.py -v -s

It needs LANGSMITH_TRACING=true and LANGSMITH_API_KEY in backend/.env. Without them it is skipped.
"""
import os
import time
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

from helpers import login_patient

load_dotenv(Path(__file__).resolve().parent.parent / ".env")   # the skip condition below must see the settings in backend/.env

pytestmark = pytest.mark.skipif(
    os.environ.get("NETRACARE_TEST_TRACING") != "1" or not os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGSMITH_TRACING", "").lower() != "true",
    reason="needs NETRACARE_TEST_TRACING=1, LANGSMITH_TRACING=true and a LANGSMITH_API_KEY (it sends one chat turn to LangSmith)")


def test_one_chat_turn_appears_in_langsmith_as_one_trace_with_its_steps(client4, patients):
    from langsmith import Client
    from langchain_core.tracers.langchain import wait_for_all_tracers

    project = os.environ.get("LANGSMITH_PROJECT") or "default"
    marker = f"trace-check-{uuid.uuid4().hex[:8]}"
    started = time.time()
    r = client4.post("/chat", headers=login_patient(client4, patients["P01"]), json={"thread_id": marker, "message": "How much is a new consultation?"})
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    wait_for_all_tracers()                                  # the traces are uploaded in the background: wait until they have gone

    client = Client()
    root = None
    for _ in range(20):                                     # LangSmith needs a few seconds to show a new trace
        for run in client.list_runs(project_name=project, is_root=True, limit=20):
            meta = (run.extra or {}).get("metadata", {})
            if meta.get("app") == "netracare-iteration4" and run.start_time.timestamp() >= started - 5 and "patient-chat" in (run.tags or []):
                root = run
                break
        if root:
            break
        time.sleep(3)
    assert root is not None, "no NetraCare trace appeared in LangSmith"
    names = {x.name for x in client.list_runs(project_name=project, trace_id=root.trace_id)}
    assert {"redflag", "agent", "tools"} <= names, f"the trace should show the graph's steps; it shows {sorted(names)}"
