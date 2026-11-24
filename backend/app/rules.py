"""THE rules check. Every clinic rule (brief section 04) lives in this one file.

Nothing else in the app decides whether a booking is allowed: the API, the screens and (in later
iterations) the AI agent all go through `check_booking` / `check_cancel`.
Reason codes match the data pack README exactly.
"""
import sqlite3
from datetime import date as Date, datetime, timedelta

from . import config

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_FULL = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]


class Refusal(Exception):
    """A rule was broken. `code` is a README reason code, `message` is plain words for people."""

    def __init__(self, code: str, message: str, to_staff: bool = False, http_status: int = 409):
        super().__init__(message)
        self.code, self.message, self.to_staff, self.http_status = code, message, to_staff, http_status
        self.extra: dict = {}  # extra fields for the error body, e.g. approval_id when a request was sent to Staff
        self.emergency = None  # set by the API when the reason was a red flag, even though the booking was refused

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "to_staff": self.to_staff}
        if self.emergency:
            d["emergency"] = self.emergency
        d.update(self.extra)
        return d


# ---------- small helpers ----------
def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_date(value: str) -> Date:
    try:
        return Date.fromisoformat(value)
    except (ValueError, TypeError):
        raise Refusal("INVALID_DATE", "That date is not valid. Use YYYY-MM-DD.", http_status=422)


def parse_time(value: str) -> int:
    try:
        h, m = value.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        raise Refusal("INVALID_TIME", "That time is not valid. Use HH:MM.", http_status=422)


def end_time_for(start: str, visit_type: str) -> str:
    return to_hhmm(to_minutes(start) + config.DURATION[visit_type])


def _doctor(conn: sqlite3.Connection, doctor_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM doctors WHERE doctor_id = ?", (doctor_id,)).fetchone()
    if row is None:
        raise Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404)
    return row


def _session_of(start_min: int):
    for name, (s, e) in config.SESSIONS.items():
        if to_minutes(s) <= start_min < to_minutes(e):
            return name, to_minutes(e)
    return None, None


# ---------- slot-level rules (do not depend on who is booking) ----------
def check_slot(conn, doctor_id, speciality, visit_type, date_str, start,
               ignore_appointment_id=None, doctor_row=None) -> None:
    """Raise Refusal if this doctor/date/start time cannot be booked by anyone."""
    if visit_type not in config.DURATION:
        raise Refusal("INVALID_VISIT_TYPE", "Visit type must be 'new' or 'follow_up'.", http_status=422)
    doctor = doctor_row or _doctor(conn, doctor_id)
    day = parse_date(date_str)
    start_min = parse_time(start)
    now = config.clinic_now()

    if start_min % config.SLOT_MINUTES != 0:
        raise Refusal("INVALID_START_TIME", "Appointments start on 10-minute marks, for example 10:00 or 10:10.")
    if datetime.combine(day, datetime.min.time()) + timedelta(minutes=start_min) < now:
        raise Refusal("IN_PAST", "That time has already passed.")
    if day > now.date() + timedelta(days=config.BOOKING_WINDOW_DAYS):
        raise Refusal("BEYOND_30_DAYS", "Bookings open only up to 30 days ahead.")
    if day.weekday() == 6:
        raise Refusal("CLINIC_CLOSED", "The clinic is closed on Sundays.")
    holiday = conn.execute("SELECT name FROM holidays WHERE date = ?", (day.isoformat(),)).fetchone()
    if holiday:
        raise Refusal("HOLIDAY", f"The clinic is closed on {day.isoformat()} ({holiday['name']}).")

    session, session_end = _session_of(start_min)
    if session is None:
        raise Refusal("OUTSIDE_SESSION", "The clinic takes appointments 9 AM to 1 PM and 2 PM to 6 PM only.")
    end_min = start_min + config.DURATION[visit_type]
    if end_min > session_end:
        raise Refusal("ENDS_AFTER_SESSION",
                      f"A {config.DURATION[visit_type]}-minute visit at {to_hhmm(start_min)} would end after "
                      f"{to_hhmm(session_end)}, when the session closes.")

    sched = conn.execute("SELECT status FROM weekly_schedule WHERE doctor_id=? AND day=? AND session=?",
                         (doctor_id, DAYS[day.weekday()], session)).fetchone()
    status = sched["status"] if sched else "OFF"
    if status == "OFF":
        raise Refusal("DOCTOR_OFF", f"{doctor['name']} is not at the clinic on {DAY_FULL[day.weekday()]}.")
    if status == "SURGERY":
        raise Refusal("SURGERY_SESSION", f"{doctor['name']} is in surgery that {'morning' if session == 'AM' else 'afternoon'}.")
    on_leave = conn.execute("SELECT 1 FROM leaves WHERE doctor_id=? AND date=? AND session IN (?, 'FULL')",
                            (doctor_id, day.isoformat(), session)).fetchone()
    if on_leave:  # iteration 4 (R7): the last rule of brief section 04
        raise Refusal("DOCTOR_ON_LEAVE", f"{doctor['name']} is on leave that {'morning' if session == 'AM' else 'afternoon'}.")

    if speciality not in doctor["specialities"].split(";"):
        raise Refusal("SPECIALITY_MISMATCH", f"{doctor['name']} does not cover {speciality}.")

    clash = conn.execute(
        "SELECT 1 FROM appointments WHERE doctor_id=? AND date=? AND status IN ('booked','proposed') "
        "AND appointment_id IS NOT ? AND start_time < ? AND end_time > ?",
        (doctor_id, day.isoformat(), ignore_appointment_id, to_hhmm(end_min), to_hhmm(start_min))).fetchone()
    if clash:
        raise Refusal("DOUBLE_BOOKED", "That slot is taken. Please choose another time.")


# ---------- patient-level rules ----------
def check_patient(conn, patient_id, doctor_id, speciality, visit_type, ignore_appointment_id=None) -> None:
    if conn.execute("SELECT 1 FROM patients WHERE patient_id=?", (patient_id,)).fetchone() is None:
        raise Refusal("PATIENT_NOT_FOUND", "That patient does not exist.", http_status=404)

    if visit_type == "follow_up":
        last = conn.execute(
            "SELECT a.doctor_id, d.name FROM appointments a JOIN doctors d USING(doctor_id) "
            "WHERE a.patient_id=? AND a.speciality=? AND a.status='completed' "
            "ORDER BY a.date DESC, a.start_time DESC LIMIT 1", (patient_id, speciality)).fetchone()
        if last is None:
            raise Refusal("NO_PREVIOUS_VISIT", f"A follow-up needs a completed {speciality} visit first. Please book a new consultation.")
        if last["doctor_id"] != doctor_id:
            raise Refusal("FOLLOW_UP_DIFFERENT_DOCTOR", f"A {speciality} follow-up must be with {last['name']}, who saw you last.")

    active = conn.execute(
        "SELECT 1 FROM appointments WHERE patient_id=? AND speciality=? AND status IN ('booked','proposed') "
        "AND appointment_id IS NOT ?", (patient_id, speciality, ignore_appointment_id)).fetchone()
    if active:
        raise Refusal("ACTIVE_BOOKING_EXISTS", f"You already have an upcoming {speciality} appointment. Cancel it first to book another.")


def check_booking(conn, patient_id, doctor_id, speciality, visit_type, date_str, start,
                  ignore_appointment_id=None) -> None:
    """All booking rules. Order matters so that each refusal names the most basic broken rule first."""
    check_slot(conn, doctor_id, speciality, visit_type, date_str, start, ignore_appointment_id)
    check_patient(conn, patient_id, doctor_id, speciality, visit_type, ignore_appointment_id)


# ---------- cancel rules ----------
def check_cancel(appt: sqlite3.Row, role: str, acting_patient_id: str | None) -> None:
    if role == "patient" and appt["patient_id"] != acting_patient_id:
        raise Refusal("NOT_YOUR_APPOINTMENT", "That appointment belongs to someone else.", http_status=403)
    if appt["status"] != "booked":
        raise Refusal("NOT_ACTIVE", f"That appointment is already {appt['status']}.")
    if role == "patient":
        starts = datetime.fromisoformat(f"{appt['date']}T{appt['start_time']}")
        if starts - config.clinic_now() < timedelta(hours=config.CANCEL_CUTOFF_HOURS):
            raise Refusal("INSIDE_2_HOURS", "Appointments can be cancelled online until 2 hours before the visit. Please contact Staff.", to_staff=True)


# ---------- free slots (R4) ----------
def free_slots(conn, doctor_id, speciality, visit_type, date_str) -> list[str]:
    """Every start time on a 10-minute mark that passes the slot rules for this doctor and date."""
    doctor = _doctor(conn, doctor_id)
    parse_date(date_str)
    out = []
    for m in range(9 * 60, 18 * 60, config.SLOT_MINUTES):
        try:
            check_slot(conn, doctor_id, speciality, visit_type, date_str, to_hhmm(m), doctor_row=doctor)
            out.append(to_hhmm(m))
        except Refusal as r:
            if r.code in ("CLINIC_CLOSED", "HOLIDAY", "SPECIALITY_MISMATCH", "BEYOND_30_DAYS"):
                break  # whole-day reasons: no point checking every mark (session-level ones must not stop the scan)
    return out
