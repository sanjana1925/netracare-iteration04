"""The AI parts of the stretch goals, with the REAL model. Nothing is faked, and each test uses as few turns (AI calls) as it can.

  progress      the streamed chat sends short progress sentences, then the same final answer
  choose        after a doctor's leave the patient is shown the proposed time and other choices, and picks one after a Confirm card
  staff         the Staff assistant reads what is waiting, and a decision it proposes waits for Confirm before anything changes
  staff book    the Staff assistant books for a patient (find patient, find a time, card, Confirm)
  supervisor    an information question goes to the information agent (it cannot act); an appointment question goes to the booking agent;
                a card created by the booking agent is answered with Confirm and the booking is made

Fixed clock: Monday 2 November 2026, 10:00. Leave L1 of data pack 4: Dr. Rao (D1), Tuesday 3 November, the whole day, moves Asha Verma (P01) and Rohan Gupta (P02).
"""
import json
import uuid

import pytest

from app import db
from helpers import login_doctor, login_patient, login_staff
from test_iteration4_stretch import flow  # noqa: F401  (a fixture: it also resets the leave workflows)

LEAVE_DAY = "2026-11-03"


def _chat(client, headers, path="/chat", **body):
    r = client.post(path, headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _rows(sql, *args):
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _leave(client):
    r = client.post("/doctor/leave", headers=login_doctor(client, "D1"), json={"date": LEAVE_DAY, "session": "FULL"})
    assert r.status_code == 201, r.text
    return {p["patient_id"]: p["rebooking_id"] for p in r.json()["rebookings"]}


# ------------------------------------------------------------------------------------------------- progress
def test_the_streamed_chat_sends_progress_sentences_then_the_final_answer(client4, patients):
    h = login_patient(client4, patients["P01"])
    events = []
    with client4.stream("POST", "/chat/stream", headers=h,
                        json={"thread_id": uuid.uuid4().hex, "message": "Find me a free retina time with Dr. Shah on Friday morning"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    steps = [e["text"] for e in events if e["type"] == "step"]
    finals = [e for e in events if e["type"] == "final"]
    assert len(finals) == 1 and events[-1]["type"] == "final", "exactly one final event, and it is the last"
    assert steps[0] == "Checking your message…"
    assert any("free" in s and "times" in s for s in steps), f"a progress sentence about free times was expected: {steps}"
    final = finals[0]
    assert final["kind"] == "answer" and "find_slots" in final["tool_calls"] and final["reply"].strip()
    assert set(final) >= {"kind", "reply", "sources", "emergency", "pending_action", "tool_calls"}      # the same answer /chat gives


# ------------------------------------------------------------------------------------------------- the patient chooses
def test_after_leave_the_patient_sees_the_choices_and_picks_one_with_confirm(flow, patients):
    ids = _leave(flow)
    h = login_patient(flow, patients["P01"])
    thread = uuid.uuid4().hex
    first = _chat(flow, h, thread_id=thread, message="Hello, was my appointment moved? What are my options?")
    assert "my_rebooking_options" in first["tool_calls"], first
    assert "4" in first["reply"] and "Nov" in first["reply"], "the proposed time (Wed 4 Nov) should be shown"
    assert _rows("SELECT status FROM rebookings WHERE rebooking_id=?", ids["P01"])[0]["status"] == "pending"

    second = _chat(flow, h, thread_id=thread, message="The proposed time is fine, please keep it.")
    assert second["kind"] == "pending" and second["pending_action"]["action"] == "choose_rebooking", second
    assert _rows("SELECT status FROM rebookings WHERE rebooking_id=?", ids["P01"])[0]["status"] == "pending", "nothing changes before Confirm"

    done = _chat(flow, h, thread_id=thread, confirm=True)
    assert done["kind"] == "answer"
    row = _rows("SELECT status, decided_by FROM rebookings WHERE rebooking_id=?", ids["P01"])[0]
    assert row == {"status": "approved", "decided_by": "patient"}
    booked = _rows("SELECT date, start_time FROM appointments WHERE patient_id='P01' AND status='booked' AND speciality='Cataract'")
    assert [(b["date"], b["start_time"]) for b in booked] == [("2026-11-04", "09:00")]


# ------------------------------------------------------------------------------------------------- the Staff assistant
def test_the_staff_assistant_reads_what_is_waiting_and_a_decision_waits_for_confirm(flow):
    ids = _leave(flow)
    staff = login_staff(flow)
    thread = uuid.uuid4().hex
    first = _chat(flow, staff, "/staff/chat", thread_id=thread, message="What needs attention right now?")
    assert first["kind"] == "answer" and "pending_work" in first["tool_calls"], first
    assert "Asha" in first["reply"] and "Rohan" in first["reply"], "both moved patients should be named"

    second = _chat(flow, staff, "/staff/chat", thread_id=thread, message="Approve the new time for Rohan Gupta.")
    assert second["kind"] == "pending" and second["pending_action"]["action"] == "decide_rebooking", second
    assert "Rohan" in second["pending_action"]["summary"]
    assert _rows("SELECT status FROM rebookings WHERE rebooking_id=?", ids["P02"])[0]["status"] == "pending", "nothing changes before Confirm"

    done = _chat(flow, staff, "/staff/chat", thread_id=thread, confirm=True)
    assert done["kind"] == "answer"
    assert _rows("SELECT status, decided_by FROM rebookings WHERE rebooking_id=?", ids["P02"])[0] == {"status": "approved", "decided_by": "Front Desk"}
    assert _rows("SELECT status FROM rebookings WHERE rebooking_id=?", ids["P01"])[0]["status"] == "pending", "only the one that was named"


def test_the_staff_assistant_books_for_a_patient(client4):
    staff = login_staff(client4)
    thread = uuid.uuid4().hex
    first = _chat(client4, staff, "/staff/chat", thread_id=thread,
                  message="Book Aditya Bose with Dr. Shah for a Retina check on Friday 6 November, the earliest morning time. "
                          "Reason: my optician asked for a retina check.")
    assert first["kind"] == "pending" and first["pending_action"]["action"] == "staff_book", first
    assert "Aditya Bose" in first["pending_action"]["summary"] and "Kiran Shah" in first["pending_action"]["summary"]
    assert _rows("SELECT * FROM appointments WHERE patient_id='P20' AND speciality='Retina' AND status='booked'") == []
    _chat(client4, staff, "/staff/chat", thread_id=thread, confirm=True)
    booked = _rows("SELECT doctor_id, date, start_time FROM appointments WHERE patient_id='P20' AND speciality='Retina' AND status='booked'")
    assert len(booked) == 1 and booked[0]["doctor_id"] == "D3" and booked[0]["date"] == "2026-11-06" and booked[0]["start_time"] < "13:00"


# ------------------------------------------------------------------------------------------------- the supervisor
def test_the_supervisor_sends_an_information_question_to_the_information_agent(client4, patients):
    h = login_patient(client4, patients["P01"])
    out = _chat(client4, h, thread_id=uuid.uuid4().hex, message="How much is a new consultation?", mode="supervisor")
    assert out["kind"] == "answer" and "800" in out["reply"], out
    assert "search_clinic_docs" in out["tool_calls"]
    assert not set(out["tool_calls"]) & {"my_appointments", "find_slots", "book_appointment", "cancel_appointment", "reschedule_appointment"}
    assert any("Consultation fees" in s for s in out["sources"]), "the sources still come from the code"


def test_the_supervisor_sends_an_appointment_question_to_the_booking_agent_and_a_card_is_answered_by_it(client4, patients):
    h = login_patient(client4, patients["P01"])
    thread = uuid.uuid4().hex
    mine = _chat(client4, h, thread_id=thread, message="What appointments do I have?", mode="supervisor")
    assert "my_appointments" in mine["tool_calls"], mine
    card = _chat(client4, h, thread_id=thread, mode="supervisor",
                 message="Book my retina follow-up with Dr. Shah this Friday morning, the earliest time. Reason: diabetic eye review")
    assert card["kind"] == "pending" and card["pending_action"]["action"] == "book", card
    assert _rows("SELECT * FROM appointments WHERE patient_id='P01' AND speciality='Retina' AND status='booked'") == []
    _chat(client4, h, thread_id=thread, confirm=True, mode="supervisor")
    booked = _rows("SELECT doctor_id, date FROM appointments WHERE patient_id='P01' AND speciality='Retina' AND status='booked'")
    assert len(booked) == 1 and booked[0]["doctor_id"] == "D3" and booked[0]["date"] == "2026-11-06"


def test_the_supervisor_still_stops_a_red_flag_before_anyone_answers(client4, patients):
    h = login_patient(client4, patients["P20"])
    out = _chat(client4, h, thread_id=uuid.uuid4().hex, message="Floor cleaner went in my eye this morning", mode="supervisor")
    assert out["kind"] == "emergency" and out["tool_calls"] == [], out
