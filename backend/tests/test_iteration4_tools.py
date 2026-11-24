"""R2: the agent's tools, called directly the way the handout's check describes ("call each tool as a patient, including one refusal and
one appointment that belongs to someone else"). No model decides anything here: the read-only tools are invoked as tools, with the
patient in the graph config; the three action tools are tested through do_book / do_cancel / do_reschedule, which run AFTER the patient's
confirmation and call the SAME API functions as the website. A booking makes one real red-flag call to the real model.

Fixed clock: Monday 2 November 2026, 10:00. Database: data pack 1 + seed + data pack 2.
"""
import json

import pytest

from app import db, tools
from helpers import login_patient

REASON = "Routine eye test and new glasses"


def cfg(pid):
    return {"configurable": {"patient_id": pid}}


def call(tool, pid, **args):
    return json.loads(tool.invoke(args, config=cfg(pid)))


def conn_():
    return db.connect()


# ======================================================== the tools as the model sees them
def test_there_are_exactly_the_nine_tools_each_with_a_clear_name_and_description():
    # the seven of the handout, plus the two for the patient's choice of a new time after doctor leave (a stretch goal)
    assert [t.name for t in tools.TOOLS] == ["search_clinic_docs", "list_doctors", "find_slots", "my_appointments",
                                             "book_appointment", "cancel_appointment", "reschedule_appointment",
                                             "my_rebooking_options", "choose_rebooking"]
    assert all(len(t.description.strip()) > 40 for t in tools.TOOLS)


def test_the_patient_id_is_never_a_tool_argument_so_the_model_cannot_act_for_someone_else():
    for t in tools.TOOLS:
        assert "patient_id" not in t.args and "patient" not in " ".join(t.args), t.name
        assert "config" not in t.args  # the graph config is injected, not offered to the model
    assert set(tools.ACTION_TOOLS) == {"book_appointment", "cancel_appointment", "reschedule_appointment", "choose_rebooking"}


def test_every_tool_argument_is_typed():
    schemas = {t.name: t.args for t in tools.TOOLS}
    assert schemas["find_slots"]["visit_type"]["enum"] == ["new", "follow_up"]
    assert schemas["find_slots"]["session"]["anyOf"][0]["enum"] == ["AM", "PM"]
    assert schemas["book_appointment"]["visit_type"]["enum"] == ["new", "follow_up"]
    assert set(schemas["book_appointment"]) == {"doctor_id", "speciality", "visit_type", "date", "start_time", "reason"}
    assert set(schemas["cancel_appointment"]) == {"appointment_id"}
    assert set(schemas["reschedule_appointment"]) == {"appointment_id", "doctor_id", "date", "start_time"}
    assert set(schemas["choose_rebooking"]) == {"rebooking_id", "doctor_id", "date", "start_time"}
    assert set(schemas["my_rebooking_options"]) == set()


# ======================================================== read-only tools
def test_list_doctors_has_ids_specialities_and_opd_days(client4):
    r = call(tools.list_doctors, "P01")
    docs = {d["doctor_id"]: d for d in r["doctors"]}
    assert r["result"] == "ok" and set(docs) == {"D1", "D2", "D3", "D4", "D5"}
    assert docs["D1"]["name"] == "Dr. Anand Rao" and docs["D1"]["specialities"] == ["Cataract", "General"]
    assert "Thu" in docs["D1"]["opd_days"] and docs["D1"]["opd_days"]["Thu"] == "PM"  # Thursday morning is surgery, not OPD
    assert "Sat" not in docs["D3"]["opd_days"]  # Dr. Shah is not at the clinic on Saturdays


def test_find_slots_returns_at_most_10_earliest_first_and_honours_the_session(client4):
    r = call(tools.find_slots, "P01", visit_type="new", start_date="2026-11-04", end_date="2026-11-20", speciality="Retina")
    assert r["result"] == "ok" and len(r["slots"]) <= 10
    keys = [(s["date"], s["start_time"]) for s in r["slots"]]
    assert keys == sorted(keys) and all(s["doctor_id"] == "D3" and s["minutes"] == 20 for s in r["slots"])
    pm = call(tools.find_slots, "P01", visit_type="follow_up", start_date="2026-11-06", end_date="2026-11-06", doctor_id="D3", session="PM")
    assert pm["result"] == "ok" and all(s["start_time"] >= "14:00" and s["minutes"] == 10 for s in pm["slots"])
    am = call(tools.find_slots, "P01", visit_type="follow_up", start_date="2026-11-06", end_date="2026-11-06", doctor_id="D3", session="AM")
    assert am["slots"][0]["start_time"] == "09:00" and all(s["start_time"] < "13:00" for s in am["slots"])


def test_find_slots_says_why_a_doctor_has_none_and_skips_holidays(client4):
    sat = call(tools.find_slots, "P20", visit_type="new", start_date="2026-11-14", end_date="2026-11-14", doctor_id="D3", speciality="Retina")
    assert sat["result"] == "none" and sat["unavailable"][0]["reason_code"] == "DOCTOR_OFF"  # Dr. Shah is not at the clinic on Saturdays
    hol = call(tools.find_slots, "P20", visit_type="new", start_date="2026-11-09", end_date="2026-11-09", doctor_id="D3", speciality="Retina")
    assert hol["result"] == "none" and hol["unavailable"][0]["reason_code"] == "HOLIDAY"  # Monday 9 November is a clinic holiday


def test_find_slots_stays_inside_the_30_day_window_and_checks_its_arguments(client4):
    far = call(tools.find_slots, "P01", visit_type="new", start_date="2027-02-01", end_date="2027-02-10", speciality="Retina")
    assert far["result"] == "none" and "window" in far["message"]
    assert call(tools.find_slots, "P01", visit_type="new", start_date="2026-11-04", end_date="2026-11-05")["result"] == "error"  # no speciality or doctor
    assert call(tools.find_slots, "P01", visit_type="new", start_date="nope", end_date="2026-11-05", speciality="Retina")["result"] == "error"
    assert call(tools.find_slots, "P01", visit_type="new", start_date="2026-11-04", end_date="2026-11-05", speciality="Dentistry")["result"] == "error"


def test_my_appointments_lists_only_the_logged_in_patients_upcoming_appointments(client4, patients):
    mine = call(tools.my_appointments, "P03")
    assert [a["appointment_id"] for a in mine["appointments"]] == ["A103"] and mine["appointments"][0]["waiting_for_staff"] is None
    others = call(tools.my_appointments, "P04")
    assert [a["appointment_id"] for a in others["appointments"]] == ["A104"]  # a different patient sees a different list
    # a request waiting for Staff shows up next to the appointment
    client4.post("/appointments/A103/cancel", headers=login_patient(client4, patients["P03"]))
    assert call(tools.my_appointments, "P03")["appointments"][0]["waiting_for_staff"] == "cancel"


# ======================================================== the action tools, after the patient pressed Confirm
def test_do_book_books_through_the_same_api_and_the_patient_comes_from_the_login(client4):
    r = json.loads(tools.do_book(conn_(), "P20", "D1", "General", "new", "2026-11-04", "10:00", REASON))
    assert r["result"] == "booked" and r["doctor"] == "Dr. Anand Rao" and r["emergency"] is None
    row = conn_().execute("SELECT * FROM appointments WHERE appointment_id=?", (r["appointment_id"],)).fetchone()
    assert row["patient_id"] == "P20" and row["status"] == "booked" and row["reason"] == REASON


def test_a_refusal_is_a_normal_result_with_its_reason_code_and_plain_message_and_nothing_is_written(client4):
    before = conn_().execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    r = json.loads(tools.do_book(conn_(), "P16", "D2", "Glaucoma", "new", "2026-11-05", "10:00", REASON))  # Dr. Iyer is not at the clinic on Thursdays
    assert r["result"] == "refused" and r["reason_code"] == "DOCTOR_OFF" and "Thursday" in r["message"]
    assert conn_().execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == before


def test_every_clinic_rule_still_applies_through_the_tool(client4):
    expect = {  # (doctor, speciality, visit, date, start) -> reason code
        ("D2", "Glaucoma", "new", "2026-11-08", "10:00"): "CLINIC_CLOSED",          # Sunday
        ("D2", "Glaucoma", "new", "2026-11-09", "10:00"): "HOLIDAY",                # clinic holiday
        ("D2", "Glaucoma", "new", "2026-10-30", "10:00"): "IN_PAST",
        ("D2", "Glaucoma", "new", "2027-01-20", "10:00"): "BEYOND_30_DAYS",
        ("D1", "General", "new", "2026-11-04", "10:05"): "INVALID_START_TIME",
        ("D1", "General", "new", "2026-11-04", "08:00"): "OUTSIDE_SESSION",
        ("D1", "General", "new", "2026-11-04", "12:50"): "ENDS_AFTER_SESSION",
        ("D3", "Cataract", "new", "2026-11-04", "10:00"): "SPECIALITY_MISMATCH",
        ("D3", "Retina", "follow_up", "2026-11-04", "10:00"): "NO_PREVIOUS_VISIT",    # P20 has no Retina visit
    }
    for (doc, sp, vt, date, start), code in expect.items():
        r = json.loads(tools.do_book(conn_(), "P20", doc, sp, vt, date, start, REASON))
        assert r["result"] == "refused" and r["reason_code"] == code, (doc, sp, date, start, r)


def test_a_missing_reason_and_an_unknown_doctor_are_refused(client4):
    assert json.loads(tools.do_book(conn_(), "P20", "D1", "General", "new", "2026-11-04", "10:00", "  "))["reason_code"] == "REASON_REQUIRED"
    assert json.loads(tools.do_book(conn_(), "P20", "D9", "General", "new", "2026-11-04", "10:00", REASON))["reason_code"] == "DOCTOR_NOT_FOUND"


def test_a_second_booking_in_the_same_speciality_is_refused(client4):
    assert json.loads(tools.do_book(conn_(), "P20", "D1", "General", "new", "2026-11-04", "10:00", REASON))["result"] == "booked"
    r = json.loads(tools.do_book(conn_(), "P20", "D3", "General", "new", "2026-11-04", "11:00", REASON))
    assert r["result"] == "refused" and r["reason_code"] == "ACTIVE_BOOKING_EXISTS"


def test_cancel_of_someone_elses_appointment_is_refused_and_reveals_nothing(client4):
    r = json.loads(tools.do_cancel(conn_(), "P03", "A104"))  # A104 belongs to P04
    assert r == {"result": "refused", "reason_code": "NOT_YOUR_APPOINTMENT", "message": "That is not one of your appointments."}
    assert conn_().execute("SELECT status FROM appointments WHERE appointment_id='A104'").fetchone()[0] == "booked"  # unchanged
    assert "Karim" not in json.dumps(r) and "12:30" not in json.dumps(r)
    for bad in ("A999", "", "V001"):  # unknown IDs look exactly the same: the tool does not say whether the appointment exists
        assert json.loads(tools.do_cancel(conn_(), "P03", bad))["reason_code"] == "NOT_YOUR_APPOINTMENT"


def test_cancel_more_than_2_hours_ahead_is_carried_out_and_inside_2_hours_goes_to_staff(client4):
    ok = json.loads(tools.do_cancel(conn_(), "P04", "A104"))  # today 12:30 = 2.5 hours away
    assert ok == {"result": "cancelled", "appointment_id": "A104"}
    assert conn_().execute("SELECT status FROM appointments WHERE appointment_id='A104'").fetchone()[0] == "cancelled"
    late = json.loads(tools.do_cancel(conn_(), "P03", "A103"))  # today 11:00 = 1 hour away
    assert late["result"] == "to_staff" and late["reason_code"] == "INSIDE_2_HOURS" and late["approval_id"]
    assert conn_().execute("SELECT status FROM appointments WHERE appointment_id='A103'").fetchone()[0] == "booked"  # stays booked
    again = json.loads(tools.do_cancel(conn_(), "P03", "A103"))
    assert again["reason_code"] == "REQUEST_PENDING"  # no second request for the same appointment


def test_reschedule_moves_one_appointment_and_a_refusal_leaves_the_original_exactly_as_it_was(client4):
    booked = json.loads(tools.do_book(conn_(), "P20", "D3", "Retina", "new", "2026-11-04", "09:00", REASON))["appointment_id"]
    refused = json.loads(tools.do_reschedule(conn_(), "P20", booked, "D3", "2026-11-04", "15:00"))  # Dr. Shah operates Wednesday afternoon
    assert refused["result"] == "refused" and refused["reason_code"] == "SURGERY_SESSION"
    assert conn_().execute("SELECT status, start_time FROM appointments WHERE appointment_id=?", (booked,)).fetchone()[:] == ("booked", "09:00")
    moved = json.loads(tools.do_reschedule(conn_(), "P20", booked, "D3", "2026-11-05", "09:00"))
    assert moved["result"] == "rescheduled" and moved["old_appointment_id"] == booked
    assert conn_().execute("SELECT status FROM appointments WHERE appointment_id=?", (booked,)).fetchone()[0] == "cancelled"
    new = conn_().execute("SELECT * FROM appointments WHERE appointment_id=?", (moved["new_appointment_id"],)).fetchone()
    assert (new["status"], new["date"], new["start_time"], new["speciality"], new["visit_type"]) == ("booked", "2026-11-05", "09:00", "Retina", "new")


def test_reschedule_of_someone_elses_appointment_is_refused(client4):
    r = json.loads(tools.do_reschedule(conn_(), "P03", "A104", "D1", "2026-11-04", "10:00"))
    assert r["reason_code"] == "NOT_YOUR_APPOINTMENT"
    assert conn_().execute("SELECT status, date FROM appointments WHERE appointment_id='A104'").fetchone()[:] == ("booked", "2026-11-02")


def test_the_action_tools_ask_before_they_touch_the_api_so_nothing_runs_without_a_confirmation():
    import inspect
    for name, fn in (("book_appointment", tools.book_appointment), ("cancel_appointment", tools.cancel_appointment), ("reschedule_appointment", tools.reschedule_appointment)):
        src = inspect.getsource(fn.func)
        assert src.index("_confirm(") < src.index("do_" + name.split("_")[0]), name  # the card comes BEFORE the API call
        assert "service." not in src and "ops." not in src and "INSERT" not in src and "UPDATE" not in src  # a tool never touches the data itself
