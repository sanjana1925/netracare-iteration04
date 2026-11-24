"""Plain-code parts of the other stretch goals (still no AI call is made here):
the progress sentences, the Staff assistant's tools and checks, and the supervisor's plumbing.
The AI parts are in test_iteration4_stretch_live.py."""
import json

from langchain_core.messages import AIMessage, HumanMessage

from app import agent, staff_agent, supervisor
from helpers import login_doctor, login_patient, login_staff
from test_iteration4_stretch import LEAVE_DAY, _conn, _progress, _start_leave, flow  # noqa: F401  (flow is a fixture)


def test_progress_sentences_name_the_doctor_and_carry_no_patient_data(client4):
    assert agent.progress_text("find_slots", {"doctor_id": "D3", "visit_type": "follow_up"}) == "Checking Dr. Kiran Shah's free times…"
    assert agent.progress_text("find_slots", {"speciality": "Retina"}) == "Checking free Retina times…"
    assert agent.progress_text("find_slots", {}) == "Checking free times…"
    assert agent.progress_text("search_clinic_docs", {"question": "my private worry"}) == "Looking in the clinic documents…"
    assert agent.progress_text("book_appointment", {"reason": "a secret"}) == "Getting your booking ready…"
    assert agent.progress_text("something_new", {}) == "Working on it…"


def test_the_chat_stream_checks_the_request_before_it_starts(client4, patients):
    h = login_patient(client4, patients["P01"])
    assert client4.post("/chat/stream", headers=h, json={"thread_id": "t1"}).status_code == 422                      # no message
    assert client4.post("/chat/stream", headers=h, json={"thread_id": "bad id!", "message": "hi"}).status_code == 422
    assert client4.post("/chat/stream", headers=h, json={"thread_id": "t1", "message": "x" * 501}).status_code == 422
    assert client4.post("/chat/stream", json={"thread_id": "t1", "message": "hi"}).status_code == 401
    assert client4.post("/chat/stream", headers=login_staff(client4), json={"thread_id": "t1", "message": "hi"}).status_code == 403


def test_only_staff_may_use_the_staff_assistant(client4, patients):
    body = {"thread_id": "t1", "message": "hello"}
    for path in ("/staff/chat", "/staff/chat/stream"):
        assert client4.post(path, headers=login_patient(client4, patients["P01"]), json=body).status_code == 403
        assert client4.post(path, headers=login_doctor(client4, "D1"), json=body).status_code == 403
        assert client4.post(path, json=body).status_code == 401
        assert client4.post(path, headers=login_staff(client4), json={"thread_id": "t1"}).status_code == 422             # no message


def test_the_chat_mode_must_be_single_or_supervisor(client4, patients):
    h = login_patient(client4, patients["P01"])
    assert client4.post("/chat", headers=h, json={"thread_id": "t1", "message": "hi", "mode": "nonsense"}).status_code == 422


def test_the_staff_assistant_has_its_own_tools_and_every_change_needs_confirm():
    names = {t.name for t in staff_agent.TOOLS}
    assert staff_agent.ACTION_TOOLS <= names
    assert {"pending_work", "find_patient", "day_appointments", "find_slots", "list_doctors"} <= names
    assert not names & {"book_appointment", "cancel_appointment", "reschedule_appointment", "choose_rebooking"}          # nothing of the patient's


def test_the_staff_prompt_has_the_day_and_the_counts_and_no_patient_notes(flow):
    _start_leave(flow)
    p = staff_agent.build_system_prompt("Front Desk")
    assert "Front Desk" in p and "Mon 2026-11-02" in p and "2 rebooking(s)" in p
    assert "doctor_note" not in p and "general_health" not in p


def test_finding_a_patient(client4):
    out = json.loads(staff_agent.find_patient.invoke({"query": "asha"}))
    assert out["result"] == "ok" and [p["patient_id"] for p in out["patients"]] == ["P01"]
    assert json.loads(staff_agent.find_patient.invoke({"query": "9000000001"}))["patients"][0]["name"] == "Asha Verma"
    assert json.loads(staff_agent.find_patient.invoke({"query": "zzzz"}))["result"] == "none"
    assert json.loads(staff_agent.find_patient.invoke({"query": "a"}))["result"] == "error"


def test_the_days_appointments_and_what_is_waiting(flow):
    day = json.loads(staff_agent.day_appointments.invoke({"date": LEAVE_DAY}))
    assert day["weekday"] == "Tue" and {"A101", "A102"} <= {a["appointment_id"] for a in day["appointments"]}
    assert json.loads(staff_agent.day_appointments.invoke({"date": "tomorrow"}))["result"] == "error"
    _start_leave(flow)
    work = json.loads(staff_agent.pending_work.invoke({}))
    assert len(work["rebookings"]) == 2 and {r["patient"] for r in work["rebookings"]} == {"Asha Verma", "Rohan Gupta"}
    assert all("Dr." in r["proposed"] for r in work["rebookings"])


def test_staff_decisions_after_confirm_use_the_same_functions_as_the_pages(flow):
    leave_id, ids = _start_leave(flow)
    conn = _conn()
    try:
        assert json.loads(staff_agent.do_decide_rebooking(conn, "Front Desk", ids["P01"], True))["result"] == "approved"
        again = json.loads(staff_agent.do_decide_rebooking(conn, "Front Desk", ids["P01"], True))
        assert again["result"] == "refused" and again["reason_code"] == "NOT_PENDING"                                   # a refusal is a normal result
        assert json.loads(staff_agent.do_decide_rebooking(conn, "Front Desk", ids["P02"], False))["result"] == "rejected"
    finally:
        conn.close()
    assert _progress(flow, leave_id).json()["finished"] is True                                                         # the leave workflow followed along


def test_staff_can_cancel_without_the_two_hour_rule_and_get_normal_refusals_for_nonsense(client4):
    conn = _conn()
    try:
        assert json.loads(staff_agent.do_cancel(conn, "A101"))["result"] == "cancelled"
        assert json.loads(staff_agent.do_cancel(conn, "A9999"))["result"] == "refused"
        assert json.loads(staff_agent.do_urgent_seen(conn, "Front Desk", 999))["result"] == "refused"
    finally:
        conn.close()


def test_the_supervisor_graph_and_what_each_agent_may_use():
    info = {t.name for t in supervisor.INFO_TOOLS}
    booking = {t.name for t in supervisor.BOOKING_TOOLS}
    assert info == {"search_clinic_docs", "list_doctors"}                          # the information agent can only look things up
    assert not info & {"book_appointment", "cancel_appointment", "reschedule_appointment", "choose_rebooking"}
    assert {"book_appointment", "cancel_appointment", "reschedule_appointment", "choose_rebooking", "find_slots"} <= booking
    nodes = set(supervisor.build_graph(None).get_graph().nodes)
    assert {"redflag", "supervisor", "info_agent", "info_tools", "booking_agent", "booking_tools"} <= nodes


def test_the_supervisor_sees_the_last_few_spoken_messages_only():
    msgs = [HumanMessage(content="Book me a retina check"), AIMessage(content="What is the reason for your visit?"), HumanMessage(content="eye review")]
    history, last = supervisor._history_text(msgs)
    assert last == "eye review" and "assistant: What is the reason" in history and "patient: Book me a retina check" in history
    assert supervisor._history_text([HumanMessage(content="hi")]) == ("(none)", "hi")


def test_the_two_graphs_keep_separate_conversations(client4):
    assert agent._cfg("P01", "t1", "single")["configurable"]["thread_id"] == "P01:t1"
    assert agent._cfg("P01", "t1", "supervisor")["configurable"]["thread_id"] == "sup:P01:t1"
    assert agent.get_graph("single") is not agent.get_graph("supervisor")
    assert agent.get_graph("supervisor") is agent.get_graph("supervisor")
