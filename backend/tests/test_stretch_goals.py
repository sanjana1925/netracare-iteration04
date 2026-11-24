"""The six stretch goals of the Iteration 1 handout (section 11). Data: data pack 1 only, on the fixed clock Mon 2 Nov 2026, 10:00.

1. Reschedule                      -> also covered by 3 cases of rule_tests.csv (T36-T38) in test_rule_cases.py
2. Staff approval queue            -> tests below
3. Automated rule tests            -> test_rule_cases.py runs ALL 38 cases of stretch/rule_tests.csv on stretch/appointments_seed.csv
4. Staff add holidays / edit the weekly schedule -> tests below
5. Week view calendar per doctor   -> tests below
6. Docker Compose                  -> docker-compose.yml (checked by running it; see README)
"""
import pytest

from helpers import login_doctor, login_patient, login_staff

NEW = {"doctor_id": "D3", "speciality": "Retina", "visit_type": "new", "date": "2026-11-04", "start_time": "10:00", "reason": "Routine eye check"}


def code(r):
    return r.json()["detail"]["code"]


# =============================== 1. Reschedule ===============================
def test_reschedule_is_cancel_plus_new_booking_and_a_refusal_keeps_the_original(client, patients):
    p5 = login_patient(client, patients["P05"])  # Priya Menon, A105: Dr. Iyer Glaucoma Wed 4 Nov 10:00
    ok = client.post("/appointments/A105/reschedule", headers=p5,
                     json={"doctor_id": "D2", "speciality": "Glaucoma", "visit_type": "new", "date": "2026-11-06", "start_time": "10:00"})
    assert ok.status_code == 200 and ok.json()["date"] == "2026-11-06"
    new_id = ok.json()["appointment_id"]
    refused = client.post(f"/appointments/{new_id}/reschedule", headers=p5,
                          json={"doctor_id": "D2", "speciality": "Glaucoma", "visit_type": "new", "date": "2026-11-05", "start_time": "10:00"})
    assert code(refused) == "DOCTOR_OFF"  # Dr. Iyer is off on Thursdays
    still = [a for a in client.get("/appointments?status=booked", headers=p5).json() if a["appointment_id"] == new_id]
    assert len(still) == 1  # the original stays exactly as it was


# =============================== 2. Staff approval queue ===============================
def test_late_cancel_becomes_a_request_and_the_appointment_stays_booked(client, patients):
    p3 = login_patient(client, patients["P03"])  # A103 starts 11:00, an hour from now
    r = client.post("/appointments/A103/cancel", headers=p3)
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "INSIDE_2_HOURS" and d["to_staff"] and d["approval_id"] and "Please contact Staff" in d["message"]
    mine = [a for a in client.get("/appointments", headers=p3).json() if a["appointment_id"] == "A103"][0]
    assert mine["status"] == "booked" and mine["approval_status"] == "pending" and mine["approval_action"] == "cancel"
    queue = client.get("/staff/approvals", headers=login_staff(client)).json()
    assert len(queue) == 1 and queue[0]["patient_name"] == "Lakshmi Nair" and queue[0]["action"] == "cancel"


def test_staff_approve_cancels_and_reject_keeps(client, patients):
    p3, p4, staff = login_patient(client, patients["P03"]), login_patient(client, patients["P03"]), login_staff(client)
    approve_id = client.post("/appointments/A103/cancel", headers=p3).json()["detail"]["approval_id"]
    done = client.post(f"/staff/approvals/{approve_id}/approve", headers=staff).json()
    assert done["status"] == "approved" and done["decided_by"] == "Front Desk"
    assert [a for a in client.get("/appointments", headers=p3).json() if a["appointment_id"] == "A103"][0]["status"] == "cancelled"
    assert client.get("/staff/approvals", headers=staff).json() == []
    # a second request on a different appointment, rejected
    p4 = login_patient(client, patients["P04"])
    # A104 starts at 12:30 (2.5 h away), so a patient can cancel it directly; move the clock-sensitive case to a booked one instead:
    r = client.post("/appointments/A104/cancel", headers=p4)
    assert r.status_code == 200  # more than 2 hours away: cancelled at once, no request needed


def test_staff_reject_leaves_the_appointment_booked(client, patients):
    p3, staff = login_patient(client, patients["P03"]), login_staff(client)
    rid = client.post("/appointments/A103/cancel", headers=p3).json()["detail"]["approval_id"]
    assert client.post(f"/staff/approvals/{rid}/reject", headers=staff).json()["status"] == "rejected"
    mine = [a for a in client.get("/appointments", headers=p3).json() if a["appointment_id"] == "A103"][0]
    assert mine["status"] == "booked" and mine["approval_status"] == "rejected"
    assert code(client.post(f"/staff/approvals/{rid}/approve", headers=staff)) == "NOT_PENDING"


def test_late_reschedule_also_goes_to_staff_and_approve_moves_it(client, patients):
    p3, staff = login_patient(client, patients["P03"]), login_staff(client)
    r = client.post("/appointments/A103/reschedule", headers=p3, json=NEW)  # T37 of rule_tests.csv: INSIDE_2_HOURS
    assert code(r) == "INSIDE_2_HOURS" and r.json()["detail"]["approval_id"]
    assert client.post(f"/staff/approvals/{r.json()['detail']['approval_id']}/approve", headers=staff).json()["status"] == "approved"
    booked = client.get("/appointments?status=booked", headers=p3).json()
    assert [(a["date"], a["start_time"]) for a in booked if a["speciality"] == "Retina"] == [("2026-11-04", "10:00")]


def test_duplicate_request_and_impossible_change_are_not_queued(client, patients):
    p3, staff = login_patient(client, patients["P03"]), login_staff(client)
    client.post("/appointments/A103/cancel", headers=p3)
    assert code(client.post("/appointments/A103/cancel", headers=p3)) == "REQUEST_PENDING"
    sunday = {**NEW, "date": "2026-11-08"}
    p5 = login_patient(client, patients["P06"])
    assert code(client.post("/appointments/A106/reschedule", headers=p5, json={**sunday, "doctor_id": "D4", "speciality": "Dry eye"})) in (
        "CLINIC_CLOSED", "NOT_YOUR_APPOINTMENT", "REQUEST_PENDING")
    assert len(client.get("/staff/approvals", headers=staff).json()) == 1


def test_approvals_are_staff_only(client, patients):
    p3 = login_patient(client, patients["P03"])
    aid = client.post("/appointments/A103/cancel", headers=p3).json()["detail"]["approval_id"]
    for h in (p3, login_doctor(client, "D3")):
        assert client.get("/staff/approvals", headers=h).status_code == 403
        assert client.post(f"/staff/approvals/{aid}/approve", headers=h).status_code == 403
        assert client.post(f"/staff/approvals/{aid}/reject", headers=h).status_code == 403
    assert client.get("/staff/approvals").status_code == 401


# =============================== 4. Staff add holidays / edit the weekly schedule ===============================
def test_staff_adds_a_holiday_and_it_takes_effect_at_once(client, patients):
    staff, p = login_staff(client), login_patient(client, patients["P17"])
    q = "/slots?doctor_id=D1&date=2026-11-13&speciality=General&visit_type=new"  # Fri 13 Nov: no bookings that day in the seed
    assert client.get(q, headers=p).json()["slots"]
    r = client.post("/holidays", headers=staff, json={"date": "2026-11-13", "name": "Staff training"})
    assert r.status_code == 201
    assert client.get(q, headers=p).json()["slots"] == []  # no slots on the new holiday
    assert code(client.post("/appointments", headers=p, json={**NEW, "doctor_id": "D1", "speciality": "General", "date": "2026-11-13"})) == "HOLIDAY"
    assert any(h["date"] == "2026-11-13" for h in client.get("/holidays", headers=p).json())
    assert client.delete("/holidays/2026-11-13", headers=staff).status_code == 200  # and removing it reopens the day
    assert client.get(q, headers=p).json()["slots"]


def test_holiday_validation_and_roles(client, patients):
    staff = login_staff(client)
    assert code(client.post("/holidays", headers=staff, json={"date": "2026-11-09", "name": "Again"})) == "HOLIDAY_EXISTS"  # in holidays.csv
    assert code(client.post("/holidays", headers=staff, json={"date": "2026-11-01", "name": "Past"})) == "IN_PAST"
    assert code(client.post("/holidays", headers=staff, json={"date": "2026-11-13", "name": "  "})) == "NAME_REQUIRED"
    assert code(client.post("/holidays", headers=staff, json={"date": "2026-11-03", "name": "Has bookings"})) == "HOLIDAY_HAS_BOOKINGS"  # A101/A102
    assert code(client.post("/holidays", headers=staff, json={"date": "2026-11-12", "name": "Has bookings"})) == "HOLIDAY_HAS_BOOKINGS"  # A110
    assert code(client.delete("/holidays/2026-11-20", headers=staff)) == "NOT_FOUND"
    for h in (login_patient(client, patients["P01"]), login_doctor(client, "D1")):
        assert client.post("/holidays", headers=h, json={"date": "2026-11-13", "name": "x"}).status_code == 403
        assert client.delete("/holidays/2026-11-09", headers=h).status_code == 403


def test_staff_edits_a_doctors_weekly_schedule_and_it_takes_effect_at_once(client, patients):
    staff, p = login_staff(client), login_patient(client, patients["P17"])
    q = "/slots?doctor_id=D2&date=2026-11-12&speciality=Glaucoma&visit_type=new"  # Thu 12 Nov: Dr. Iyer is OFF
    assert client.get(q, headers=p).json()["slots"] == []
    r = client.put("/schedule", headers=staff, json={"doctor_id": "D2", "day": "Thu", "session": "AM", "status": "OPD"})
    assert r.status_code == 200
    slots = client.get(q, headers=p).json()["slots"]
    assert slots and max(slots) < "13:00"  # morning now open, afternoon still off
    assert {(s["day"], s["session"], s["status"]) for s in client.get("/schedule", headers=p).json() if s["doctor_id"] == "D2" and s["day"] == "Thu"} == {
        ("Thu", "AM", "OPD"), ("Thu", "PM", "OFF")}
    # and back: set it to SURGERY
    assert client.put("/schedule", headers=staff, json={"doctor_id": "D2", "day": "Thu", "session": "AM", "status": "SURGERY"}).status_code == 200
    assert code(client.post("/appointments", headers=p, json={**NEW, "doctor_id": "D2", "speciality": "Glaucoma", "date": "2026-11-12"})) == "SURGERY_SESSION"


def test_a_schedule_change_that_would_strand_booked_appointments_is_refused(client):
    staff = login_staff(client)
    # Dr. Rao (D1) has A101/A102 on Tuesday 3 Nov in the morning
    r = client.put("/schedule", headers=staff, json={"doctor_id": "D1", "day": "Tue", "session": "AM", "status": "OFF"})
    assert code(r) == "SCHEDULE_HAS_BOOKINGS" and "Cancel or move them first" in r.json()["detail"]["message"]
    # A109 (Dr. Rao, Tuesday 10 Nov, 15:00) also protects the Tuesday afternoon; Saturday afternoon has nothing booked, so it is allowed
    assert code(client.put("/schedule", headers=staff, json={"doctor_id": "D1", "day": "Tue", "session": "PM", "status": "OFF"})) == "SCHEDULE_HAS_BOOKINGS"
    assert client.put("/schedule", headers=staff, json={"doctor_id": "D1", "day": "Sat", "session": "PM", "status": "OFF"}).status_code == 200


def test_schedule_validation_and_roles(client, patients):
    staff = login_staff(client)
    assert client.put("/schedule", headers=staff, json={"doctor_id": "D1", "day": "Sun", "session": "AM", "status": "OPD"}).status_code == 422
    assert client.put("/schedule", headers=staff, json={"doctor_id": "D1", "day": "Mon", "session": "AM", "status": "HOLIDAY"}).status_code == 422
    assert code(client.put("/schedule", headers=staff, json={"doctor_id": "D9", "day": "Mon", "session": "AM", "status": "OPD"})) == "DOCTOR_NOT_FOUND"
    for h in (login_patient(client, patients["P01"]), login_doctor(client, "D1")):
        assert client.put("/schedule", headers=h, json={"doctor_id": "D1", "day": "Mon", "session": "AM", "status": "OFF"}).status_code == 403


# =============================== 5. Week view calendar per doctor ===============================
def test_week_calendar_for_a_doctor(client):
    staff = login_staff(client)
    w = client.get("/calendar?doctor_id=D1&date=2026-11-04", headers=staff).json()  # any date in the week: Wed 4 Nov
    assert (w["doctor"], w["week_start"], w["prev_week"], w["next_week"]) == ("Dr. Anand Rao", "2026-11-02", "2026-10-26", "2026-11-09")
    assert [d["weekday"] for d in w["days"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    by = {d["weekday"]: d for d in w["days"]}
    assert by["Thu"]["sessions"] == {"AM": "SURGERY", "PM": "OPD"}  # Dr. Rao operates on Thursday mornings
    assert [(a["start_time"], a["patient_name"]) for a in by["Tue"]["appointments"]] == [("09:00", "Asha Verma"), ("09:20", "Rohan Gupta")]
    nxt = client.get(f"/calendar?doctor_id=D1&date={w['next_week']}", headers=staff).json()
    assert {d["weekday"]: d for d in nxt["days"]}["Mon"]["holiday"] == "Clinic holiday (Diwali week)"  # 9 Nov, from holidays.csv


def test_calendar_roles_a_doctor_sees_only_their_own_week(client, patients):
    doc = login_doctor(client, "D3")
    w = client.get("/calendar?doctor_id=D1&date=2026-11-04", headers=doc).json()  # asks for Dr. Rao's week...
    assert w["doctor_id"] == "D3"  # ...gets their own
    assert client.get("/calendar?doctor_id=D1", headers=login_patient(client, patients["P01"])).status_code == 403
    assert client.get("/calendar?doctor_id=D1").status_code == 401
    assert code(client.get("/calendar", headers=login_staff(client))) == "DOCTOR_REQUIRED"
    assert code(client.get("/calendar?doctor_id=D9", headers=login_staff(client))) == "DOCTOR_NOT_FOUND"
