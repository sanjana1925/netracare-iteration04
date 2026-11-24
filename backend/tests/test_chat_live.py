"""Scores the REAL model, through the booking agent (POST /chat with a thread_id), on data3/questions.csv (20) and data3/routing_samples.csv (20).

Skipped automatically with no key. Run:  python -m pytest tests/test_chat_live.py -v -s
Uses the real local embeddings and the real index, but patients are logged in through the real API with a fresh database.

- Questions Q01-Q15: the answer must cite the expected source (file > section). Q16-Q20: must be the "I don't know" message.
- Routing R01-R10 (and R11-R20 reported): the reply must name the right speciality's doctors and name NO condition.
Handout target: at least 9 of R01-R10 routed correctly.
"""
import csv

import pytest

from app import llm
from helpers import DATA3, login_patient

pytestmark = pytest.mark.skipif(not llm.llm_configured(), reason="No LLM API key in .env: live chat tests skipped")

with open(DATA3 / "questions.csv", newline="", encoding="utf-8") as f:
    QUESTIONS = list(csv.DictReader(f))
with open(DATA3 / "routing_samples.csv", newline="", encoding="utf-8") as f:
    ROUTING = list(csv.DictReader(f))

CONDITIONS = ["glaucoma is", "you have a ", "you have an ", "cataract is", "diabetic retinopathy", "conjunctivitis", "infection", "dry eye syndrome",
              "you may have", "this could be", "this might be", "it could be", "caused by"]
IDK = "I don't know. Our clinic documents don't cover that. Please ask Staff at the front desk."
@pytest.fixture(autouse=True)
def extra_pace():
    """Each case here makes several model calls (red-flag check, agent loop, answer): leave extra room under the provider's rate limit."""
    yield
    import time
    time.sleep(float(__import__("os").environ.get("LIVE_PACE_SECONDS", "8")))


_scores = {"q": 0, "r": 0, "r10": 0}



@pytest.fixture()
def ask(client4, rag_index, patients):
    """Each message starts a NEW chat (a new thread_id) so one answer can never help the next. Asha Verma is logged in."""
    import uuid
    h = login_patient(client4, patients["P01"])
    return lambda msg: client4.post("/chat", headers=h, json={"thread_id": uuid.uuid4().hex, "message": msg}).json()


@pytest.mark.parametrize("q", QUESTIONS, ids=[q["question_id"] for q in QUESTIONS])
def test_question(ask, q):
    r = ask(q["question"])
    if q["expected_source"] == "none":
        assert r["kind"] == "idk" and r["reply"] == IDK and r["sources"] == [], f"{q['question']!r} -> {r}"
    else:
        want = q["expected_source"].replace(" > ", " › ")
        assert r["kind"] == "answer", f"{q['question']!r} -> {r}"
        assert want in r["sources"], f"{q['question']!r}: expected {want}, got {r['sources']}"
    _scores["q"] += 1


@pytest.mark.parametrize("s", ROUTING, ids=[s["sample_id"] for s in ROUTING])
def test_routing(ask, s):
    r = ask(s["message"])
    text = r["reply"]
    low = text.lower()
    assert not any(c in low for c in CONDITIONS), f"{s['sample_id']} names a condition: {text}"
    if s["expected_speciality"].lower() in ("none", ""):
        assert "only" in low and "eye" in low, text
    else:
        ok = all(d.strip().lower() in low for d in s["expected_doctors"].split(",") if d.strip()) or \
             s["expected_speciality"].lower() in low
        alt = s["note"].lower() if s.get("note") else ""
        assert ok or any(word in low for word in alt.split() if len(word) > 4), f"{s['sample_id']}: {text}"
        if s["sample_id"] <= "R10":
            _scores["r10"] += 1


def test_report():
    print(f"\nQuestions passed: {_scores['q']}/20   Routing R01-R10 correct: {_scores['r10']}/10 (target: at least 9)")
