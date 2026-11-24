"""Actions on appointments. Each one runs the rules check and the write inside ONE transaction,
so two people can never grab the same slot and a refused reschedule leaves the original untouched."""
import re
import secrets
import sqlite3
from contextlib import contextmanager

from datetime import date as Date, timedelta

from . import events
from . import config, rules
from .rules import Refusal


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")  # take the write lock before checking, so checks and write are atomic
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _next_id(conn) -> str:
    row = conn.execute("SELECT MAX(CAST(SUBSTR(appointment_id, 2) AS INTEGER)) FROM appointments").fetchone()
    return f"A{(row[0] or 0) + 1:03d}"


def _insert(conn, patient_id, doctor_id, speciality, visit_type, date, start, reason, urgent=False) -> str:
    appt_id = _next_id(conn)
    conn.execute("INSERT INTO appointments (appointment_id,patient_id,doctor_id,speciality,visit_type,date,start_time,end_time,"
                 "status,reason,urgent) VALUES (?,?,?,?,?,?,?,?,'booked',?,?)",
                 (appt_id, patient_id, doctor_id, speciality, visit_type, date, start,
                  rules.end_time_for(start, visit_type), reason, 1 if urgent else 0))
    return appt_id


def get_appointment(conn, appointment_id):
    row = conn.execute("SELECT * FROM appointments WHERE appointment_id=?", (appointment_id,)).fetchone()
    if row is None:
        raise Refusal("NOT_FOUND", "That appointment does not exist.", http_status=404)
    return row


REASON_MAX = 300


def clean_reason(reason) -> str:
    """R3: the reason is required and at most 300 characters."""
    reason = " ".join((reason or "").split())
    if not reason:
        raise Refusal("REASON_REQUIRED", "Please tell us the reason for your visit.", http_status=422)
    if len(reason) > REASON_MAX:
        raise Refusal("REASON_TOO_LONG", f"Please keep the reason to {REASON_MAX} characters or fewer.", http_status=422)
    return reason


def book(conn, patient_id, doctor_id, speciality, visit_type, date, start, reason, urgent=False) -> str:
    with transaction(conn):
        rules.check_booking(conn, patient_id, doctor_id, speciality, visit_type, date, start)
        new_id = _insert(conn, patient_id, doctor_id, speciality, visit_type, date, start, reason, urgent)
    events.publish("appointments", change="booked")   # after the change is saved
    return new_id


def _now_iso() -> str:
    return config.clinic_now().isoformat(timespec="minutes")


LATE_MESSAGE = ("That appointment starts in under 2 hours. Please contact Staff: your request has been sent to Staff, "
                "and the appointment stays booked until they approve it.")


def request_approval(conn, appt, action: str, new=None) -> int:
    """Stretch goal: a patient's late cancel / reschedule is saved as a request for Staff. The appointment stays booked."""
    with transaction(conn):
        if conn.execute("SELECT 1 FROM approvals WHERE appointment_id=? AND status='pending'", (appt["appointment_id"],)).fetchone():
            raise Refusal("REQUEST_PENDING", "A request for this appointment is already waiting for Staff.", to_staff=True)
        d, sp, vt, date, start = new or (None, None, None, None, None)
        cur = conn.execute(
            "INSERT INTO approvals (appointment_id,patient_id,action,new_doctor_id,new_speciality,new_visit_type,new_date,"
            "new_start_time,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (appt["appointment_id"], appt["patient_id"], action, d, sp, vt, date, start, _now_iso()))
        new_id = cur.lastrowid
    events.publish("approval", change="requested")   # after the change is saved
    return new_id


def cancel(conn, appointment_id, role, acting_patient_id) -> None:
    try:
        with transaction(conn):
            appt = get_appointment(conn, appointment_id)
            rules.check_cancel(appt, role, acting_patient_id)
            conn.execute("UPDATE appointments SET status='cancelled' WHERE appointment_id=?", (appointment_id,))
        events.publish("appointments", change="cancelled")
    except Refusal as r:
        if r.code == "INSIDE_2_HOURS" and role == "patient":
            r.extra["approval_id"] = request_approval(conn, get_appointment(conn, appointment_id), "cancel")
            r.message = LATE_MESSAGE
        raise


def reschedule(conn, appointment_id, role, acting_patient_id, doctor_id, speciality, visit_type, date, start) -> str:
    """Cancel + new booking as one action. Same rules and 2-hour cut-off. If the new booking is refused, the original stays."""
    try:
        with transaction(conn):
            appt = get_appointment(conn, appointment_id)
            rules.check_cancel(appt, role, acting_patient_id)
            # check the new booking as if the old one were already cancelled
            rules.check_booking(conn, appt["patient_id"], doctor_id, speciality, visit_type, date, start,
                                ignore_appointment_id=appointment_id)
            conn.execute("UPDATE appointments SET status='cancelled' WHERE appointment_id=?", (appointment_id,))
            # the reason (and Urgent tag) were already checked when first booked, so they carry over
            new_id = _insert(conn, appt["patient_id"], doctor_id, speciality, visit_type, date, start,
                             appt["reason"], bool(appt["urgent"]))
        events.publish("appointments", change="rescheduled")
        return new_id
    except Refusal as r:
        if r.code == "INSIDE_2_HOURS" and role == "patient":
            appt = get_appointment(conn, appointment_id)
            # only queue a change that would actually be allowed; otherwise explain why it cannot happen
            rules.check_booking(conn, appt["patient_id"], doctor_id, speciality, visit_type, date, start,
                                ignore_appointment_id=appointment_id)
            r.extra["approval_id"] = request_approval(conn, appt, "reschedule", (doctor_id, speciality, visit_type, date, start))
            r.message = LATE_MESSAGE
        raise


NOTE_MAX = 1000


def complete(conn, appointment_id, doctor_id, note) -> None:
    """R2: a doctor marks a visit completed and MUST write a note."""
    note = " ".join((note or "").split())
    if not note:
        raise Refusal("NOTE_REQUIRED", "Please write a short note about the visit before marking it completed.", http_status=422)
    if len(note) > NOTE_MAX:
        raise Refusal("NOTE_TOO_LONG", f"Please keep the note to {NOTE_MAX} characters or fewer.", http_status=422)
    with transaction(conn):
        appt = get_appointment(conn, appointment_id)
        if appt["doctor_id"] != doctor_id:
            raise Refusal("NOT_YOUR_APPOINTMENT", "That appointment is with another doctor.", http_status=403)
        if appt["status"] != "booked":
            raise Refusal("NOT_ACTIVE", f"That appointment is already {appt['status']}.")
        conn.execute("UPDATE appointments SET status='completed', doctor_note=? WHERE appointment_id=?", (note, appointment_id))
    events.publish("appointments", change="completed")


# ---------- urgent cases (R4) ----------
def require_patient(conn, patient_id) -> None:
    if conn.execute("SELECT 1 FROM patients WHERE patient_id=?", (patient_id,)).fetchone() is None:
        raise Refusal("PATIENT_NOT_FOUND", "That patient does not exist.", http_status=404)


def save_urgent_case(conn, patient_id, reason, category, why, source="booking") -> int:
    """Saved BEFORE the booking rules run, so a refused booking still leaves the urgent case standing.
    Trying again with the same words while Staff have not yet seen the case does not create a duplicate."""
    with transaction(conn):
        same = conn.execute("SELECT urgent_id FROM urgent_cases WHERE patient_id=? AND reason=? AND source=? AND seen=0 "
                            "ORDER BY urgent_id DESC LIMIT 1", (patient_id, reason, source)).fetchone()
        if same:
            return same["urgent_id"]
        cur = conn.execute("INSERT INTO urgent_cases (patient_id,reason,category,why,created_at,source) VALUES (?,?,?,?,?,?)",
                           (patient_id, reason, category, why, config.clinic_now().isoformat(timespec="minutes"), source))
        new_id = cur.lastrowid
    events.publish("urgent_case", category=category)
    return new_id


def list_urgent_cases(conn, include_seen=False):
    sql = ("SELECT u.*, p.name AS patient_name, p.mobile AS patient_mobile FROM urgent_cases u "
           "JOIN patients p USING(patient_id) ")
    if not include_seen:
        sql += "WHERE u.seen = 0 "
    return [dict(r) for r in conn.execute(sql + "ORDER BY u.seen, u.created_at DESC, u.urgent_id DESC")]


def mark_urgent_seen(conn, urgent_id: int, staff_name: str) -> dict:
    with transaction(conn):
        row = conn.execute("SELECT * FROM urgent_cases WHERE urgent_id=?", (urgent_id,)).fetchone()
        if row is None:
            raise Refusal("NOT_FOUND", "That urgent case does not exist.", http_status=404)
        conn.execute("UPDATE urgent_cases SET seen=1, seen_at=?, seen_by=? WHERE urgent_id=?",
                     (config.clinic_now().isoformat(timespec="minutes"), staff_name, urgent_id))
    return dict(conn.execute("SELECT * FROM urgent_cases WHERE urgent_id=?", (urgent_id,)).fetchone())


# ---------- Fact of the Day, saved per patient ----------
def get_saved_fact(conn, patient_id):
    row = conn.execute("SELECT fact FROM patient_facts WHERE patient_id=?", (patient_id,)).fetchone()
    return row["fact"] if row else None


def save_fact(conn, patient_id, fact: str) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO patient_facts (patient_id, fact, created_at) VALUES (?,?,?) "
                     "ON CONFLICT(patient_id) DO UPDATE SET fact=excluded.fact, created_at=excluded.created_at",
                     (patient_id, fact, config.clinic_now().isoformat(timespec="minutes")))


# ---------- doctor's view of a patient (R6) ----------
def patient_history(conn, patient_id: str) -> dict:
    p = conn.execute("SELECT * FROM patients WHERE patient_id=?", (patient_id,)).fetchone()
    if p is None:
        raise Refusal("PATIENT_NOT_FOUND", "That patient does not exist.", http_status=404)
    visits = [dict(r) for r in conn.execute(
        "SELECT a.appointment_id, a.date, a.start_time, a.speciality, a.visit_type, a.reason, a.doctor_note, "
        "d.name AS doctor_name FROM appointments a JOIN doctors d USING(doctor_id) "
        "WHERE a.patient_id=? AND a.status='completed' ORDER BY a.date DESC, a.start_time DESC", (patient_id,))]
    age = config.clinic_now().year - p["year_of_birth"] if p["year_of_birth"] else None
    return {"patient": {"patient_id": p["patient_id"], "name": p["name"], "age": age,
                        "general_health": p["general_health"], "allergies": p["allergies"]},
            "visits": visits}


# ---------- login (no passwords, classroom-level only) ----------
def _new_session(conn, role, name, patient_id=None, doctor_id=None) -> str:
    token = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (token, role, patient_id, doctor_id, name))
    return token


def _clean_patient_input(mobile: str, name: str) -> tuple[str, str]:
    mobile, name = mobile.strip(), " ".join(name.split())
    if not re.fullmatch(r"\d{10}", mobile):
        raise Refusal("INVALID_MOBILE", "Mobile number must be 10 digits.", http_status=422)
    if not name:
        raise Refusal("INVALID_NAME", "Please enter the name.", http_status=422)
    return mobile, name


def _insert_patient(conn, mobile: str, name: str) -> sqlite3.Row:
    n = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] + 1
    pid = f"P{n:02d}"
    while conn.execute("SELECT 1 FROM patients WHERE patient_id=?", (pid,)).fetchone():
        n += 1
        pid = f"P{n:02d}"
    conn.execute("INSERT INTO patients (patient_id,name,mobile) VALUES (?,?,?)", (pid, name, mobile))  # named columns: the table also has the profile columns
    return conn.execute("SELECT * FROM patients WHERE patient_id=?", (pid,)).fetchone()


def create_patient(conn, mobile: str, name: str) -> sqlite3.Row:
    """Staff registers a new walk-in. An existing mobile number is refused, never overwritten."""
    mobile, name = _clean_patient_input(mobile, name)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM patients WHERE mobile=?", (mobile,)).fetchone():
            raise Refusal("MOBILE_EXISTS", "A patient with that mobile number already exists. Pick them from the list.")
        return _insert_patient(conn, mobile, name)


def login_patient(conn, mobile: str, name: str) -> tuple[str, sqlite3.Row]:
    mobile, name = _clean_patient_input(mobile, name)
    with transaction(conn):
        p = conn.execute("SELECT * FROM patients WHERE mobile=?", (mobile,)).fetchone()
        if p is None:  # a new mobile creates a new patient
            p = _insert_patient(conn, mobile, name)
        elif p["name"].casefold() != name.casefold():
            raise Refusal("LOGIN_FAILED", "That mobile number is registered under a different name.", http_status=401)
        return _new_session(conn, "patient", p["name"], patient_id=p["patient_id"]), p


def login_doctor(conn, doctor_id: str):
    d = conn.execute("SELECT * FROM doctors WHERE doctor_id=?", (doctor_id,)).fetchone()
    if d is None:
        raise Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404)
    with transaction(conn):
        return _new_session(conn, "doctor", d["name"], doctor_id=doctor_id), d


def login_staff(conn, name: str):
    name = " ".join(name.split())
    if not name:
        raise Refusal("INVALID_NAME", "Please enter your name.", http_status=422)
    with transaction(conn):
        return _new_session(conn, "staff", name), name


# =====================================================================================================
# Stretch goal: Staff approval queue
# =====================================================================================================
_APPROVAL_SQL = (
    "SELECT ap.*, p.name AS patient_name, p.mobile AS patient_mobile, a.date AS date, a.start_time AS start_time, "
    "a.speciality AS speciality, d.name AS doctor_name, nd.name AS new_doctor_name "
    "FROM approvals ap JOIN patients p USING(patient_id) JOIN appointments a USING(appointment_id) "
    "JOIN doctors d ON d.doctor_id = a.doctor_id LEFT JOIN doctors nd ON nd.doctor_id = ap.new_doctor_id ")


def list_approvals(conn, include_decided=False):
    sql = _APPROVAL_SQL + ("" if include_decided else "WHERE ap.status='pending' ") + "ORDER BY ap.status!='pending', ap.approval_id DESC"
    return [dict(r) for r in conn.execute(sql)]


def _get_pending_approval(conn, approval_id):
    row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if row is None:
        raise Refusal("NOT_FOUND", "That request does not exist.", http_status=404)
    if row["status"] != "pending":
        raise Refusal("NOT_PENDING", f"That request was already {row['status']}.")
    return row


def _decide_approval(conn, approval_id, staff_name, status, result):
    with transaction(conn):
        conn.execute("UPDATE approvals SET status=?, decided_at=?, decided_by=?, result=? WHERE approval_id=?",
                     (status, _now_iso(), staff_name, result, approval_id))
    events.publish("approval", change=status)
    return dict(conn.execute(_APPROVAL_SQL + "WHERE ap.approval_id=?", (approval_id,)).fetchone())


def approve_request(conn, approval_id, staff_name) -> dict:
    """Approve carries the action out AS STAFF (no 2-hour limit), through the same rules check as everything else."""
    row = _get_pending_approval(conn, approval_id)
    if row["action"] == "cancel":
        cancel(conn, row["appointment_id"], "staff", None)
        result = "Cancelled"
    else:
        new_id = reschedule(conn, row["appointment_id"], "staff", None, row["new_doctor_id"], row["new_speciality"],
                            row["new_visit_type"], row["new_date"], row["new_start_time"])
        result = f"Rescheduled as {new_id}"
    return _decide_approval(conn, approval_id, staff_name, "approved", result)


def reject_request(conn, approval_id, staff_name) -> dict:
    _get_pending_approval(conn, approval_id)
    return _decide_approval(conn, approval_id, staff_name, "rejected", "Rejected by Staff; the appointment stays booked")


# =====================================================================================================
# Stretch goal: Staff can add holidays and edit a doctor's weekly schedule
# The rules check reads these tables on every booking, so a change takes effect immediately.
# A change that would leave an existing booked appointment breaking the new rules is REFUSED (cancel or move it first).
# =====================================================================================================
def add_holiday(conn, date_str: str, name: str) -> dict:
    day = rules.parse_date(date_str)
    name = " ".join((name or "").split())
    if not name:
        raise Refusal("NAME_REQUIRED", "Please give the holiday a name.", http_status=422)
    if day < config.clinic_now().date():
        raise Refusal("IN_PAST", "A holiday cannot be added for a past date.")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM holidays WHERE date=?", (day.isoformat(),)).fetchone():
            raise Refusal("HOLIDAY_EXISTS", "That date is already a holiday.")
        n = conn.execute("SELECT COUNT(*) FROM appointments WHERE date=? AND status='booked'", (day.isoformat(),)).fetchone()[0]
        if n:
            raise Refusal("HOLIDAY_HAS_BOOKINGS", f"{n} booked appointment(s) fall on that date. Cancel or move them first.")
        conn.execute("INSERT INTO holidays (date, name) VALUES (?,?)", (day.isoformat(), name))
    return {"date": day.isoformat(), "name": name}


def remove_holiday(conn, date_str: str) -> None:
    day = rules.parse_date(date_str)
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM holidays WHERE date=?", (day.isoformat(),)).fetchone():
            raise Refusal("NOT_FOUND", "That date is not a holiday.", http_status=404)
        conn.execute("DELETE FROM holidays WHERE date=?", (day.isoformat(),))


def _session_of_time(hhmm: str) -> str:
    return "AM" if rules.to_minutes(hhmm) < rules.to_minutes("13:00") else "PM"


def set_schedule(conn, doctor_id: str, day: str, session: str, status: str) -> dict:
    if day not in rules.DAYS[:6]:
        raise Refusal("INVALID_DAY", "Day must be Mon, Tue, Wed, Thu, Fri or Sat.", http_status=422)
    if session not in ("AM", "PM"):
        raise Refusal("INVALID_SESSION", "Session must be AM or PM.", http_status=422)
    if status not in ("OPD", "SURGERY", "OFF"):
        raise Refusal("INVALID_STATUS", "Status must be OPD, SURGERY or OFF.", http_status=422)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM doctors WHERE doctor_id=?", (doctor_id,)).fetchone() is None:
            raise Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404)
        if status != "OPD":
            today = config.clinic_now().date().isoformat()
            clash = [r for r in conn.execute(
                "SELECT date, start_time FROM appointments WHERE doctor_id=? AND status='booked' AND date >= ?", (doctor_id, today))
                if rules.DAYS[Date.fromisoformat(r["date"]).weekday()] == day and _session_of_time(r["start_time"]) == session]
            if clash:
                raise Refusal("SCHEDULE_HAS_BOOKINGS", f"{len(clash)} booked appointment(s) fall in that session. Cancel or move them first.")
        conn.execute("INSERT INTO weekly_schedule (doctor_id, day, session, status) VALUES (?,?,?,?) "
                     "ON CONFLICT(doctor_id, day, session) DO UPDATE SET status=excluded.status", (doctor_id, day, session, status))
    return {"doctor_id": doctor_id, "day": day, "session": session, "status": status}


# =====================================================================================================
# Stretch goal: week view calendar for each doctor
# =====================================================================================================
def week_calendar(conn, doctor_id: str, any_date: str, show_patients: bool) -> dict:
    doctor = conn.execute("SELECT * FROM doctors WHERE doctor_id=?", (doctor_id,)).fetchone()
    if doctor is None:
        raise Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404)
    d = rules.parse_date(any_date)
    monday = d - timedelta(days=d.weekday())
    days = []
    for i in range(6):  # Mon..Sat (the clinic is closed on Sundays)
        day = monday + timedelta(days=i)
        name = rules.DAYS[i]
        holiday = conn.execute("SELECT name FROM holidays WHERE date=?", (day.isoformat(),)).fetchone()
        sessions = {s: (conn.execute("SELECT status FROM weekly_schedule WHERE doctor_id=? AND day=? AND session=?",
                                     (doctor_id, name, s)).fetchone() or {"status": "OFF"})["status"] for s in ("AM", "PM")}
        appts = []
        for r in conn.execute(
                "SELECT a.*, p.name AS patient_name FROM appointments a JOIN patients p USING(patient_id) "
                "WHERE a.doctor_id=? AND a.date=? AND a.status IN ('booked','completed') ORDER BY a.start_time", (doctor_id, day.isoformat())):
            item = {"appointment_id": r["appointment_id"], "start_time": r["start_time"], "end_time": r["end_time"],
                    "speciality": r["speciality"], "visit_type": r["visit_type"], "status": r["status"]}
            if show_patients:
                item["patient_name"] = r["patient_name"]
            appts.append(item)
        days.append({"date": day.isoformat(), "weekday": name, "holiday": holiday["name"] if holiday else None,
                     "sessions": sessions, "appointments": appts})
    return {"doctor_id": doctor_id, "doctor": doctor["name"], "week_start": monday.isoformat(),
            "prev_week": (monday - timedelta(days=7)).isoformat(), "next_week": (monday + timedelta(days=7)).isoformat(), "days": days}
