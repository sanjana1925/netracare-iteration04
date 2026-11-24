"""Stretch goals, plain-code parts (no AI model is called here, so no AI calls are spent):

  * doctor leave as a LangGraph workflow: it pauses for every decision, keeps its place in SQLite across a restart, and is not touched by a refused decision
  * the patient chooses a new time after doctor leave (the proposed time or two other offered times)

The AI parts of the same stretch goals (the agent's tools and prompt, the Staff assistant, the supervisor, the progress messages) are checked with
the real model in test_iteration4_stretch_live.py.

Fixed clock: Monday 2 November 2026, 10:00. Leave L1 of data pack 4: Dr. Rao (D1) on Tuesday 3 November, the whole day. That moves A101 (Asha Verma, P01,
Cataract 9:00) and A102 (Rohan Gupta, P02, General 9:20).
"""
import pytest

from app import db, leave_flow, ops, rules
from helpers import login_doctor, login_patient, login_staff

LEAVE_DAY = "2026-11-03"


@pytest.fixture()
def flow(client4):
    leave_flow.reset()
    yield client4
    leave_flow.reset()


def _start_leave(client):
    r = client.post("/doctor/leave", headers=login_doctor(client, "D1"), json={"date": LEAVE_DAY, "session": "FULL"})
    assert r.status_code == 201, r.text
    body = r.json()
    ids = {p["patient_id"]: p["rebooking_id"] for p in body["rebookings"]}
    return body["leave_id"], ids


def _progress(client, leave_id, headers=None):
    return client.get(f"/doctor/leave/{leave_id}/progress", headers=headers or login_doctor(client, "D1"))


# ------------------------------------------------------------------------------------------------- leave as a workflow
def test_leave_runs_as_a_workflow_that_waits_for_each_decision(flow):
    leave_id, ids = _start_leave(flow)
    assert set(ids) == {"P01", "P02"}
    p = _progress(flow, leave_id).json()
    assert p["finished"] is False and sorted(p["waiting_for"]) == sorted(ids.values()) and p["decided"] == []

    staff = login_staff(flow)
    assert flow.post(f"/staff/rebookings/{ids['P01']}/approve", headers=staff).status_code == 200
    p = _progress(flow, leave_id).json()
    assert p["finished"] is False and p["waiting_for"] == [ids["P02"]]
    assert p["decided"] == [{"rebooking_id": ids["P01"], "outcome": "approved"}]

    assert flow.post(f"/staff/rebookings/{ids['P02']}/reject", headers=staff).status_code == 200
    p = _progress(flow, leave_id).json()
    assert p["finished"] is True and p["waiting_for"] == []
    assert [d["outcome"] for d in p["decided"]] == ["approved", "rejected"]


def test_decisions_can_come_in_any_order(flow):
    leave_id, ids = _start_leave(flow)
    staff = login_staff(flow)
    assert flow.post(f"/staff/rebookings/{ids['P02']}/approve", headers=staff).status_code == 200   # the second one first
    assert _progress(flow, leave_id).json()["waiting_for"] == [ids["P01"]]
    assert flow.post(f"/staff/rebookings/{ids['P01']}/approve", headers=staff).status_code == 200
    assert _progress(flow, leave_id).json()["finished"] is True


def test_the_workflow_survives_a_server_restart(flow):
    leave_id, ids = _start_leave(flow)
    leave_flow.reset()                                  # "restart": every open workflow file is closed, the next call opens it again from disk
    p = _progress(flow, leave_id).json()
    assert p["finished"] is False and sorted(p["waiting_for"]) == sorted(ids.values())

    staff = login_staff(flow)
    assert flow.post(f"/staff/rebookings/{ids['P01']}/approve", headers=staff).status_code == 200   # resumes the graph saved before the restart
    leave_flow.reset()                                  # and another restart
    p = _progress(flow, leave_id).json()
    assert p["waiting_for"] == [ids["P02"]] and p["decided"][0]["rebooking_id"] == ids["P01"]
    assert flow.post(f"/staff/rebookings/{ids['P02']}/approve", headers=staff).status_code == 200
    assert _progress(flow, leave_id).json()["finished"] is True


def test_a_refused_decision_does_not_touch_the_workflow(flow):
    leave_id, ids = _start_leave(flow)
    staff = login_staff(flow)
    assert flow.post(f"/staff/rebookings/{ids['P01']}/approve", headers=staff).status_code == 200
    again = flow.post(f"/staff/rebookings/{ids['P01']}/approve", headers=staff)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "NOT_PENDING"
    p = _progress(flow, leave_id).json()
    assert p["waiting_for"] == [ids["P02"]] and len(p["decided"]) == 1


def test_only_the_right_people_can_see_a_leaves_progress(flow, patients):
    leave_id, _ = _start_leave(flow)
    assert _progress(flow, leave_id, login_staff(flow)).status_code == 200
    assert _progress(flow, leave_id, login_doctor(flow, "D2")).status_code == 404      # another doctor's leave
    assert _progress(flow, 9999).status_code == 404
    assert _progress(flow, leave_id, login_patient(flow, patients["P01"])).status_code == 403
    assert flow.get(f"/doctor/leave/{leave_id}/progress").status_code == 401


def test_a_refused_leave_starts_no_workflow(flow):
    r = flow.post("/doctor/leave", headers=login_doctor(flow, "D1"), json={"date": "2026-01-01", "session": "FULL"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "IN_PAST"


# ------------------------------------------------------------------------------------------------- the patient chooses a new time
def _conn():
    return db.connect()


def _options(patient_id):
    conn = _conn()
    try:
        return ops.rebooking_options(conn, patient_id)
    finally:
        conn.close()


def test_a_moved_patient_is_offered_the_proposed_time_and_two_others(flow):
    _start_leave(flow)
    [opt] = _options("P01")
    assert opt["speciality"] == "Cataract" and opt["why"].startswith("Moved because Dr. Anand Rao")
    assert opt["original"]["date"] == LEAVE_DAY
    assert (opt["proposed"]["doctor_id"], opt["proposed"]["date"], opt["proposed"]["start_time"]) == ("D1", "2026-11-04", "09:00")
    assert len(opt["other_choices"]) == 2
    times = [(o["date"], o["start_time"]) for o in opt["other_choices"]]
    assert times == sorted(times) and all(t > ("2026-11-04", "09:00") for t in times)
    assert all(o["doctor_id"] != "D4" for o in opt["other_choices"])          # only doctors who cover Cataract


def test_nothing_is_offered_to_a_patient_who_was_not_moved(flow):
    _start_leave(flow)
    assert _options("P03") == []


def test_choosing_the_proposed_time_approves_it(flow):
    leave_id, ids = _start_leave(flow)
    [opt] = _options("P01")
    p = opt["proposed"]
    conn = _conn()
    try:
        row = leave_flow.choose(conn, "P01", ids["P01"], p["doctor_id"], p["date"], p["start_time"])
    finally:
        conn.close()
    assert row["status"] == "approved" and row["decided_by"] == "patient"
    assert _options("P01") == []
    assert _progress(flow, leave_id).json()["decided"] == [{"rebooking_id": ids["P01"], "outcome": "chosen"}]


def test_choosing_another_offered_time_replaces_the_proposal_and_frees_its_slot(flow):
    leave_id, ids = _start_leave(flow)
    [opt] = _options("P01")
    other = opt["other_choices"][0]
    conn = _conn()
    try:
        row = leave_flow.choose(conn, "P01", ids["P01"], other["doctor_id"], other["date"], other["start_time"])
        assert row["status"] == "approved" and row["decided_by"] == "patient"
        assert (row["new_doctor_id"], row["new_date"], row["new_start"]) == (other["doctor_id"], other["date"], other["start_time"])
        mine = conn.execute("SELECT * FROM appointments WHERE patient_id='P01' AND status='booked' AND speciality='Cataract'").fetchall()
        assert len(mine) == 1 and (mine[0]["date"], mine[0]["start_time"]) == (other["date"], other["start_time"])
        assert mine[0]["moved_note"].startswith("Moved because Dr. Anand Rao")
        # the time that had been held for her is free again (any other patient could book it)
        assert opt["proposed"]["start_time"] in rules.free_slots(conn, opt["proposed"]["doctor_id"], "Cataract", "new", opt["proposed"]["date"])
        assert conn.execute("SELECT status FROM appointments WHERE appointment_id=?",
                            (row["proposed_appointment_id"],)).fetchone()["status"] == "booked"
    finally:
        conn.close()
    assert _progress(flow, leave_id).json()["decided"] == [{"rebooking_id": ids["P01"], "outcome": "chosen"}]


def test_a_time_that_was_not_offered_is_refused(flow):
    _, ids = _start_leave(flow)
    conn = _conn()
    try:
        with pytest.raises(Exception) as bad:
            leave_flow.choose(conn, "P01", ids["P01"], "D1", "2026-11-05", "17:50")
    finally:
        conn.close()
    assert getattr(bad.value, "code", "") == "NOT_OFFERED"
    assert [o["rebooking_id"] for o in _options("P01")] == [ids["P01"]]            # still waiting, nothing changed


def test_a_patient_cannot_choose_for_somebody_else(flow):
    _, ids = _start_leave(flow)
    [opt] = _options("P01")
    p = opt["proposed"]
    conn = _conn()
    try:
        with pytest.raises(Exception) as bad:
            leave_flow.choose(conn, "P02", ids["P01"], p["doctor_id"], p["date"], p["start_time"])   # P02 tries P01's moved appointment
    finally:
        conn.close()
    assert getattr(bad.value, "code", "") == "NOT_YOUR_REBOOKING"
    assert [o["rebooking_id"] for o in _options("P01")] == [ids["P01"]]
