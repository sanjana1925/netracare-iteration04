"""The agent's nine tools. Each is a thin layer over the SAME functions the website's endpoints use.

Rules this file follows (handout R2):
  * A tool never re-checks a clinic rule itself and never writes to the database directly. It calls ops/service,
    which run the iteration 1 rules check. A refusal comes back as a NORMAL result with its reason code and plain message.
  * The patient comes from the login, never from the LLM: the tools read `patient_id` from the graph's config
    (config["configurable"]["patient_id"]). It is NOT a tool argument, so the LLM cannot act for someone else.
  * book / cancel / reschedule call interrupt() with a one-line summary BEFORE they touch the API. Nothing changes until the
    patient presses Confirm. (On resume the tool runs again from the top, so everything before interrupt() is read-only.)
"""
import json
from datetime import date as Date, datetime, timedelta
from typing import Literal, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt

from . import config, db, leave_flow, ops, rag, rules, service
from .rules import DAYS, Refusal

ACTION_TOOLS = {"book_appointment", "cancel_appointment", "reschedule_appointment", "choose_rebooking"}  # these need the patient's confirmation


def _patient_id(cfg: RunnableConfig) -> str:
    return cfg["configurable"]["patient_id"]


def fmt_date(iso: str) -> str:
    d = Date.fromisoformat(iso)
    return f"{DAYS[d.weekday()]} {d.day} {d.strftime('%b')}"


def fmt_time(hhmm: str) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:])
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _json(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def _refused(r: Refusal) -> str:
    """A refusal is a normal result, not an error."""
    out = {"result": "refused", "reason_code": r.code, "message": r.message}
    if r.to_staff:
        out["to_staff"] = True
    out.update(r.extra)
    return json.dumps(out, ensure_ascii=False)


def _visit_label(visit_type: str) -> str:
    return "follow-up" if visit_type == "follow_up" else "new consultation"


def _mins(visit_type: str) -> int:
    return config.DURATION[visit_type]


def _doctor_name(conn, doctor_id: str) -> Optional[str]:
    row = conn.execute("SELECT name FROM doctors WHERE doctor_id=?", (doctor_id,)).fetchone()
    return row["name"] if row else None


def _own_appointment(conn, patient_id: str, appointment_id: str):
    """Ownership check BEFORE any card is shown, so nothing about someone else's appointment is ever revealed."""
    row = conn.execute("SELECT * FROM appointments WHERE appointment_id=? AND patient_id=?", (appointment_id, patient_id)).fetchone()
    if row is None:
        return None, _refused(Refusal("NOT_YOUR_APPOINTMENT", "That is not one of your appointments.", http_status=403))
    return row, None


# -------------------------------------------------------------------------------------------------------
@tool
def search_clinic_docs(question: str, config: RunnableConfig) -> str:
    """Answer a question about NetraCare from the clinic documents (timings, fees, booking rules, what to bring, dilation,
    procedures, doctors and their OPD days, which speciality sees which problem). Returns the answer, its sources and whether the
    documents covered it. Write the question as a complete standalone question."""
    conn = db.connect()
    try:
        age = service.patient_history(conn, _patient_id(config))["patient"]["age"]
    finally:
        conn.close()
    try:
        rows = rag.search_hybrid(question)
        res = rag.answer_question(question, rows, age)
    except Exception:
        return _json(result="error", message=rag.CHAT_ERROR)
    return _json(result="answered" if res["answered"] else "idk", answer=res["answer"], sources=res["sources"])


@tool
def list_doctors() -> str:
    """List NetraCare's doctors with their IDs, specialities and OPD days (the days and sessions they see patients)."""
    conn = db.connect()
    try:
        sched: dict = {}
        for r in conn.execute("SELECT * FROM weekly_schedule WHERE status='OPD' ORDER BY doctor_id"):
            sched.setdefault(r["doctor_id"], {}).setdefault(r["day"], []).append(r["session"])
        order = {d: i for i, d in enumerate(DAYS)}
        docs = []
        for d in conn.execute("SELECT * FROM doctors ORDER BY doctor_id"):
            days = sched.get(d["doctor_id"], {})
            docs.append({"doctor_id": d["doctor_id"], "name": d["name"], "specialities": d["specialities"].split(";"),
                         "opd_days": {k: "+".join(sorted(v)) for k, v in sorted(days.items(), key=lambda kv: order[kv[0]])}})
        return _json(result="ok", doctors=docs)
    finally:
        conn.close()


@tool
def find_slots(visit_type: Literal["new", "follow_up"], start_date: str, end_date: str, speciality: Optional[str] = None,
               doctor_id: Optional[str] = None, session: Optional[Literal["AM", "PM"]] = None) -> str:
    """Find free appointment start times, earliest first, at most 10. Give a speciality OR a doctor_id (or both), a visit_type
    ('new' = 20 min, 'follow_up' = 10 min), and a date range (YYYY-MM-DD, within the next 30 days). Optionally limit to the
    morning (AM) or afternoon (PM) session. If nothing is free, the result says why on each day the doctor is unavailable."""
    conn = db.connect()
    try:
        today = config.clinic_now().date()
        try:
            first, last = Date.fromisoformat(start_date), Date.fromisoformat(end_date)
        except ValueError:
            return _json(result="error", message="Dates must be YYYY-MM-DD.")
        first = max(first, today)
        last = min(last, today + timedelta(days=config.BOOKING_WINDOW_DAYS))
        if last < first:
            return _json(result="none", message="No dates in the bookable window (today up to 30 days ahead).")
        if not speciality and not doctor_id:
            return _json(result="error", message="Give a speciality or a doctor_id.")
        if doctor_id:
            doc = conn.execute("SELECT * FROM doctors WHERE doctor_id=?", (doctor_id,)).fetchone()
            if doc is None:
                return _json(result="error", message="That doctor does not exist.")
            doctors = [doc]
            speciality = speciality or doc["specialities"].split(";")[0]
        else:
            doctors = [d for d in conn.execute("SELECT * FROM doctors ORDER BY doctor_id") if speciality in d["specialities"].split(";")]
            if not doctors:
                return _json(result="error", message=f"No NetraCare doctor covers '{speciality}'.")
        slots, unavailable, day = [], [], first
        while day <= last and len(slots) < 10:
            day_slots = []
            for d in doctors:
                times = rules.free_slots(conn, d["doctor_id"], speciality, visit_type, day.isoformat())
                if session:
                    times = [t for t in times if (t < "13:00") == (session == "AM")]
                for t in times:
                    day_slots.append((t, d["doctor_id"], d["name"]))
                if not times and len(unavailable) < 8:
                    reason = _why_unavailable(conn, d["doctor_id"], speciality, visit_type, day.isoformat(), session)
                    if reason:
                        unavailable.append({"date": day.isoformat(), "weekday": DAYS[day.weekday()], "doctor": d["name"], **reason})
            for t, did, dname in sorted(day_slots):
                if len(slots) < 10:
                    slots.append({"doctor_id": did, "doctor": dname, "date": day.isoformat(), "weekday": DAYS[day.weekday()],
                                  "start_time": t, "minutes": _mins(visit_type)})
            day += timedelta(days=1)
        if not slots:
            return _json(result="none", message="No free slots in that range.", unavailable=unavailable, visit_type=visit_type)
        return _json(result="ok", slots=slots, unavailable=unavailable, visit_type=visit_type)
    finally:
        conn.close()


def _why_unavailable(conn, doctor_id, speciality, visit_type, date_str, session) -> Optional[dict]:
    """Ask the SAME rules check why a doctor has no slots that day (probing a morning and an afternoon time)."""
    probes = ["10:00", "15:00"] if session is None else (["10:00"] if session == "AM" else ["15:00"])
    for probe in probes:
        try:
            rules.check_slot(conn, doctor_id, speciality, visit_type, date_str, probe)
            return {"reason_code": "FULLY_BOOKED", "message": "No free times left that day."}
        except Refusal as r:
            if r.code in ("DOUBLE_BOOKED", "IN_PAST"):
                continue
            return {"reason_code": r.code, "message": r.message}
    return None


@tool
def my_appointments(config: RunnableConfig) -> str:
    """List the logged-in patient's upcoming appointments (with their IDs) and any change requests waiting for Staff."""
    conn = db.connect()
    try:
        pid = _patient_id(config)
        now = datetime.fromisoformat(f"{_today()}T{_now_time()}")
        rows = conn.execute(
            "SELECT a.*, d.name AS doctor_name, (SELECT action FROM approvals ap WHERE ap.appointment_id=a.appointment_id "
            "AND ap.status='pending') AS pending_request FROM appointments a JOIN doctors d USING(doctor_id) "
            "WHERE a.patient_id=? AND a.status='booked' ORDER BY a.date, a.start_time", (pid,)).fetchall()
        upcoming = [{"appointment_id": r["appointment_id"], "doctor_id": r["doctor_id"], "doctor": r["doctor_name"],
                     "speciality": r["speciality"], "visit_type": r["visit_type"], "date": r["date"],
                     "weekday": DAYS[Date.fromisoformat(r["date"]).weekday()], "start_time": r["start_time"],
                     "waiting_for_staff": r["pending_request"]} for r in rows
                    if datetime.fromisoformat(f"{r['date']}T{r['end_time']}") >= now]
        return _json(result="ok", appointments=upcoming)
    finally:
        conn.close()


def _today() -> str:
    return config.clinic_now().date().isoformat()


def _now_time() -> str:
    return config.clinic_now().strftime("%H:%M")


# -------------------------------------------------------------------------------------------------------
# The three action tools. Each one is two steps:
#   1. the TOOL asks the patient to confirm (interrupt) and nothing changes until they do;
#   2. do_book / do_cancel / do_reschedule run AFTER the confirmation, through the same API functions as the website.
# The do_* functions hold no LLM and no graph, so they can be tested directly against the real API (tests/test_iteration4_tools.py).
# -------------------------------------------------------------------------------------------------------
def _confirm(action: str, summary: str) -> bool:
    return bool(interrupt({"action": action, "summary": summary}))


def do_book(conn, patient_id: str, doctor_id: str, speciality: str, visit_type: str, date: str, start_time: str, reason: str) -> str:
    name = _doctor_name(conn, doctor_id)
    if name is None:
        return _refused(Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404))
    try:
        res = ops.create_booking(conn, patient_id, doctor_id, speciality, visit_type, date, start_time, reason)
    except Refusal as r:
        return _refused(r)
    return _json(result="booked", appointment_id=res["appointment_id"], doctor=name, speciality=speciality,
                 visit_type=visit_type, date=date, start_time=start_time, emergency=res["emergency"])


def do_cancel(conn, patient_id: str, appointment_id: str) -> str:
    _, err = _own_appointment(conn, patient_id, appointment_id)
    if err:
        return err
    try:
        service.cancel(conn, appointment_id, "patient", patient_id)
    except Refusal as r:
        if r.code == "INSIDE_2_HOURS":  # sent to Staff; the appointment stays booked
            return _json(result="to_staff", reason_code=r.code, approval_id=r.extra.get("approval_id"), message=r.message)
        return _refused(r)
    return _json(result="cancelled", appointment_id=appointment_id)


def do_reschedule(conn, patient_id: str, appointment_id: str, doctor_id: str, date: str, start_time: str) -> str:
    appt, err = _own_appointment(conn, patient_id, appointment_id)
    if err:
        return err
    new_name = _doctor_name(conn, doctor_id)
    if new_name is None:
        return _refused(Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404))
    try:
        new_id = service.reschedule(conn, appointment_id, "patient", patient_id, doctor_id, appt["speciality"], appt["visit_type"], date, start_time)
    except Refusal as r:
        if r.code == "INSIDE_2_HOURS":
            return _json(result="to_staff", reason_code=r.code, approval_id=r.extra.get("approval_id"), message=r.message)
        return _refused(r)
    return _json(result="rescheduled", old_appointment_id=appointment_id, new_appointment_id=new_id, doctor=new_name,
                 date=date, start_time=start_time)


@tool
def book_appointment(doctor_id: str, speciality: str, visit_type: Literal["new", "follow_up"], date: str, start_time: str,
                     reason: str, config: RunnableConfig) -> str:
    """Book an appointment for the logged-in patient. Needs the doctor_id, speciality, visit_type, date (YYYY-MM-DD), start_time
    (HH:MM, a time returned by find_slots) and the patient's reason for the visit. The patient is asked to confirm first; nothing is
    booked until they do. The clinic API decides whether it is allowed."""
    conn = db.connect()
    try:
        name = _doctor_name(conn, doctor_id)
        if name is None:
            return _refused(Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404))
        summary = f"Book {name}, {speciality} {_visit_label(visit_type)}, {fmt_date(date)}, {fmt_time(start_time)} ({_mins(visit_type)} min)?"
        if not _confirm("book", summary):
            return _json(result="declined", message="The patient said no. Nothing was changed.")
        return do_book(conn, _patient_id(config), doctor_id, speciality, visit_type, date, start_time, reason)
    finally:
        conn.close()


@tool
def cancel_appointment(appointment_id: str, config: RunnableConfig) -> str:
    """Cancel one of the logged-in patient's appointments by its ID (from my_appointments). The patient is asked to confirm first.
    A cancel inside 2 hours of the appointment is sent to Staff for approval instead, and the appointment stays booked."""
    conn = db.connect()
    try:
        pid = _patient_id(config)
        appt, err = _own_appointment(conn, pid, appointment_id)  # checked BEFORE any card, so nothing about someone else's appointment is shown
        if err:
            return err
        summary = (f"Cancel your {appt['speciality']} appointment with {_doctor_name(conn, appt['doctor_id'])}, "
                   f"{fmt_date(appt['date'])}, {fmt_time(appt['start_time'])}?")
        if not _confirm("cancel", summary):
            return _json(result="declined", message="The patient said no. Nothing was changed.")
        return do_cancel(conn, pid, appointment_id)
    finally:
        conn.close()


@tool
def reschedule_appointment(appointment_id: str, doctor_id: str, date: str, start_time: str, config: RunnableConfig) -> str:
    """Move one of the logged-in patient's appointments (by ID) to a new doctor, date (YYYY-MM-DD) and start_time (HH:MM, from
    find_slots). The speciality and visit type stay the same. One confirmation covers the change. If the new time is refused the
    original stays. A change inside 2 hours of the appointment is sent to Staff for approval."""
    conn = db.connect()
    try:
        pid = _patient_id(config)
        appt, err = _own_appointment(conn, pid, appointment_id)
        if err:
            return err
        new_name = _doctor_name(conn, doctor_id)
        if new_name is None:
            return _refused(Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404))
        summary = (f"Move your {appt['speciality']} appointment ({fmt_date(appt['date'])}, {fmt_time(appt['start_time'])}) to "
                   f"{new_name}, {fmt_date(date)}, {fmt_time(start_time)} ({_mins(appt['visit_type'])} min)?")
        if not _confirm("reschedule", summary):
            return _json(result="declined", message="The patient said no. Nothing was changed.")
        return do_reschedule(conn, pid, appointment_id, doctor_id, date, start_time)
    finally:
        conn.close()


# -------------------------------------------------------------------------------------------------------
# Stretch goal: after a doctor's leave the PATIENT chooses the new time (the proposed one or two others), instead of waiting for Staff.
# -------------------------------------------------------------------------------------------------------
@tool
def my_rebooking_options(config: RunnableConfig) -> str:
    """If a doctor's leave moved one of the logged-in patient's appointments, show it: the new time that was proposed and up to two OTHER free times
    to choose from. Read only. Use it when the patient has a moved appointment waiting and wants to choose a time."""
    conn = db.connect()
    try:
        opts = ops.rebooking_options(conn, _patient_id(config))
        if not opts:
            return _json(result="none", message="You have no moved appointment waiting for a choice.")
        for o in opts:
            for slot in [o["proposed"]] + o["other_choices"]:
                slot["weekday"] = DAYS[Date.fromisoformat(slot["date"]).weekday()]
        return _json(result="ok", moved_appointments=opts)
    finally:
        conn.close()


def do_choose_rebooking(conn, patient_id: str, rebooking_id: int, doctor_id: str, date: str, start_time: str) -> str:
    try:
        row = leave_flow.choose(conn, patient_id, rebooking_id, doctor_id, date, start_time)
    except Refusal as r:
        return _refused(r)
    return _json(result="chosen", rebooking_id=rebooking_id, doctor=row["new_doctor"], date=row["new_date"], start_time=row["new_start"])


@tool
def choose_rebooking(rebooking_id: int, doctor_id: str, date: str, start_time: str, config: RunnableConfig) -> str:
    """Choose the new time for a moved appointment. Give the rebooking_id and the exact doctor_id, date (YYYY-MM-DD) and start_time (HH:MM) of ONE
    of the times returned by my_rebooking_options (the proposed time or one of the other choices). The patient is asked to confirm first."""
    conn = db.connect()
    try:
        pid = _patient_id(config)
        mine = [o for o in ops.rebooking_options(conn, pid) if o["rebooking_id"] == rebooking_id]
        if not mine:
            return _refused(Refusal("NOT_YOUR_REBOOKING", "There is no moved appointment of yours waiting for a choice.", http_status=403))
        name = _doctor_name(conn, doctor_id)
        if name is None:
            return _refused(Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404))
        o = mine[0]
        summary = (f"Move your {o['speciality']} appointment (was {fmt_date(o['original']['date'])}, {fmt_time(o['original']['start_time'])}) to "
                   f"{name}, {fmt_date(date)}, {fmt_time(start_time)}?")
        if not _confirm("choose_rebooking", summary):
            return _json(result="declined", message="The patient said no. Nothing was changed.")
        return do_choose_rebooking(conn, pid, rebooking_id, doctor_id, date, start_time)
    finally:
        conn.close()


TOOLS = [search_clinic_docs, list_doctors, find_slots, my_appointments, book_appointment, cancel_appointment, reschedule_appointment,
         my_rebooking_options, choose_rebooking]
