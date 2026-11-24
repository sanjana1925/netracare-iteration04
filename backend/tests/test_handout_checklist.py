"""THE CHECKER: the Iteration 1 handout's own checklist, item by item, through the real HTTP API.

Data: ONLY data pack 1 (doctors, weekly schedule, holidays, patients, and the stretch seed bookings). The clock is the fixed
"today" the data pack README gives for testing: Monday 2 November 2026, 10:00 (so "in the next few weeks" has fixed dates).
Each test name says which handout item it checks (R1-R6, section 03 roles, section 09 rows).
"""
import csv

import pytest

from helpers import DATA, login_doctor, login_patient, login_staff

# Dates relative to the fixed Monday 2 Nov 2026
WED, THU, SUN = "2026-11-04", "2026-11-05", "2026-11-08"


def holidays():
    with open(DATA / "holidays.csv", newline="", encoding="utf-8") as f:
        return [r["date"] for r in csv.DictReader(f)]


def book(client, headers, doctor, speciality, date, start, visit="new", patient_id=None):
    body = {"doctor_id": doctor, "speciality": speciality, "visit_type": visit, "date": date, "start_time": start, "reason": "Routine eye test"}
    if patient_id:
        body["patient_id"] = patient_id
    return client.post("/appointments", headers=headers, json=body)


def code(response):
    return response.json()["detail"]["code"]


# ============================== section 04: requirements R1-R6 ==============================
def test_R1_data_pack_loaded_asha_logs_in_and_sees_5_doctors(client, patients):
    assert login_patient(client, patients["P01"])  # Asha Verma, 9000000001
    me = client.get("/me", headers=login_patient(client, patients["P01"])).json()
    assert me["name"] == "Asha Verma"
    assert [d["name"] for d in client.get("/doctors").json()] == [
        "Dr. Anand Rao", "Dr. Meera Iyer", "Dr. Kiran Shah", "Dr. Fatima Khan", "Dr. Vikram Menon"]
    staff = login_staff(client)
    with open(DATA / "patients.csv", newline="", encoding="utf-8") as f:
        assert len(client.get("/patients", headers=staff).json()) == len(list(csv.DictReader(f))) == 25


def test_R1_nothing_is_booked_at_start_up(tmp_path, monkeypatch):
    """Iteration 1 R1: appointments start empty. In Iteration 2 the default start-up also loads data pack 2, whose 27 PAST visits are
    completed appointments, so what must be empty is the set of upcoming (booked) appointments. Pack 1 alone still starts empty."""
    from app import db
    monkeypatch.setenv("NETRACARE_DB", str(tmp_path / "fresh.db"))
    db.init_db()  # the default start-up: data pack 1 + data pack 2, NO seed bookings
    c = db.connect()
    assert c.execute("SELECT COUNT(*) FROM appointments WHERE status='booked'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM appointments WHERE status='completed'").fetchone()[0] == 27
    assert c.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 5
    c.close()
    monkeypatch.setenv("NETRACARE_DB", str(tmp_path / "pack1_only.db"))
    db.init_db(with_past_visits=False)
    c = db.connect()
    assert c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0  # pack 1 alone: truly empty
    c.close()


def test_R2_completed_cataract_visit_allows_follow_up_with_rao_not_iyer(client, patients):
    # P01 (Asha) has an upcoming Cataract visit with Dr. Rao (A101). Dr. Rao completes it.
    assert client.post("/appointments/A101/complete", headers=login_doctor(client, "D1"), json={"note": "Reviewed."}).status_code == 200
    h = login_patient(client, patients["P01"])
    assert code(book(client, h, "D2", "Cataract", "2026-11-11", "10:00", "follow_up")) == "FOLLOW_UP_DIFFERENT_DOCTOR"
    ok = book(client, h, "D1", "Cataract", "2026-11-11", "10:00", "follow_up")
    assert ok.status_code == 201 and ok.json()["end_time"] == "10:10"


def test_R3_one_patient_cannot_see_or_cancel_anothers_appointment_via_the_api(client, patients):
    p6 = login_patient(client, patients["P06"])
    assert all(a["patient_id"] == "P06" for a in client.get("/appointments", headers=p6).json())
    assert code(client.post("/appointments/A105/cancel", headers=p6)) == "NOT_YOUR_APPOINTMENT"  # A105 is Priya Menon's
    assert [a for a in client.get("/appointments", headers=p6).json() if a["appointment_id"] == "A105"] == []


def test_R3_a_new_mobile_creates_a_new_patient_and_a_wrong_name_is_refused(client, patients):
    # Not in the pack by necessity: R3 says a NEW mobile creates a new patient, so this number cannot be one of the 25.
    new = client.post("/login", json={"role": "patient", "mobile": "9000000099", "name": "New Patient"})
    assert new.status_code == 200 and new.json()["user"]["patient_id"] == "P26"
    assert client.post("/login", json={"role": "patient", "mobile": "9000000001", "name": "Someone Else"}).status_code == 401


def test_R3_doctor_picks_name_and_staff_enters_a_name(client):
    assert client.post("/login", json={"role": "doctor", "doctor_id": "D3"}).json()["user"]["name"] == "Dr. Kiran Shah"
    assert client.post("/login", json={"role": "staff", "name": "Front Desk"}).json()["user"]["role"] == "staff"


def test_R4_free_slots_rao_thursday_morning_is_empty_and_a_booked_slot_disappears(client, patients):
    h = login_patient(client, patients["P17"])

    def slots(doctor, date, sp, visit="new"):
        return client.get(f"/slots?doctor_id={doctor}&date={date}&speciality={sp}&visit_type={visit}", headers=h).json()["slots"]

    thu = slots("D1", THU, "Cataract")
    assert thu and min(thu) >= "14:00"  # no Thursday MORNING slots (surgery); the afternoon is OPD
    wed = slots("D1", WED, "General")
    assert "10:00" in wed and all(int(s[3:]) % 10 == 0 for s in wed)  # 10-minute marks
    assert book(client, h, "D1", "General", WED, "10:00").status_code == 201
    after = slots("D1", WED, "General")
    assert "10:00" not in after and "09:50" not in after and "10:10" not in after  # the new 20-minute visit blocks 10:00-10:20
    assert "10:20" in after


def test_R5_every_action_goes_through_one_rules_check_and_refusals_say_which_rule(client, patients):
    h = login_patient(client, patients["P17"])
    r = book(client, h, "D2", "Glaucoma", THU, "10:00")
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "DOCTOR_OFF" and d["message"] == "Dr. Meera Iyer is not at the clinic on Thursdays."


def test_R5_cancelled_slot_becomes_free_for_anyone(client, patients):
    assert client.post("/appointments/A104/cancel", headers=login_patient(client, patients["P04"])).status_code == 200  # 12:30 is 2.5 h away
    assert book(client, login_patient(client, patients["P17"]), "D3", "Retina", "2026-11-02", "12:30").status_code == 201


# ============================== section 03: what each role can do ==============================
def test_section03_patient_can_do_everything_listed(client, patients):
    h = login_patient(client, patients["P17"])
    assert len(client.get("/doctors").json()) == 5 and client.get("/doctors").json()[0]["specialities"]  # doctors + specialities
    assert client.get(f"/slots?doctor_id=D1&date={WED}&speciality=General&visit_type=new", headers=h).json()["slots"]  # free slots
    booked = book(client, h, "D1", "General", WED, "11:00")
    assert booked.status_code == 201  # book
    mine = client.get("/appointments", headers=h).json()
    assert [a["appointment_id"] for a in mine] == [booked.json()["appointment_id"]]  # see only their own
    assert client.post(f"/appointments/{booked.json()['appointment_id']}/cancel", headers=h).status_code == 200  # cancel


def test_section03_staff_can_do_everything_listed(client, patients):
    staff = login_staff(client)
    assert len(client.get("/appointments", headers=staff).json()) == 19  # every appointment
    by_day = client.get("/appointments?date=2026-11-03", headers=staff).json()
    assert by_day and all(a["date"] == "2026-11-03" for a in by_day)  # by day
    by_doc = client.get("/appointments?doctor_id=D3", headers=staff).json()
    assert by_doc and all(a["doctor_id"] == "D3" for a in by_doc)  # by doctor
    assert book(client, staff, "D1", "General", WED, "10:00", patient_id="P20").status_code == 201  # book for any patient
    walk_in = client.post("/patients", headers=staff, json={"name": "Walk In", "mobile": "9000000099"})
    assert walk_in.status_code == 201  # a brand-new walk-in
    assert book(client, staff, "D1", "General", WED, "10:30", patient_id=walk_in.json()["patient_id"]).status_code == 201
    assert client.post("/appointments/A103/cancel", headers=staff).status_code == 200  # cancel even inside 2 hours (A103 is 1 h away)


def test_section03_doctor_can_do_everything_listed(client):
    doc = login_doctor(client, "D1")  # picks their name to log in
    mine = client.get("/appointments?date=2026-11-03", headers=doc).json()  # their own schedule for a chosen day
    assert mine and all(a["doctor_id"] == "D1" for a in mine)
    assert all(a["patient_name"] for a in mine)  # which patient is in each slot
    assert client.post(f"/appointments/{mine[0]['appointment_id']}/complete", headers=doc, json={"note": "Reviewed."}).json()["status"] == "completed"
    assert client.post("/appointments/A103/complete", headers=doc, json={"note": "Reviewed."}).status_code == 403  # not their appointment (it is Dr. Shah's)


# ============================== section 09: the 13 "Done when" rows ==============================
def test_S09_row01_new_consultation_with_a_free_doctor_in_opd_is_booked_for_20_minutes(client, patients):
    r = book(client, login_patient(client, patients["P17"]), "D1", "General", WED, "10:00")
    assert r.status_code == 201 and (r.json()["start_time"], r.json()["end_time"]) == ("10:00", "10:20")


def test_S09_row02_same_doctor_and_time_for_another_patient_is_refused_slot_taken(client, patients):
    book(client, login_patient(client, patients["P17"]), "D1", "General", WED, "10:00")
    assert code(book(client, login_patient(client, patients["P20"]), "D1", "General", WED, "10:00")) == "DOUBLE_BOOKED"


def test_S09_row03_rao_thursday_morning_is_refused_surgery(client, patients):
    assert code(book(client, login_patient(client, patients["P17"]), "D1", "Cataract", THU, "10:00")) == "SURGERY_SESSION"


def test_S09_row04_iyer_any_thursday_is_refused_not_at_the_clinic(client, patients):
    h = login_patient(client, patients["P17"])
    for thursday in ("2026-11-05", "2026-11-12", "2026-11-19"):
        assert code(book(client, h, "D2", "Glaucoma", thursday, "10:00")) == "DOCTOR_OFF"


def test_S09_row05_130pm_sunday_and_every_holiday_in_the_csv_are_refused_clinic_closed(client, patients):
    h = login_patient(client, patients["P17"])
    assert code(book(client, h, "D1", "General", WED, "13:30")) == "OUTSIDE_SESSION"  # lunch break
    assert code(book(client, h, "D3", "Retina", SUN, "10:00")) == "CLINIC_CLOSED"
    in_window = [d for d in holidays() if "2026-11-03" <= d <= "2026-12-02" and __import__("datetime").date.fromisoformat(d).weekday() != 6]
    assert in_window  # holidays.csv has at least one bookable-weekday holiday inside the window
    for date in in_window:
        assert code(book(client, h, "D3", "Retina", date, "10:00")) == "HOLIDAY"


def test_S09_row06_new_consultation_at_1250pm_is_refused_would_end_after_1pm(client, patients):
    assert code(book(client, login_patient(client, patients["P17"]), "D1", "General", WED, "12:50")) == "ENDS_AFTER_SESSION"


def test_S09_row07_more_than_30_days_ahead_or_in_the_past_is_refused(client, patients):
    h = login_patient(client, patients["P17"])
    assert code(book(client, h, "D3", "Retina", "2026-12-03", "10:00")) == "BEYOND_30_DAYS"
    assert code(book(client, h, "D3", "Retina", "2026-11-02", "09:00")) == "IN_PAST"
    assert book(client, h, "D3", "Retina", "2026-12-02", "10:00").status_code == 201  # exactly 30 days ahead is fine


def test_S09_row08_dr_khan_for_glaucoma_is_refused_doctor_does_not_cover_it(client, patients):
    assert code(book(client, login_patient(client, patients["P17"]), "D4", "Glaucoma", WED, "10:00")) == "SPECIALITY_MISMATCH"


def test_S09_row09_second_cataract_appointment_for_a_patient_who_has_one_is_refused(client, patients):
    assert code(book(client, login_patient(client, patients["P01"]), "D2", "Cataract", WED, "11:00")) == "ACTIVE_BOOKING_EXISTS"


def test_S09_row10_follow_up_for_a_patient_never_seen_or_with_a_different_doctor_is_refused(client, patients):
    assert code(book(client, login_patient(client, patients["P17"]), "D1", "General", WED, "10:00", "follow_up")) == "NO_PREVIOUS_VISIT"
    # P18 (Harish Chandra) was seen by Dr. Rao for Cataract (A006), so a follow-up with Dr. Iyer is refused
    assert code(book(client, login_patient(client, patients["P18"]), "D2", "Cataract", WED, "11:00", "follow_up")) == "FOLLOW_UP_DIFFERENT_DOCTOR"
    assert book(client, login_patient(client, patients["P18"]), "D1", "Cataract", WED, "09:20", "follow_up").status_code == 201


def test_S09_row11_patient_cancel_inside_2_hours_is_refused_please_contact_staff(client, patients):
    r = client.post("/appointments/A103/cancel", headers=login_patient(client, patients["P03"]))  # starts 11:00, now is 10:00
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "INSIDE_2_HOURS" and d["to_staff"] is True and "Please contact Staff" in d["message"]


def test_S09_row12_staff_can_cancel_that_same_appointment(client, patients):
    client.post("/appointments/A103/cancel", headers=login_patient(client, patients["P03"]))
    r = client.post("/appointments/A103/cancel", headers=login_staff(client))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_S09_row13_cancel_then_book_the_slot_as_someone_else(client, patients):
    assert client.post("/appointments/A105/cancel", headers=login_patient(client, patients["P05"])).status_code == 200  # Wed 4 Nov 10:00
    assert book(client, login_patient(client, patients["P17"]), "D2", "Glaucoma", WED, "10:00").status_code == 201


def test_S09_final_bullet_patient_cannot_change_another_patients_appointment_by_calling_the_api(client, patients):
    p6 = login_patient(client, patients["P06"])
    assert code(client.post("/appointments/A105/cancel", headers=p6)) == "NOT_YOUR_APPOINTMENT"
    body = {"doctor_id": "D2", "speciality": "Glaucoma", "visit_type": "new", "date": "2026-11-06", "start_time": "10:00"}
    assert code(client.post("/appointments/A105/reschedule", headers=p6, json=body)) == "NOT_YOUR_APPOINTMENT"
    assert client.post("/appointments/A105/complete", headers=p6, json={"note": "Reviewed."}).status_code == 403
