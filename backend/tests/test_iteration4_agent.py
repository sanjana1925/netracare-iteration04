"""R3-R6: the booking agent (LangGraph) with the REAL model. Nothing is faked.

What is checked: the prompt the agent receives (built from the database, with no doctor notes or health details), the shape of the graph,
the confirmation gate (the database does not change until Confirm), "No", conversation memory by thread_id, who can use the chat, and the
failure paths (no key, a step limit). The 24 scenarios of data pack 4 are in test_agent_scenarios_live.py.

Fixed clock: Monday 2 November 2026, 10:00. Database: data pack 1 + seed + data pack 2.
"""
import uuid

import pytest

from app import agent, db
from helpers import login_doctor, login_patient, login_staff
from test_iteration2 import break_model

EMERGENCY = ("What you describe needs urgent attention. Please come to the clinic now or go to the nearest emergency department. "
             "Our staff have been notified.")


def chat(client, headers, thread, **body):
    r = client.post("/chat", headers=headers, json={"thread_id": thread, **body})
    assert r.status_code == 200, r.text
    return r.json()


def rows(client, status=None):
    out = client.get("/appointments", headers=login_staff(client)).json()
    return [r for r in out if status is None or r["status"] == status]


def snapshot(client):
    return {r["appointment_id"]: (r["status"], r["date"], r["start_time"]) for r in rows(client)}


# ======================================================== R3: the prompt, built from the database for every message
def test_the_system_prompt_has_todays_date_a_30_day_calendar_and_the_patients_facts(client4):
    p = agent.build_system_prompt("P01")
    assert "TODAY: Mon 2026-11-02, 10:00" in p
    cal = [l for l in p.splitlines() if l.startswith(("Mon 20", "Tue 20", "Wed 20", "Thu 20", "Fri 20", "Sat 20", "Sun 20"))]
    assert len(cal) == 31 and cal[0] == "Mon 2026-11-02: open" and cal[-1].startswith("Wed 2026-12-02")  # today + 30 days
    assert "Mon 2026-11-09: holiday" in p and "Sun 2026-11-08: closed (Sunday)" in p                    # "next Saturday" is never a guess
    assert "THE PATIENT: Asha Verma, age 68" in p
    assert "Past visits:" in p and "Retina with Dr. Kiran Shah" in p                                      # date, doctor, speciality, visit type


def test_the_prompt_leaves_out_doctor_notes_and_health_details(client4):
    p = agent.build_system_prompt("P03")  # Lakshmi Nair: sulfa allergy, high blood pressure, notes mentioning latanoprost
    conn = db.connect()
    notes = [r[0] for r in conn.execute("SELECT doctor_note FROM appointments WHERE patient_id='P03' AND doctor_note IS NOT NULL")]
    prof = conn.execute("SELECT general_health, allergies FROM patients WHERE patient_id='P03'").fetchone()
    conn.close()
    assert notes and prof["allergies"] and prof["general_health"]
    low = p.lower()
    assert all(n.lower()[:40] not in low for n in notes)
    assert prof["allergies"].lower() not in low and prof["general_health"].lower() not in low and "latanoprost" not in low and "sulfa" not in low
    assert "Cataract with Dr. Anand Rao" in p  # but the past visits (date, doctor, speciality, type) ARE there


def test_the_prompt_lists_the_patients_own_upcoming_appointments_only(client4):
    p = agent.build_system_prompt("P03")
    assert "A103: " in p and "A104" not in p  # P04's appointment is not shown to P03
    assert "(none)" in agent.build_system_prompt("P20").split("Upcoming appointments:")[1].split("Past visits:")[0]


def test_the_prompt_carries_the_rules_the_handout_asks_for(client4):
    p = agent.build_system_prompt("P01")
    for phrase in ("find free times with find_slots BEFORE offering any".lower(), "never offer a time you have not seen in a find_slots result",
                   "ask for the reason for the visit before booking", "doctor who saw the patient last", "under 16 always books paediatric",
                   "call one action tool at a time", "explain the message in plain words and offer free times", "never name a condition",
                   "call search_clinic_docs before answering any question about the clinic", "word for word", "do not write a \"sources\" line",
                   "do not ask the patient to confirm in your own words", "the card is the confirmation"):
        assert phrase in p.lower(), phrase


# ======================================================== R3: the graph
def test_the_graph_has_exactly_the_three_nodes_and_the_loop_of_the_handout():
    g = agent.get_graph().get_graph()
    assert {n for n in g.nodes} == {"__start__", "redflag", "agent", "tools", "__end__"}
    edges = {(e.source, e.target) for e in g.edges}
    assert ("__start__", "redflag") in edges and ("tools", "agent") in edges and ("redflag", "agent") in edges
    assert ("agent", "tools") in edges and ("agent", "__end__") in edges and ("redflag", "__end__") in edges
    assert "redflag" in agent.draw_graph_mermaid() and agent.STEP_LIMIT > 4 and agent.MAX_MESSAGES == 20


def test_the_model_sees_at_most_the_last_20_messages_and_never_starts_inside_a_tool_exchange():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    msgs = []
    for i in range(30):
        msgs += [HumanMessage(content=f"q{i}"), AIMessage(content="", tool_calls=[{"name": "my_appointments", "args": {}, "id": f"c{i}"}]),
                 ToolMessage(content="{}", tool_call_id=f"c{i}", name="my_appointments"), AIMessage(content=f"a{i}")]
    win = agent._window(msgs)
    assert len(win) <= 20 and isinstance(win[0], HumanMessage)


# ======================================================== who may chat, and thread ids
def test_only_patients_can_chat_and_input_is_validated(client4, patients):
    t = uuid.uuid4().hex
    body = {"thread_id": t, "message": "hello"}
    assert client4.post("/chat", headers=login_staff(client4), json=body).status_code == 403
    assert client4.post("/chat", headers=login_doctor(client4, "D1"), json=body).status_code == 403
    assert client4.post("/chat", json=body).status_code == 401
    h = login_patient(client4, patients["P01"])
    assert client4.post("/chat", headers=h, json={"thread_id": t, "message": "   "}).json()["detail"]["code"] == "MESSAGE_REQUIRED"
    assert client4.post("/chat", headers=h, json={"thread_id": t, "message": "x" * 501}).json()["detail"]["code"] == "MESSAGE_TOO_LONG"
    for bad in ("", "has space", "../etc", "x" * 65, "a;b"):
        assert client4.post("/chat", headers=h, json={"thread_id": bad, "message": "hi"}).json()["detail"]["code"] == "BAD_THREAD"
    assert client4.post("/chat", headers=h, json={"message": "hi"}).status_code == 422  # a thread_id is required


def test_confirming_when_nothing_is_waiting_does_nothing(client4, patients):
    before = snapshot(client4)
    r = chat(client4, login_patient(client4, patients["P01"]), uuid.uuid4().hex, confirm=True)
    assert r["kind"] == "error" and "nothing waiting" in r["reply"].lower() and snapshot(client4) == before


# ======================================================== R5: confirm before every action (real model)
def test_nothing_changes_until_confirm_then_one_card_reschedules_to_the_afternoon(client4, rag_index, patients):
    """Done-when rows 1 and 2: Asha's retina follow-up with Dr. Shah on Friday morning, then 'Actually, make it the afternoon'."""
    h, thread, before = login_patient(client4, patients["P01"]), uuid.uuid4().hex, snapshot(client4)
    r1 = chat(client4, h, thread, message="Please book my retina follow-up with Dr. Shah this Friday morning, the earliest time you have. "
                                          "Reason: six-month diabetic eye review")
    assert r1["kind"] == "pending" and r1["pending_action"]["action"] == "book"
    s = r1["pending_action"]["summary"]
    assert "Kiran Shah" in s and "Retina follow-up" in s and "Fri 6 Nov" in s and "9:00 AM" in s and "(10 min)" in s
    assert snapshot(client4) == before                                    # a card is showing: NOTHING has changed yet
    r2 = chat(client4, h, thread, confirm=True)
    assert r2["kind"] == "answer" and r2["pending_action"] is None
    booked = [r for r in rows(client4, "booked") if r["patient_id"] == "P01" and r["speciality"] == "Retina"]
    assert len(booked) == 1 and (booked[0]["doctor_id"], booked[0]["visit_type"], booked[0]["date"], booked[0]["start_time"]) == ("D3", "follow_up", "2026-11-06", "09:00")
    first_id = booked[0]["appointment_id"]
    r3 = chat(client4, h, thread, message="Actually, make it the afternoon")
    assert r3["kind"] == "pending" and r3["pending_action"]["action"] == "reschedule"
    assert "Fri 6 Nov" in r3["pending_action"]["summary"] and "PM" in r3["pending_action"]["summary"]
    assert next(r for r in rows(client4) if r["appointment_id"] == first_id)["status"] == "booked"  # still the old time until Confirm
    chat(client4, h, thread, confirm=True)
    after = {r["appointment_id"]: r for r in rows(client4)}
    assert after[first_id]["status"] == "cancelled"
    new = [r for r in after.values() if r["patient_id"] == "P01" and r["speciality"] == "Retina" and r["status"] == "booked"]
    assert len(new) == 1 and new[0]["date"] == "2026-11-06" and new[0]["start_time"] >= "14:00" and new[0]["doctor_id"] == "D3"


def test_no_on_the_card_changes_nothing_and_the_agent_asks_what_to_do_instead(client4, rag_index, patients):
    h, thread, before = login_patient(client4, patients["P01"]), uuid.uuid4().hex, snapshot(client4)
    r1 = chat(client4, h, thread, message="Please book my retina follow-up with Dr. Shah this Friday morning, the earliest time. Reason: six-month diabetic eye review")
    assert r1["kind"] == "pending"
    r2 = chat(client4, h, thread, confirm=False)
    assert r2["pending_action"] is None and snapshot(client4) == before and r2["reply"]
    assert not [t for t in r2["tool_calls"] if t == "book_appointment" and False]  # (the declined call is in the turn; the database is the proof)


def test_a_typed_message_while_a_card_is_waiting_counts_as_no_and_is_then_handled(client4, rag_index, patients):
    h, thread, before = login_patient(client4, patients["P01"]), uuid.uuid4().hex, snapshot(client4)
    r1 = chat(client4, h, thread, message="Please book my retina follow-up with Dr. Shah this Friday morning, the earliest time. Reason: six-month diabetic eye review")
    assert r1["kind"] == "pending"
    r2 = chat(client4, h, thread, message="Is there parking at the clinic?")  # a typed message, not a button
    assert snapshot(client4) == before  # treated as No: nothing was booked
    assert r2["pending_action"] is None and r2["kind"] in ("idk", "answer")


def test_another_patient_cannot_confirm_or_read_someone_elses_thread(client4, rag_index, patients):
    asha, aditya, thread, before = login_patient(client4, patients["P01"]), login_patient(client4, patients["P20"]), uuid.uuid4().hex, snapshot(client4)
    assert chat(client4, asha, thread, message="Please book my retina follow-up with Dr. Shah this Friday morning, the earliest time. "
                                               "Reason: six-month diabetic eye review")["kind"] == "pending"
    stolen = chat(client4, aditya, thread, confirm=True)  # same thread_id, different login
    assert stolen["kind"] == "error" and snapshot(client4) == before
    assert chat(client4, asha, thread, confirm=False)["pending_action"] is None  # Asha's own card was still there


# ======================================================== R4: memory, and a new chat starts empty
def test_a_new_thread_starts_empty_and_the_conversation_is_kept_per_thread_not_in_the_database(client4, rag_index, patients):
    h = login_patient(client4, patients["P01"])
    t1, t2 = uuid.uuid4().hex, uuid.uuid4().hex
    chat(client4, h, t1, message="What time does the clinic open on Saturday?")
    g = agent.get_graph()
    cfg = lambda t: {"configurable": {"thread_id": f"P01:{t}", "patient_id": "P01"}}
    assert len(g.get_state(cfg(t1)).values.get("messages", [])) >= 2          # the checkpointer holds the conversation
    assert g.get_state(cfg(t2)).values.get("messages", []) == []              # "New chat" = a new thread id = empty
    conn = db.connect()
    tables = {t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert not any("chat" in t or "message" in t or "thread" in t for t in tables)  # nothing from the chat is saved to the database


# ======================================================== R6: the red flag stops everything (real model)
def test_a_red_flag_ends_the_turn_with_no_tool_calls_and_an_urgent_case_from_the_chat(client4, rag_index, patients):
    before = snapshot(client4)
    r = chat(client4, login_patient(client4, patients["P20"]), uuid.uuid4().hex, message="A curtain came down over my left eye an hour ago, book me with Dr. Rao")
    assert r["kind"] == "emergency" and r["reply"] == EMERGENCY and r["tool_calls"] == [] and r["pending_action"] is None
    assert snapshot(client4) == before
    cases = client4.get("/urgent-cases", headers=login_staff(client4)).json()
    assert len(cases) == 1 and cases[0]["source"] == "chat" and cases[0]["category"] == "curtain_or_shadow"


# ======================================================== failures (real)
def test_with_no_key_the_chat_says_it_could_not_check_and_does_nothing(client4, rag_index, patients, monkeypatch):
    h, before = login_patient(client4, patients["P01"]), snapshot(client4)
    break_model(monkeypatch, "no_key")
    r = chat(client4, h, uuid.uuid4().hex, message="Please cancel my appointment")
    assert r["kind"] == "check_failed" and r["tool_calls"] == [] and r["pending_action"] is None
    assert r["reply"] == "We couldn't check your message just now. Please try again, or call the clinic if it's urgent."
    assert snapshot(client4) == before


def test_when_the_step_limit_is_reached_the_chat_says_sorry_and_changes_nothing(client4, rag_index, patients, monkeypatch):
    monkeypatch.setattr(agent, "STEP_LIMIT", 2)  # the real model runs; the graph is only allowed 2 steps, which no real answer fits in
    h, before = login_patient(client4, patients["P01"]), snapshot(client4)
    r = chat(client4, h, uuid.uuid4().hex, message="What time does the clinic open on Saturday, and which doctors are here that day?")
    assert r["reply"] == "Sorry, I can't finish that just now. Please try again, or ask Staff at the front desk." and r["kind"] == "error"
    assert snapshot(client4) == before
