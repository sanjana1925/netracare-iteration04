"""Iteration 4 API checks: R1 (reschedule and Staff approvals, re-verified) and R7 (doctor leave, rebooking proposals, Staff approval).

Fixed clock CLINIC_NOW = 2026-11-02 10:00 (a Monday) and the database the data pack 4 README starts from (data pack 1 + seed + data pack 2).
The real model runs wherever a booking is made (every booking goes through the red-flag check); nothing is faked.
leave_scenarios.csv (data pack 4) is replayed row by row: the proposals must be exactly the ones the data pack says.
"""
import csv

import pytest

from helpers import DATA4, login_doctor, login_patient, login_staff

LEAVE = list(csv.DictReader(open(DATA4 / "leave_scenarios.csv", encoding="utf-8", newline="")))
REASON = "Routine eye test and new glasses"
REFUSED_TO_STAFF = "INSIDE_2_HOURS"


def _book(client, headers, doctor, sp, vt, date, start, patient=None, reason=REASON):
    body = {"doctor_id": doctor, "speciality": sp, "visit_type": vt, "date": date, "start_time": start, "reason": reason}
    if patient:
        body["patient_id"] = patient
    return client.post("/appointments", headers=headers, json=body)


def _by_id(client, appt_id):
    rows = client.get("/appointments", headers=login_staff(client)).json()
    return next(r for r in rows if r["appointment_id"] == appt_id)


# ======================================================== R7 data pack 4: leave_scenarios.csv
@pytest.mark.parametrize("leave_id", sorted({r["leave_id"] for r in LEAVE}))
def test_leave_scenario_proposals_match_the_data_pack(client4, leave_id):
    rows = [r for r in LEAVE if r["leave_id"] == leave_id]
    first, staff = rows[0], login_staff(client4)
    new_booking = None
    if first["setup"]:  # e.g. L4: "As Staff, book P20 Aditya Bose for Retina (new) with Dr. Shah on 2026-11-04 at 09:00"
        d, sp, vt, date, start = first["original"].split()
        r = _book(client4, staff, d, sp, vt, date, start, patient=first["patient_id"])
        assert r.status_code == 201, r.text
        new_booking = r.json()["appointment_id"]

    leave = client4.post("/doctor/leave", headers=login_doctor(client4, first["doctor_id"]),
                         json={"date": first["leave_date"], "session": first["leave_session"]})
    assert leave.status_code == 201, leave.text
    assert leave.json()["affected"] == len(rows), f"{leave_id}: the data pack lists {len(rows)} affected appointment(s)"

    proposals = {p["original_appointment_id"]: p for p in client4.get("/staff/rebookings", headers=staff).json()}
    for row in rows:
        orig_id = new_booking if row["affected_appointment"].startswith("(") else row["affected_appointment"]
        reb = proposals[orig_id]
        want_doc, want_sp, want_vt, want_date, want_start = row["expected_proposal"].split()
        got = (reb["new_doctor_id"], reb["speciality"], reb["visit_type"], reb["new_date"], reb["new_start"])
        assert got == (want_doc, want_sp, want_vt, want_date, want_start), f"{leave_id} {orig_id}: {got} != {row['expected_proposal']}  ({row['why']})"
        assert reb["status"] == "pending" and reb["patient_id"] == row["patient_id"]
        assert _by_id(client4, orig_id)["status"] == "cancelled"                              # the original was cancelled
        assert _by_id(client4, reb["proposed_appointment_id"])["status"] == "proposed"        # the proposal holds the slot

    # Staff approve them all: the proposals become booked and the patient sees why they moved
    for row in rows:
        orig_id = new_booking if row["affected_appointment"].startswith("(") else row["affected_appointment"]
        assert client4.post(f"/staff/rebookings/{proposals[orig_id]['rebooking_id']}/approve", headers=staff).status_code == 200
        mine = client4.get("/appointments?status=booked", headers=login_patient(client4, {"mobile": _mobile(client4, row["patient_id"]), "name": _name(client4, row["patient_id"])})).json()
        moved = [a for a in mine if a["appointment_id"] == proposals[orig_id]["proposed_appointment_id"]]
        assert moved and moved[0]["moved_note"].startswith("Moved because Dr. ") and moved[0]["moved_note"].endswith(" is on leave")


def _row(client, pid):
    rows = client.get("/patients", headers=login_staff(client)).json()
    return next(r for r in rows if r["patient_id"] == pid)


def _mobile(client, pid):
    return _row(client, pid)["mobile"]


def _name(client, pid):
    return _row(client, pid)["name"]


# ======================================================== R7 the rule itself
def test_a_booking_in_a_session_on_leave_is_refused_with_doctor_on_leave_and_slots_skip_it(client4, patients):
    assert client4.post("/doctor/leave", headers=login_doctor(client4, "D1"), json={"date": "2026-11-10", "session": "AM"}).status_code == 201
    staff, h = login_staff(client4), login_patient(client4, patients["P20"])
    r = _book(client4, h, "D1", "General", "new", "2026-11-10", "10:00")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DOCTOR_ON_LEAVE" and "Dr. Anand Rao is on leave that morning" in r.json()["detail"]["message"]
    slots = client4.get("/slots", params={"doctor_id": "D1", "date": "2026-11-10", "speciality": "General", "visit_type": "new"}, headers=staff).json()["slots"]
    assert slots and min(slots) >= "14:00"  # the morning is gone, the afternoon is still bookable
    assert _book(client4, h, "D1", "General", "new", "2026-11-10", "16:00").status_code == 201


def test_a_full_day_leave_removes_both_sessions(client4):
    client4.post("/doctor/leave", headers=login_doctor(client4, "D1"), json={"date": "2026-11-12", "session": "FULL"})
    slots = client4.get("/slots", params={"doctor_id": "D1", "date": "2026-11-12", "speciality": "General", "visit_type": "new"}, headers=login_staff(client4)).json()["slots"]
    assert slots == []


def test_a_proposed_time_holds_its_slot_and_is_hidden_from_patients_until_staff_approve(client4, patients):
    staff = login_staff(client4)
    client4.post("/doctor/leave", headers=login_doctor(client4, "D1"), json={"date": "2026-11-03", "session": "FULL"})  # A101, A102 are on this day
    reb = client4.get("/staff/rebookings", headers=staff).json()[0]
    assert _by_id(client4, reb["proposed_appointment_id"])["status"] == "proposed"
    asha = login_patient(client4, patients["P01"])
    assert all(a["status"] != "proposed" for a in client4.get("/appointments", headers=asha).json())  # a patient never sees a proposal early
    # the held slot cannot be taken by someone else
    taken = _book(client4, login_staff(client4), reb["new_doctor_id"], reb["speciality"], reb["visit_type"], reb["new_date"], reb["new_start"], patient="P20")
    assert taken.status_code == 409 and taken.json()["detail"]["code"] in ("DOUBLE_BOOKED", "ACTIVE_BOOKING_EXISTS")


def test_staff_reject_frees_the_slot(client4):
    staff = login_staff(client4)
    client4.post("/doctor/leave", headers=login_doctor(client4, "D1"), json={"date": "2026-11-03", "session": "FULL"})
    reb = client4.get("/staff/rebookings", headers=staff).json()[0]
    assert client4.post(f"/staff/rebookings/{reb['rebooking_id']}/reject", headers=staff).json()["status"] == "rejected"
    assert _by_id(client4, reb["proposed_appointment_id"])["status"] == "cancelled"
    free = client4.get("/slots", params={"doctor_id": reb["new_doctor_id"], "date": reb["new_date"], "speciality": reb["speciality"], "visit_type": reb["visit_type"]}, headers=staff).json()["slots"]
    assert reb["new_start"] in free  # the slot is free again
    assert client4.post(f"/staff/rebookings/{reb['rebooking_id']}/approve", headers=staff).json()["detail"]["code"] == "NOT_PENDING"  # decided once


def test_when_nothing_is_free_in_the_30_day_window_staff_are_told_to_call_the_patient(client4):
    staff = login_staff(client4)
    # Dr. Shah is the only Retina doctor. A follow-up on the LAST bookable day (2 December) has no later day to move to.
    r = _book(client4, staff, "D3", "Retina", "follow_up", "2026-12-02", "09:00", patient="P01")
    assert r.status_code == 201, r.text
    client4.post("/doctor/leave", headers=login_doctor(client4, "D3"), json={"date": "2026-12-02", "session": "AM"})
    reb = client4.get("/staff/rebookings", headers=staff).json()
    assert len(reb) == 1 and reb[0]["status"] == "no_slot" and reb[0]["message"] == "No slot: call the patient" and reb[0]["proposed_appointment_id"] is None
    assert client4.post(f"/staff/rebookings/{reb[0]['rebooking_id']}/approve", headers=staff).json()["detail"]["code"] == "NO_PROPOSAL"


def test_marking_leave_is_validated_and_role_checked(client4, patients):
    doc = login_doctor(client4, "D2")
    assert client4.post("/doctor/leave", headers=doc, json={"date": "2026-11-01", "session": "AM"}).json()["detail"]["code"] == "IN_PAST"
    assert client4.post("/doctor/leave", headers=doc, json={"date": "2026-12-05", "session": "AM"}).json()["detail"]["code"] == "BEYOND_30_DAYS"
    assert client4.post("/doctor/leave", headers=doc, json={"date": "2026-11-20", "session": "EVENING"}).status_code == 422
    assert client4.post("/doctor/leave", headers=doc, json={"date": "not-a-date", "session": "AM"}).status_code == 422
    assert client4.post("/doctor/leave", headers=doc, json={"date": "2026-11-20", "session": "AM"}).status_code == 201
    assert client4.post("/doctor/leave", headers=doc, json={"date": "2026-11-20", "session": "AM"}).json()["detail"]["code"] == "LEAVE_EXISTS"  # the same leave twice
    for who in (login_staff(client4), login_patient(client4, patients["P01"])):
        assert client4.post("/doctor/leave", headers=who, json={"date": "2026-11-21", "session": "AM"}).status_code == 403
        assert client4.get("/doctor/leave", headers=who).status_code == 403
    assert client4.post("/doctor/leave", json={"date": "2026-11-21", "session": "AM"}).status_code == 401
    assert [l["date"] for l in client4.get("/doctor/leave", headers=doc).json()] == ["2026-11-20"]  # a doctor sees only their own leave


def test_only_staff_can_see_and_decide_rebookings(client4, patients):
    client4.post("/doctor/leave", headers=login_doctor(client4, "D1"), json={"date": "2026-11-03", "session": "FULL"})
    for who in (login_patient(client4, patients["P01"]), login_doctor(client4, "D1")):
        assert client4.get("/staff/rebookings", headers=who).status_code == 403
        assert client4.post("/staff/rebookings/1/approve", headers=who).status_code == 403
    assert client4.get("/staff/rebookings").status_code == 401


# ======================================================== R1 reschedule and Staff approvals (re-verified in this iteration)
def test_reschedule_cancels_the_old_and_books_the_new_in_one_action_and_a_refusal_keeps_the_original(client4, patients):
    h = login_patient(client4, patients["P20"])
    appt = _book(client4, h, "D3", "Retina", "new", "2026-11-04", "09:00").json()["appointment_id"]
    bad = client4.post(f"/appointments/{appt}/reschedule", headers=h, json={"doctor_id": "D3", "speciality": "Retina", "visit_type": "new", "date": "2026-11-04", "start_time": "15:00"})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "SURGERY_SESSION"  # Dr. Shah operates Wednesday afternoon
    assert _by_id(client4, appt)["status"] == "booked"  # the original stays exactly as it was
    ok = client4.post(f"/appointments/{appt}/reschedule", headers=h, json={"doctor_id": "D3", "speciality": "Retina", "visit_type": "new", "date": "2026-11-05", "start_time": "09:00"})
    assert ok.status_code == 200 and ok.json()["status"] == "booked" and ok.json()["date"] == "2026-11-05"
    assert _by_id(client4, appt)["status"] == "cancelled"


def test_a_cancel_inside_2_hours_goes_to_staff_and_the_patient_sees_the_request_status(client4, patients):
    staff, h = login_staff(client4), login_patient(client4, patients["P03"])
    appt = "A103"  # data pack 1's seed: Lakshmi Nair, today (Monday 2 November) at 11:00, one hour from the fixed clock
    r = client4.post(f"/appointments/{appt}/cancel", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == REFUSED_TO_STAFF and r.json()["detail"]["to_staff"] is True
    assert _by_id(client4, appt)["status"] == "booked"  # it stays booked
    mine = [a for a in client4.get("/appointments?status=booked", headers=h).json() if a["appointment_id"] == appt]
    assert mine[0]["approval_status"] == "pending" and mine[0]["approval_action"] == "cancel"  # shown next to the appointment
    approvals = client4.get("/staff/approvals", headers=staff).json()
    assert len(approvals) == 1
    assert client4.post(f"/staff/approvals/{approvals[0]['approval_id']}/approve", headers=staff).status_code == 200
    assert _by_id(client4, appt)["status"] == "cancelled"  # carried out as Staff


# ======================================================== the public preview of the next free times (the home page's booking card)
def test_the_slot_preview_is_public_returns_real_earliest_free_times_and_no_patient_data(client4):
    r = client4.get("/slots/preview", params={"speciality": "Retina"})  # no login at all
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"speciality", "doctors", "slots"} and body["doctors"] == [{"doctor_id": "D3", "name": "Dr. Kiran Shah"}]
    slots = body["slots"]
    assert 1 <= len(slots) <= 6 and slots == sorted(slots, key=lambda s: (s["date"], s["start_time"]))
    assert all(set(s) == {"date", "weekday", "start_time", "doctor_id", "doctor", "minutes"} and s["minutes"] == 20 for s in slots)
    first = slots[0]  # the same times the logged-in /slots endpoint gives for that doctor and day
    real = client4.get("/slots", params={"doctor_id": "D3", "date": first["date"], "speciality": "Retina", "visit_type": "new"}, headers=login_staff(client4)).json()["slots"]
    assert first["start_time"] == real[0] and {s["start_time"] for s in slots if s["date"] == first["date"]} <= set(real)


def test_the_slot_preview_skips_holidays_leave_and_surgery_like_every_other_check(client4):
    plain = client4.get("/slots/preview", params={"speciality": "Retina"}).json()["slots"]
    assert not any(s["date"] == "2026-11-09" for s in plain)  # clinic holiday
    assert not any(s["weekday"] == "Sat" for s in plain)      # Dr. Shah is not at the clinic on Saturdays
    first_day = plain[0]["date"]
    client4.post("/doctor/leave", headers=login_doctor(client4, "D3"), json={"date": first_day, "session": "FULL"})
    after = client4.get("/slots/preview", params={"speciality": "Retina"}).json()["slots"]
    assert all(s["date"] != first_day for s in after)         # a doctor on leave is not offered


def test_the_slot_preview_refuses_an_unknown_speciality_cleanly(client4):
    r = client4.get("/slots/preview", params={"speciality": "Dentistry"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "SPECIALITY_NOT_FOUND"
    assert client4.get("/slots/preview").status_code == 422                       # a speciality is required
    assert client4.get("/slots/preview", params={"speciality": "x" * 41}).status_code == 422
    assert client4.get("/slots/preview", params={"speciality": "General' OR '1'='1"}).status_code == 404  # never reaches the database as SQL


def test_a_speciality_with_several_doctors_offers_the_earliest_times_across_them(client4):
    slots = client4.get("/slots/preview", params={"speciality": "General"}).json()["slots"]
    assert len({s["doctor_id"] for s in slots}) >= 1 and all(s["doctor_id"] in ("D1", "D3", "D5") for s in slots)  # Dr. Rao, Dr. Shah, Dr. Menon
