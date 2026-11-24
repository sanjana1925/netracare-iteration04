"""Requirements R1-R5 checks that are not in rule_tests.csv: roles, completion, free slots, login."""
import csv

from helpers import DATA, login_doctor, login_patient, login_staff


def _count(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return len(list(csv.DictReader(f)))


def test_r1_data_loaded_and_asha_can_login(client, patients):
    h = login_patient(client, patients["P01"])
    assert client.get("/me", headers=h).json()["name"] == "Asha Verma"
    assert len(client.get("/doctors").json()) == 5
    assert len(client.get("/patients", headers=login_staff(client)).json()) == _count("patients.csv") == 25
    assert len(client.get("/holidays", headers=h).json()) == _count("holidays.csv")
    assert len(client.get("/schedule", headers=h).json()) == _count("weekly_schedule.csv") == 60


def test_login_rules(client):
    bad = client.post("/login", json={"role": "patient", "mobile": "9000000001", "name": "Someone Else"})
    assert bad.status_code == 401
    assert client.post("/login", json={"role": "patient", "mobile": "123", "name": "X"}).status_code == 422
    new = client.post("/login", json={"role": "patient", "mobile": "9000000099", "name": "New Person"})
    assert new.status_code == 200 and new.json()["user"]["patient_id"] == "P26"  # new mobile creates a new patient
    again = client.post("/login", json={"role": "patient", "mobile": "9000000099", "name": "new person"})
    assert again.json()["user"]["patient_id"] == "P26"


def test_no_token_is_refused(client):
    assert client.get("/appointments").status_code == 401
    assert client.get("/slots?doctor_id=D1&date=2026-11-04&speciality=Cataract").status_code == 401


def test_r3_patient_sees_only_own_and_cannot_touch_others(client, patients):
    h = login_patient(client, patients["P05"])
    mine = client.get("/appointments", headers=h).json()
    assert mine and all(a["patient_id"] == "P05" for a in mine)
    assert all("patient_mobile" not in a for a in mine)
    assert all(a["patient_id"] == "P05" for a in client.get("/appointments?doctor_id=D1", headers=h).json())
    assert client.post("/appointments/A106/cancel", headers=h).json()["detail"]["code"] == "NOT_YOUR_APPOINTMENT"
    assert client.post("/appointments/A106/reschedule", headers=h, json={
        "doctor_id": "D4", "speciality": "Dry eye", "visit_type": "new", "date": "2026-11-06",
        "start_time": "10:00", "reason": "Routine eye check"}).json()["detail"]["code"] == "NOT_YOUR_APPOINTMENT"
    assert client.get("/patients", headers=h).status_code == 403
    # a patient_id in the body is ignored: a patient cannot book on behalf of someone else
    r = client.post("/appointments", headers=h, json={"patient_id": "P20", "doctor_id": "D1", "speciality": "General",
                                                      "visit_type": "new", "date": "2026-11-04", "start_time": "10:00", "reason": "Routine eye check"})
    assert r.status_code == 201 and r.json()["patient_id"] == "P05"


def test_staff_sees_everything_doctor_sees_own(client):
    assert len(client.get("/appointments", headers=login_staff(client)).json()) == _count("stretch/appointments_seed.csv")
    d = client.get("/appointments", headers=login_doctor(client, "D1")).json()
    assert d and all(a["doctor_id"] == "D1" for a in d)
    assert all(a["patient_name"] for a in d)  # the doctor sees which patient is in each slot


def test_doctor_cannot_cancel_patient_cannot_complete(client, patients):
    assert client.post("/appointments/A101/cancel", headers=login_doctor(client, "D1")).status_code == 403
    pat = login_patient(client, patients["P01"])
    assert client.post("/appointments/A101/complete", headers=pat, json={"note": "Reviewed, doing well."}).status_code == 403


def test_r2_completed_visit_enables_follow_up_with_same_doctor_only(client, patients):
    assert client.post("/appointments/A101/complete", headers=login_doctor(client, "D2"), json={"note": "Reviewed, doing well."}).status_code == 403  # wrong doctor
    assert client.post("/appointments/A101/complete", headers=login_doctor(client, "D1"), json={"note": "Reviewed, doing well."}).status_code == 200
    h = login_patient(client, patients["P01"])
    body = {"speciality": "Cataract", "visit_type": "follow_up", "date": "2026-11-11", "start_time": "10:00", "reason": "Routine eye check"}
    iyer = client.post("/appointments", headers=h, json={**body, "doctor_id": "D2"})
    assert iyer.json()["detail"]["code"] == "FOLLOW_UP_DIFFERENT_DOCTOR"
    rao = client.post("/appointments", headers=h, json={**body, "doctor_id": "D1"})
    assert rao.status_code == 201 and rao.json()["end_time"] == "10:10"
    again = client.post("/appointments/A101/complete", headers=login_doctor(client, "D1"), json={"note": "Reviewed, doing well."})
    assert again.json()["detail"]["code"] == "NOT_ACTIVE"


def test_r4_free_slots(client, patients):
    h = login_patient(client, patients["P17"])

    def q(doc, date, sp, vt="new"):
        url = f"/slots?doctor_id={doc}&date={date}&speciality={sp}&visit_type={vt}"
        return client.get(url, headers=h).json()["slots"]

    thu = q("D1", "2026-11-05", "Cataract")
    assert thu and min(thu) >= "14:00"  # Dr. Rao: no Thursday morning (surgery), PM still open
    assert q("D2", "2026-11-05", "Glaucoma") == []  # Dr. Iyer off on Thursdays
    assert q("D1", "2026-11-08", "General") == []  # Sunday
    assert q("D1", "2026-11-09", "General") == []  # holiday
    assert q("D4", "2026-11-04", "Glaucoma") == []  # speciality the doctor does not cover
    wed = q("D1", "2026-11-04", "General")
    assert wed[0] == "09:00" and "12:40" in wed and "12:50" not in wed and "17:40" in wed and "17:50" not in wed
    assert "13:00" not in wed and "13:50" not in wed and "14:00" in wed
    assert "12:50" in q("D1", "2026-11-04", "General", "follow_up")  # a 10-minute follow-up still fits
    # a slot disappears once someone books it, and returns once it is cancelled
    assert "10:00" in wed
    r = client.post("/appointments", headers=h, json={"doctor_id": "D1", "speciality": "General", "visit_type": "new",
                                                      "date": "2026-11-04", "start_time": "10:00", "reason": "Routine eye check"})
    after = q("D1", "2026-11-04", "General")
    assert "10:00" not in after and "09:50" not in after and "10:10" not in after and "09:40" in after
    assert client.post(f"/appointments/{r.json()['appointment_id']}/cancel", headers=login_staff(client)).status_code == 200
    assert "10:00" in q("D1", "2026-11-04", "General")


def test_cancel_then_rebook_as_someone_else(client, patients):
    p4 = login_patient(client, patients["P04"])
    assert client.post("/appointments/A104/cancel", headers=p4).status_code == 200  # 12:30 is 2.5h away
    p17 = login_patient(client, patients["P17"])
    r = client.post("/appointments", headers=p17, json={"doctor_id": "D3", "speciality": "Retina", "visit_type": "new",
                                                        "date": "2026-11-02", "start_time": "12:30", "reason": "Routine eye check"})
    assert r.status_code == 201  # the freed slot is bookable by anyone


def test_empty_database_starts_with_no_appointments(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setenv("NETRACARE_DB", str(tmp_path / "empty.db"))
    db.init_db(with_past_visits=False)  # data pack 1 only: no appointments yet
    c = db.connect()
    assert c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 5
    c.close()


def test_staff_can_register_new_walk_in_and_book_them(client, patients):
    staff = login_staff(client)
    r = client.post("/patients", headers=staff, json={"name": "Walk In", "mobile": "9000000099"})
    assert r.status_code == 201 and r.json()["patient_id"] == "P26"
    dup = client.post("/patients", headers=staff, json={"name": "Other", "mobile": "9000000099"})
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "MOBILE_EXISTS"
    assert client.post("/patients", headers=staff, json={"name": "X", "mobile": "12"}).status_code == 422
    book = client.post("/appointments", headers=staff, json={"patient_id": "P26", "doctor_id": "D1", "speciality": "General",
                                                             "visit_type": "new", "date": "2026-11-04", "start_time": "10:00", "reason": "Routine eye check"})
    assert book.status_code == 201 and book.json()["patient_name"] == "Walk In"
    # patients and doctors cannot register patients
    assert client.post("/patients", headers=login_patient(client, patients["P01"]), json={"name": "A", "mobile": "9000000099"}).status_code == 403
    assert client.post("/patients", headers=login_doctor(client, "D1"), json={"name": "A", "mobile": "9000000099"}).status_code == 403


def test_bad_date_and_time_get_their_own_codes(client, patients):
    h = login_patient(client, patients["P01"])
    bad_date = client.get("/slots?doctor_id=D1&date=2026-13-45&speciality=General", headers=h)
    assert bad_date.status_code == 422 and bad_date.json()["detail"]["code"] == "INVALID_DATE"
    body = {"doctor_id": "D1", "speciality": "General", "visit_type": "new", "date": "2026-11-04", "start_time": "25:99", "reason": "Routine eye check"}
    bad_time = client.post("/appointments", headers=h, json=body)
    assert bad_time.status_code == 422 and bad_time.json()["detail"]["code"] == "INVALID_TIME"


def test_a_database_connection_can_be_used_from_another_thread(tmp_path, monkeypatch):
    """REGRESSION. FastAPI creates the per-request connection (a dependency) in one worker thread and may run the endpoint in
    another. Without check_same_thread=False that raised 'SQLite objects created in a thread can only be used in that same
    thread' at random, e.g. the list refresh right after a successful booking returned HTTP 500 (the page said 'No appointments')."""
    import threading
    from app import db
    monkeypatch.setenv("NETRACARE_DB", str(tmp_path / "threads.db"))
    db.init_db()
    conn = db.connect()
    out = {}

    def work():
        try:
            out["doctors"] = conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0]
        except Exception as e:  # pragma: no cover - only on regression
            out["error"] = repr(e)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    conn.close()
    assert out == {"doctors": 5}, out
