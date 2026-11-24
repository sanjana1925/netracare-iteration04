"""Iteration 4 operations built on top of service.py:

  * create_booking      the ONE booking path, shared by POST /appointments and the agent's book_appointment tool
  * Doctor leave (R7)   mark leave, cancel the affected appointments, propose new times, Staff approve or reject.
                        This is a fixed set of steps, so it is plain code, not an agent.

(Staff approvals of a patient's late cancel or reschedule, and reschedule itself, are in service.py: R1.)
"""
from datetime import timedelta

from . import events
from . import config, llm, rules, service
from .rules import Refusal
from .service import transaction


# =====================================================================================================
# One booking path
# =====================================================================================================
def create_booking(conn, patient_id, doctor_id, speciality, visit_type, date, start, reason_text, flag=None) -> dict:
    """Reason check -> red-flag check -> urgent case -> iteration 1 rules check -> insert.

    Returns {appointment_id, emergency}. Raises Refusal exactly like the endpoint (incl. RED_FLAG_CHECK_FAILED)."""
    reason = service.clean_reason(reason_text)
    # the red-flag check runs BEFORE any clinic rule. If it cannot run, nothing is booked.
    # (`flag` is already filled in when the async endpoint has done the check; otherwise it is done here.)
    if flag is None:
        try:
            flag = llm.check_red_flags(reason)
        except llm.RedFlagCheckError:
            raise Refusal("RED_FLAG_CHECK_FAILED", llm.CHECK_FAILED_MESSAGE, http_status=503)
    emergency = None
    if flag.red_flag:
        service.require_patient(conn, patient_id)  # an unknown patient is a clean 404, not a database error
        urgent_id = service.save_urgent_case(conn, patient_id, reason, flag.category, flag.why)  # stands even if the booking is refused
        emergency = {"message": llm.EMERGENCY_RESPONSE, "category": flag.category, "urgent_id": urgent_id}
    # The LLM never decides the booking: it still goes through the iteration 1 rules check.
    try:
        appt_id = service.book(conn, patient_id, doctor_id, speciality, visit_type, date, start, reason, urgent=flag.red_flag)
    except Refusal as refusal:
        refusal.emergency = emergency
        raise
    return {"appointment_id": appt_id, "emergency": emergency}


# =====================================================================================================
# Public preview of the next free times (the home page's booking card). Times and doctor names only: no patient data.
# =====================================================================================================
def next_slots(conn, speciality: str, limit: int = 6) -> dict:
    """The earliest free NEW-consultation start times for a speciality across all its doctors (the same rules check as /slots)."""
    from datetime import timedelta
    doctors = [d for d in conn.execute("SELECT * FROM doctors ORDER BY doctor_id") if speciality in d["specialities"].split(";")]
    if not doctors:
        raise Refusal("SPECIALITY_NOT_FOUND", f"No NetraCare doctor covers '{speciality[:40]}'.", http_status=404)
    today, slots = config.clinic_now().date(), []
    for i in range(config.BOOKING_WINDOW_DAYS + 1):
        day = today + timedelta(days=i)
        found = sorted(((t, d) for d in doctors for t in rules.free_slots(conn, d["doctor_id"], speciality, "new", day.isoformat())),
                       key=lambda x: (x[0], x[1]["doctor_id"]))  # earliest first; a tie goes to the lower doctor ID
        for t, d in found:
            slots.append({"date": day.isoformat(), "weekday": rules.DAYS[day.weekday()], "start_time": t,
                          "doctor_id": d["doctor_id"], "doctor": d["name"], "minutes": config.DURATION["new"]})
        if len(slots) >= limit:
            break
    return {"speciality": speciality, "doctors": [{"doctor_id": d["doctor_id"], "name": d["name"]} for d in doctors], "slots": slots[:limit]}


# =====================================================================================================
# R7: doctor leave and Staff rebooking
# =====================================================================================================
def _session_name(start_time: str) -> str:
    return "AM" if rules.to_minutes(start_time) < rules.to_minutes("13:00") else "PM"


def candidate_slots(conn, appt, limit: int = 1, after=None):
    """The earliest free slots for an appointment hit by leave, best first: a list of (doctor_id, date, start), at most `limit`.

    New consultation: any doctor covering the speciality, at or after the original time (tie: the original doctor,
    then the lower doctor ID). Follow-up: the SAME doctor's times. Looks up to 30 days ahead of the clinic clock.
    `after` = (date, start): only slots later than that (used to offer other choices after the proposed one)."""
    doctors = {d["doctor_id"]: d for d in conn.execute("SELECT * FROM doctors ORDER BY doctor_id")}
    if appt["visit_type"] == "follow_up":
        candidates = [appt["doctor_id"]]
    else:
        covering = [i for i, d in doctors.items() if appt["speciality"] in d["specialities"].split(";")]
        candidates = sorted(covering, key=lambda i: (i != appt["doctor_id"], i))  # original doctor first, then lower ID
    last_day = config.clinic_now().date() + timedelta(days=config.BOOKING_WINDOW_DAYS)
    day = rules.parse_date(appt["date"])
    found = []
    while day <= last_day and len(found) < limit:
        day_slots = []
        for rank, doc_id in enumerate(candidates):
            for slot in rules.free_slots(conn, doc_id, appt["speciality"], appt["visit_type"], day.isoformat()):
                if day.isoformat() == appt["date"] and slot < appt["start_time"]:
                    continue
                if after and (day.isoformat(), slot) <= tuple(after):
                    continue
                day_slots.append((slot, rank, doc_id))
        for slot, _rank, doc_id in sorted(day_slots):
            found.append((doc_id, day.isoformat(), slot))
            if len(found) >= limit:
                break
        day += timedelta(days=1)
    return found


def find_proposal(conn, appt):
    """Earliest free slot for an appointment hit by leave. Returns (doctor_id, date, start) or None."""
    found = candidate_slots(conn, appt, limit=1)
    return found[0] if found else None


def mark_leave(conn, doctor_id: str, date_str: str, session: str) -> dict:
    session = (session or "").upper()
    if session not in ("AM", "PM", "FULL"):
        raise Refusal("INVALID_SESSION", "Leave must be for AM, PM or FULL day.", http_status=422)
    day = rules.parse_date(date_str)
    now = config.clinic_now()
    if day < now.date():
        raise Refusal("IN_PAST", "Leave cannot be marked for a past date.")
    if day > now.date() + timedelta(days=config.BOOKING_WINDOW_DAYS):
        raise Refusal("BEYOND_30_DAYS", "Leave can be marked up to 30 days ahead.")
    doctor = conn.execute("SELECT * FROM doctors WHERE doctor_id=?", (doctor_id,)).fetchone()
    covered = ("AM", "PM") if session == "FULL" else (session,)
    proposals = []
    with transaction(conn):  # all or nothing: leave, cancellations and proposals are saved together
        if conn.execute("SELECT 1 FROM leaves WHERE doctor_id=? AND date=? AND (session=? OR session='FULL')",
                        (doctor_id, day.isoformat(), session)).fetchone():
            raise Refusal("LEAVE_EXISTS", "You have already marked leave for that time.")
        leave_id = conn.execute("INSERT INTO leaves (doctor_id,date,session,created_at) VALUES (?,?,?,?)",
                                (doctor_id, day.isoformat(), session, service._now_iso())).lastrowid
        affected = [r for r in conn.execute(
            "SELECT * FROM appointments WHERE doctor_id=? AND date=? AND status='booked' ORDER BY start_time, appointment_id",
            (doctor_id, day.isoformat())) if _session_name(r["start_time"]) in covered]
        note = f"Moved because {doctor['name']} is on leave"
        for appt in affected:  # in time order; each proposal holds its slot, so the next one cannot take it
            conn.execute("UPDATE appointments SET status='cancelled', moved_note=? WHERE appointment_id=?", (note, appt["appointment_id"]))
            found = find_proposal(conn, appt)
            new_id = None
            if found:
                new_doc, new_date, new_start = found
                new_id = service._next_id(conn)
                conn.execute(
                    "INSERT INTO appointments (appointment_id,patient_id,doctor_id,speciality,visit_type,date,start_time,end_time,"
                    "status,reason,urgent,moved_note) VALUES (?,?,?,?,?,?,?,?,'proposed',?,?,?)",
                    (new_id, appt["patient_id"], new_doc, appt["speciality"], appt["visit_type"], new_date, new_start,
                     rules.end_time_for(new_start, appt["visit_type"]), appt["reason"], appt["urgent"], note))
            status = "pending" if new_id else "no_slot"
            reb = conn.execute(
                "INSERT INTO rebookings (leave_id,original_appointment_id,proposed_appointment_id,patient_id,status,created_at) "
                "VALUES (?,?,?,?,?,?)", (leave_id, appt["appointment_id"], new_id, appt["patient_id"], status, service._now_iso())).lastrowid
            proposals.append({"rebooking_id": reb, "original_appointment_id": appt["appointment_id"],
                              "patient_id": appt["patient_id"], "proposed_appointment_id": new_id, "status": status})
    if proposals:
        events.publish("rebooking", change="proposed", count=len(proposals))
    return {"leave_id": leave_id, "doctor": doctor["name"], "date": day.isoformat(), "session": session,
            "affected": len(proposals), "rebookings": proposals}


def list_leaves(conn, doctor_id: str):
    return [dict(r) for r in conn.execute("SELECT * FROM leaves WHERE doctor_id=? ORDER BY date DESC, leave_id DESC", (doctor_id,))]


_REBOOK_SQL = (
    "SELECT rb.*, p.name AS patient_name, p.mobile AS patient_mobile, o.speciality AS speciality, o.visit_type AS visit_type, "
    "o.date AS original_date, o.start_time AS original_start, od.name AS original_doctor, o.doctor_id AS original_doctor_id, "
    "n.date AS new_date, n.start_time AS new_start, n.doctor_id AS new_doctor_id, nd.name AS new_doctor "
    "FROM rebookings rb JOIN patients p USING(patient_id) JOIN appointments o ON o.appointment_id = rb.original_appointment_id "
    "JOIN doctors od ON od.doctor_id = o.doctor_id "
    "LEFT JOIN appointments n ON n.appointment_id = rb.proposed_appointment_id LEFT JOIN doctors nd ON nd.doctor_id = n.doctor_id ")


def list_rebookings(conn, include_decided=False):
    where = "" if include_decided else "WHERE rb.status IN ('pending','no_slot') "
    rows = [dict(r) for r in conn.execute(_REBOOK_SQL + where + "ORDER BY rb.rebooking_id")]
    for r in rows:
        if r["status"] == "no_slot":
            r["message"] = "No slot: call the patient"
    return rows


def rebooking_options(conn, patient_id: str, other_choices: int = 2) -> list:
    """For each of this patient's moved appointments that is waiting for a decision: the time already proposed, plus up to two OTHER free times.
    Read only. The patient picks one in the chat (stretch goal: the patient chooses instead of Staff)."""
    out = []
    rows = conn.execute(
        "SELECT rb.rebooking_id, rb.original_appointment_id, rb.proposed_appointment_id FROM rebookings rb "
        "WHERE rb.patient_id=? AND rb.status='pending' ORDER BY rb.rebooking_id", (patient_id,)).fetchall()
    for rb in rows:
        orig = conn.execute("SELECT a.*, d.name AS doctor_name FROM appointments a JOIN doctors d USING(doctor_id) WHERE a.appointment_id=?",
                            (rb["original_appointment_id"],)).fetchone()
        prop = conn.execute("SELECT a.*, d.name AS doctor_name FROM appointments a JOIN doctors d USING(doctor_id) WHERE a.appointment_id=?",
                            (rb["proposed_appointment_id"],)).fetchone()
        others = candidate_slots(conn, orig, limit=other_choices, after=(prop["date"], prop["start_time"]))
        names = {d["doctor_id"]: d["name"] for d in conn.execute("SELECT doctor_id, name FROM doctors")}

        def slot(doctor_id, date, start):
            return {"doctor_id": doctor_id, "doctor": names[doctor_id], "date": date, "start_time": start}
        out.append({"rebooking_id": rb["rebooking_id"], "speciality": orig["speciality"], "visit_type": orig["visit_type"],
                    "original": {"doctor": orig["doctor_name"], "date": orig["date"], "start_time": orig["start_time"]},
                    "why": orig["moved_note"],
                    "proposed": slot(prop["doctor_id"], prop["date"], prop["start_time"]),
                    "other_choices": [slot(*o) for o in others]})
    return out


def choose_rebooking(conn, patient_id: str, rebooking_id: int, doctor_id: str, date: str, start_time: str) -> dict:
    """The patient picks one of the offered times. The proposed time is simply approved. Another offered time replaces the proposal: the held slot
    is freed, the new time is checked by the SAME clinic rules and booked. Anything that was not offered is refused."""
    options = [o for o in rebooking_options(conn, patient_id) if o["rebooking_id"] == rebooking_id]
    if not options:
        raise Refusal("NOT_YOUR_REBOOKING", "There is no moved appointment of yours waiting for a choice.", http_status=403)
    opt = options[0]
    chosen = (doctor_id, date, start_time)
    offered = [opt["proposed"]] + opt["other_choices"]
    if chosen not in [(o["doctor_id"], o["date"], o["start_time"]) for o in offered]:
        raise Refusal("NOT_OFFERED", "That time was not one of the times offered. Please pick one of them.")
    if chosen == (opt["proposed"]["doctor_id"], opt["proposed"]["date"], opt["proposed"]["start_time"]):
        return decide_rebooking(conn, rebooking_id, "patient", True)
    with transaction(conn):
        row = conn.execute("SELECT * FROM rebookings WHERE rebooking_id=? AND patient_id=? AND status='pending'", (rebooking_id, patient_id)).fetchone()
        if row is None:
            raise Refusal("NOT_PENDING", "That moved appointment was already decided.")
        prop = conn.execute("SELECT * FROM appointments WHERE appointment_id=?", (row["proposed_appointment_id"],)).fetchone()
        conn.execute("UPDATE appointments SET status='cancelled' WHERE appointment_id=?", (prop["appointment_id"],))  # frees the held slot
        rules.check_booking(conn, patient_id, doctor_id, prop["speciality"], prop["visit_type"], date, start_time)  # the same rules as every booking
        new_id = service._insert(conn, patient_id, doctor_id, prop["speciality"], prop["visit_type"], date, start_time, prop["reason"], bool(prop["urgent"]))
        conn.execute("UPDATE appointments SET moved_note=? WHERE appointment_id=?", (prop["moved_note"], new_id))
        conn.execute("UPDATE rebookings SET status='approved', proposed_appointment_id=?, decided_at=?, decided_by='patient' WHERE rebooking_id=?",
                     (new_id, service._now_iso(), rebooking_id))
    events.publish("rebooking", change="chosen")
    return dict(conn.execute(_REBOOK_SQL + "WHERE rb.rebooking_id=?", (rebooking_id,)).fetchone())


def decide_rebooking(conn, rebooking_id: int, staff_name: str, approve: bool) -> dict:
    with transaction(conn):
        row = conn.execute("SELECT * FROM rebookings WHERE rebooking_id=?", (rebooking_id,)).fetchone()
        if row is None:
            raise Refusal("NOT_FOUND", "That rebooking does not exist.", http_status=404)
        if row["status"] == "no_slot":
            raise Refusal("NO_PROPOSAL", "There is no proposed time for this one: call the patient.")
        if row["status"] != "pending":
            raise Refusal("NOT_PENDING", f"That rebooking was already {row['status']}.")
        # Approve makes the proposal booked. Reject frees the slot.
        conn.execute("UPDATE appointments SET status=? WHERE appointment_id=?",
                     ("booked" if approve else "cancelled", row["proposed_appointment_id"]))
        conn.execute("UPDATE rebookings SET status=?, decided_at=?, decided_by=? WHERE rebooking_id=?",
                     ("approved" if approve else "rejected", service._now_iso(), staff_name, rebooking_id))
    events.publish("rebooking", change="approved" if approve else "rejected")
    return dict(conn.execute(_REBOOK_SQL + "WHERE rb.rebooking_id=?", (rebooking_id,)).fetchone())
