"""NetraCare clinic API. Roles are enforced HERE on every request, not in the frontend."""
import asyncio
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, config, db, events, leave_flow, llm, ops, rag, rules, service, staff_agent
from .rules import Refusal

VisitType = Literal["new", "follow_up"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Appointments start empty (data pack README). NETRACARE_SEED_APPOINTMENTS=1 loads stretch/appointments_seed.csv.
    db.init_db(with_seed_appointments=os.environ.get("NETRACARE_SEED_APPOINTMENTS") == "1")
    threading.Thread(target=rag.index_size, daemon=True).start()  # load the embedding model + retrievers now, not on the first chat message
    yield


app = FastAPI(title="NetraCare Clinic API", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(Refusal)
async def refusal_handler(request: Request, exc: Refusal):
    return JSONResponse(status_code=exc.http_status, content={"detail": exc.as_dict()})


# ---------- dependencies ----------
def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


class User(BaseModel):
    role: Literal["patient", "staff", "doctor"]
    name: str
    patient_id: Optional[str] = None
    doctor_id: Optional[str] = None


def current_user(authorization: str = Header(default=""), conn: sqlite3.Connection = Depends(get_conn)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone() if token else None
    if row is None:
        raise Refusal("NOT_LOGGED_IN", "Please log in first.", http_status=401)
    return User(role=row["role"], name=row["name"], patient_id=row["patient_id"], doctor_id=row["doctor_id"])


def require(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise Refusal("FORBIDDEN", f"This action is not available to the {user.role} role.", http_status=403)
        return user
    return dep


# ---------- schemas ----------
class LoginIn(BaseModel):
    role: Literal["patient", "staff", "doctor"]
    name: Optional[str] = None
    mobile: Optional[str] = None
    doctor_id: Optional[str] = None


class BookIn(BaseModel):
    reason: Optional[str] = None      # R3: required (checked in service.clean_reason), max 300 characters
    patient_id: Optional[str] = None  # staff only; a patient always books for themselves
    doctor_id: str
    speciality: str
    visit_type: VisitType = "new"
    date: str
    start_time: str


class RescheduleIn(BaseModel):
    doctor_id: str
    speciality: str
    visit_type: VisitType = "new"
    date: str
    start_time: str


# ---------- routes ----------
@app.get("/health")
def health():
    return {"ok": True, "clinic_now": config.clinic_now().isoformat(timespec="minutes")}


@app.post("/login")
def login(body: LoginIn, conn: sqlite3.Connection = Depends(get_conn)):
    if body.role == "patient":
        token, p = service.login_patient(conn, body.mobile or "", body.name or "")
        user = {"role": "patient", "name": p["name"], "patient_id": p["patient_id"]}
    elif body.role == "doctor":
        token, d = service.login_doctor(conn, body.doctor_id or "")
        user = {"role": "doctor", "name": d["name"], "doctor_id": d["doctor_id"]}
    else:
        token, name = service.login_staff(conn, body.name or "")
        user = {"role": "staff", "name": name}
    return {"token": token, "user": user}


@app.get("/me")
def me(user: User = Depends(current_user)):
    return user


@app.get("/doctors")
def doctors(conn: sqlite3.Connection = Depends(get_conn)):
    """Public: the doctor list is also what the doctor login screen shows."""
    return [{"doctor_id": r["doctor_id"], "name": r["name"], "specialities": r["specialities"].split(";")}
            for r in conn.execute("SELECT * FROM doctors ORDER BY doctor_id")]


@app.get("/schedule")
def schedule(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return [dict(r) for r in conn.execute("SELECT * FROM weekly_schedule ORDER BY doctor_id")]


@app.get("/holidays")
def holidays(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return [dict(r) for r in conn.execute("SELECT * FROM holidays ORDER BY date")]


@app.get("/patients")
def patients(user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    """Staff only: needed to book for a walk-in."""
    return [dict(r) for r in conn.execute("SELECT patient_id, name, mobile FROM patients ORDER BY patient_id")]


class NewPatientIn(BaseModel):
    name: str
    mobile: str


@app.post("/patients", status_code=201)
def create_patient(body: NewPatientIn, user: User = Depends(require("staff")),
                   conn: sqlite3.Connection = Depends(get_conn)):
    """Staff only: register a brand-new walk-in so they can be booked."""
    p = service.create_patient(conn, body.mobile, body.name)
    return {"patient_id": p["patient_id"], "name": p["name"], "mobile": p["mobile"]}


@app.get("/slots/preview")
def slots_preview(speciality: str = Query(min_length=1, max_length=40), conn: sqlite3.Connection = Depends(get_conn)):
    """PUBLIC (the home page): the next free times for a speciality, with doctor names. No login and no patient data."""
    return ops.next_slots(conn, speciality)


@app.get("/slots")
def slots(doctor_id: str, date: str, speciality: str, visit_type: VisitType = "new",
          user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    times = rules.free_slots(conn, doctor_id, speciality, visit_type, date)
    return {"doctor_id": doctor_id, "date": date, "speciality": speciality, "visit_type": visit_type,
            "duration_minutes": config.DURATION[visit_type], "slots": times}


APPT_SELECT = ("SELECT a.*, d.name AS doctor_name, p.name AS patient_name, p.mobile AS patient_mobile, "
               "(SELECT status FROM approvals ap WHERE ap.appointment_id = a.appointment_id ORDER BY approval_id DESC LIMIT 1) AS approval_status, "
               "(SELECT action FROM approvals ap WHERE ap.appointment_id = a.appointment_id ORDER BY approval_id DESC LIMIT 1) AS approval_action "
               "FROM appointments a JOIN doctors d USING(doctor_id) JOIN patients p USING(patient_id)")


def _appt_out(r: sqlite3.Row, user: User) -> dict:
    out = dict(r)
    if user.role == "patient":
        out.pop("patient_mobile", None)
    if user.role != "doctor":
        out.pop("doctor_note", None)  # clinical notes are for doctors only
    return out


@app.get("/appointments")
def list_appointments(date: Optional[str] = None, doctor_id: Optional[str] = None, status: Optional[str] = None,
                      user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    where, args = [], []
    if user.role == "patient":  # forced by the backend: a patient can only ever list their own
        where.append("a.patient_id = ?"); args.append(user.patient_id)
    elif user.role == "doctor":
        where.append("a.doctor_id = ?"); args.append(user.doctor_id)
    if user.role != "staff":
        where.append("a.status != 'proposed'")  # a proposal after doctor leave is only visible once Staff approve it
    if user.role == "staff" and doctor_id:
        where.append("a.doctor_id = ?"); args.append(doctor_id)
    if date:
        where.append("a.date = ?"); args.append(date)
    if status:
        where.append("a.status = ?"); args.append(status)
    sql = APPT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY a.date, a.start_time"
    return [_appt_out(r, user) for r in conn.execute(sql, args)]


def _one(conn, appointment_id, user):
    r = conn.execute(APPT_SELECT + " WHERE a.appointment_id = ?", (appointment_id,)).fetchone()
    return _appt_out(r, user)


@app.post("/appointments", status_code=201)
async def create_appointment(body: BookIn, user: User = Depends(require("patient", "staff")),
                             conn: sqlite3.Connection = Depends(get_conn)):
    if user.role == "patient":
        patient_id = user.patient_id  # any patient_id in the body is ignored
    else:
        if not body.patient_id:
            raise Refusal("PATIENT_REQUIRED", "Staff must choose which patient the booking is for.", http_status=422)
        patient_id = body.patient_id
    # ONE booking path (shared with the agent's tool): reason -> red-flag check -> urgent case -> rules check -> insert
    # The slow part (the AI's red-flag check) is awaited, so while it runs the server keeps serving everyone else; the rest is quick database work.
    reason = service.clean_reason(body.reason)
    try:
        flag = await llm.acheck_red_flags(reason)
    except llm.RedFlagCheckError:
        raise Refusal("RED_FLAG_CHECK_FAILED", llm.CHECK_FAILED_MESSAGE, http_status=503)
    result = await run_in_threadpool(ops.create_booking, conn, patient_id, body.doctor_id, body.speciality, body.visit_type, body.date,
                                     body.start_time, body.reason, flag)
    out = await run_in_threadpool(_one, conn, result["appointment_id"], user)
    out["emergency"] = result["emergency"]
    return out


@app.post("/appointments/{appointment_id}/cancel")
def cancel_appointment(appointment_id: str, user: User = Depends(require("patient", "staff")),
                       conn: sqlite3.Connection = Depends(get_conn)):
    service.cancel(conn, appointment_id, user.role, user.patient_id)
    return _one(conn, appointment_id, user)


@app.post("/appointments/{appointment_id}/reschedule")
def reschedule_appointment(appointment_id: str, body: RescheduleIn, user: User = Depends(require("patient", "staff")),
                           conn: sqlite3.Connection = Depends(get_conn)):
    new_id = service.reschedule(conn, appointment_id, user.role, user.patient_id, body.doctor_id,
                                body.speciality, body.visit_type, body.date, body.start_time)
    return _one(conn, new_id, user)


class CompleteIn(BaseModel):
    note: Optional[str] = None  # R2: required


@app.post("/appointments/{appointment_id}/complete")
def complete_appointment(appointment_id: str, user: User = Depends(require("doctor")),
                         conn: sqlite3.Connection = Depends(get_conn), body: CompleteIn = CompleteIn()):
    service.complete(conn, appointment_id, user.doctor_id, body.note)
    return _one(conn, appointment_id, user)


# ---------- stretch: Staff approval queue ----------
@app.get("/staff/approvals")
def staff_approvals(include_decided: bool = False, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    return service.list_approvals(conn, include_decided)


@app.post("/staff/approvals/{approval_id}/approve")
def staff_approve(approval_id: int, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    return service.approve_request(conn, approval_id, user.name)


@app.post("/staff/approvals/{approval_id}/reject")
def staff_reject(approval_id: int, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    return service.reject_request(conn, approval_id, user.name)


# ---------- stretch: Staff can add holidays and edit a doctor's weekly schedule ----------
class HolidayIn(BaseModel):
    date: str
    name: str


class ScheduleIn(BaseModel):
    doctor_id: str
    day: Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    session: Literal["AM", "PM"]
    status: Literal["OPD", "SURGERY", "OFF"]


@app.post("/holidays", status_code=201)
def add_holiday(body: HolidayIn, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    return service.add_holiday(conn, body.date, body.name)


@app.delete("/holidays/{date}")
def remove_holiday(date: str, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    service.remove_holiday(conn, date)
    return {"removed": date}


@app.put("/schedule")
def edit_schedule(body: ScheduleIn, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    return service.set_schedule(conn, body.doctor_id, body.day, body.session, body.status)


# ---------- stretch: week view calendar for each doctor ----------
@app.get("/calendar")
def calendar(doctor_id: Optional[str] = None, date: Optional[str] = None, user: User = Depends(require("staff", "doctor")),
             conn: sqlite3.Connection = Depends(get_conn)):
    """Staff may open any doctor's week; a doctor only their own (any doctor_id they send is ignored)."""
    target = user.doctor_id if user.role == "doctor" else doctor_id
    if not target:
        raise Refusal("DOCTOR_REQUIRED", "Choose a doctor.", http_status=422)
    return service.week_calendar(conn, target, date or config.clinic_now().date().isoformat(), show_patients=True)


# ---------- iteration 2: LLM features ----------
@app.get("/fact-of-the-day")
def fact_of_the_day(previous: Optional[str] = Query(default=None, max_length=500), refresh: bool = False,
                    user: User = Depends(require("patient")), conn: sqlite3.Connection = Depends(get_conn)):
    """R5 + saved facts. Login shows the fact saved for this patient instantly (no AI call).
    Refresh (`refresh=true`, or `previous` = the fact on screen) asks the model for a DIFFERENT fact and saves it as the replacement."""
    saved = service.get_saved_fact(conn, user.patient_id)
    if saved and not refresh and not previous:
        return {"fact": saved, "caveat": llm.CAVEAT, "source": "saved"}  # the caveat always comes from code
    result = llm.fact_of_the_day(previous or saved)
    if result["source"] == "ai":  # a fixed fallback fact is never saved, so the next login tries the AI again
        service.save_fact(conn, user.patient_id, result["fact"])
    return result


def _summary_context(conn, patient_id: str, appointment_id: str, user: User):
    """R6 access rules, shared by the plain and the streaming endpoint: doctors only, and only for a patient on THEIR OWN schedule."""
    appt = service.get_appointment(conn, appointment_id)
    if appt["doctor_id"] != user.doctor_id or appt["patient_id"] != patient_id:
        raise Refusal("NOT_YOUR_APPOINTMENT", "You can only open patients from your own schedule.", http_status=403)
    if appt["status"] == "cancelled":
        raise Refusal("NOT_ACTIVE", "That appointment was cancelled.")
    hist = service.patient_history(conn, patient_id)
    p = hist["patient"]
    meta = {"patient": p, "today_reason": appt["reason"], "label": "AI summary of recorded notes. Check the notes below.",
            "notes": hist["visits"]}
    return meta, (p["age"], p["general_health"], p["allergies"], appt["reason"], hist["visits"])


@app.get("/doctor/patients/{patient_id}/summary")
def doctor_patient_summary(patient_id: str, appointment_id: str, user: User = Depends(require("doctor")),
                           conn: sqlite3.Connection = Depends(get_conn)):
    meta, args = _summary_context(conn, patient_id, appointment_id, user)
    result = llm.summarise_patient(*args)
    return {**meta, "summary": result["summary"], "structured": result["structured"], "source": result["source"]}


@app.get("/doctor/patients/{patient_id}/summary/stream")
def doctor_patient_summary_stream(patient_id: str, appointment_id: str, user: User = Depends(require("doctor")),
                                  conn: sqlite3.Connection = Depends(get_conn)):
    """Same summary, streamed as server-sent events: first `meta` (profile + the full past notes), then `partial` events
    while the model writes, then one `final` event with the checked summary. Access is checked BEFORE the stream starts."""
    meta, args = _summary_context(conn, patient_id, appointment_id, user)

    def events():
        yield "data: " + json.dumps({"type": "meta", **meta}) + "\n\n"
        for ev in llm.stream_summary(*args):
            yield "data: " + json.dumps(ev) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})  # no buffering, so text appears as it is written


# ---------- iteration 3/4: the patient chat (now a booking agent) and the Staff search debug view ----------
class ChatIn(BaseModel):
    thread_id: str                    # the browser sends only the new message and the thread_id; the server keeps the conversation
    message: Optional[str] = None     # a typed message ...
    confirm: Optional[bool] = None    # ... or the answer from the confirmation card (Confirm = true, No = false)
    mode: Literal["single", "supervisor"] = "single"   # stretch goal: "supervisor" = an information agent and a booking agent behind a router


@app.post("/chat")
async def chat_endpoint(body: ChatIn, user: User = Depends(require("patient"))):
    """Only patients can chat (the role is checked here). The patient's ID comes from the login, never from the request."""
    try:
        thread_id = agent.clean_thread_id(body.thread_id)
    except ValueError:
        raise Refusal("BAD_THREAD", "Invalid chat id.", http_status=422)
    if body.confirm is None:
        message = " ".join((body.message or "").split())
        if not message:
            raise Refusal("MESSAGE_REQUIRED", "Please type a message.", http_status=422)
        if len(message) > agent.MAX_MESSAGE_CHARS:
            raise Refusal("MESSAGE_TOO_LONG", f"Please keep your message to {agent.MAX_MESSAGE_CHARS} characters or fewer.", http_status=422)
        return await agent.arun_turn(user.patient_id, thread_id, message=message, mode=body.mode)
    return await agent.arun_turn(user.patient_id, thread_id, confirm=body.confirm, mode=body.mode)


@app.post("/chat/stream")
async def chat_stream(body: ChatIn, user: User = Depends(require("patient"))):
    """The same chat turn as POST /chat, but streamed (server-sent events): `step` events with a short sentence while the agent works
    ("Checking Dr. Shah's free times…"), then one `final` event with exactly what POST /chat would have returned. The checks (role, thread id,
    message length) happen BEFORE the stream starts, so a refusal is a normal error."""
    try:
        thread_id = agent.clean_thread_id(body.thread_id)
    except ValueError:
        raise Refusal("BAD_THREAD", "Invalid chat id.", http_status=422)
    message = None
    if body.confirm is None:
        message = " ".join((body.message or "").split())
        if not message:
            raise Refusal("MESSAGE_REQUIRED", "Please type a message.", http_status=422)
        if len(message) > agent.MAX_MESSAGE_CHARS:
            raise Refusal("MESSAGE_TOO_LONG", f"Please keep your message to {agent.MAX_MESSAGE_CHARS} characters or fewer.", http_status=422)

    async def events_out():
        async for ev in agent.astream_turn(user.patient_id, thread_id, message=message, confirm=body.confirm, mode=body.mode):
            yield "data: " + json.dumps(ev) + "\n\n"

    return StreamingResponse(events_out(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _staff_chat_input(body: "ChatIn"):
    try:
        thread_id = agent.clean_thread_id(body.thread_id)
    except ValueError:
        raise Refusal("BAD_THREAD", "Invalid chat id.", http_status=422)
    message = None
    if body.confirm is None:
        message = " ".join((body.message or "").split())
        if not message:
            raise Refusal("MESSAGE_REQUIRED", "Please type a message.", http_status=422)
        if len(message) > agent.MAX_MESSAGE_CHARS:
            raise Refusal("MESSAGE_TOO_LONG", f"Please keep your message to {agent.MAX_MESSAGE_CHARS} characters or fewer.", http_status=422)
    return thread_id, message


@app.post("/staff/chat")
async def staff_chat(body: ChatIn, user: User = Depends(require("staff"))):
    """The Staff assistant (a second agent). Only Staff. The staff member's name comes from the login, never from the request.
    Same body and answer as the patient chat: {thread_id, message} or {thread_id, confirm}; every change waits for Confirm."""
    thread_id, message = _staff_chat_input(body)
    return await staff_agent.arun_turn(user.name, thread_id, message=message, confirm=body.confirm)


@app.post("/staff/chat/stream")
async def staff_chat_stream(body: ChatIn, user: User = Depends(require("staff"))):
    """The same, streamed: `step` sentences while the assistant works, then one `final` event."""
    thread_id, message = _staff_chat_input(body)

    async def events_out():
        async for ev in staff_agent.astream_turn(user.name, thread_id, message=message, confirm=body.confirm):
            yield "data: " + json.dumps(ev) + "\n\n"

    return StreamingResponse(events_out(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/staff/search-debug")
def search_debug(q: str = Query(min_length=1, max_length=500), user: User = Depends(require("staff"))):
    """R2: the top 4 chunks from keyword only, vector only and hybrid search, each with file, section and score."""
    try:
        return rag.search_debug(q)
    except rag.IndexNotBuilt:
        raise Refusal("INDEX_NOT_BUILT", "The knowledge base is not built yet. Run: python build_index.py", http_status=503)


# ---------- iteration 4 (R7): doctor leave and Staff rebooking ----------
class LeaveIn(BaseModel):
    date: str
    session: Literal["AM", "PM", "FULL"]


@app.post("/doctor/leave", status_code=201)
def doctor_leave(body: LeaveIn, user: User = Depends(require("doctor")), conn: sqlite3.Connection = Depends(get_conn)):
    return leave_flow.start(user.doctor_id, body.date, body.session)   # the leave runs as a LangGraph workflow that waits for the decisions


@app.get("/doctor/leave")
def doctor_leaves(user: User = Depends(require("doctor")), conn: sqlite3.Connection = Depends(get_conn)):
    return ops.list_leaves(conn, user.doctor_id)


@app.get("/doctor/leave/{leave_id}/progress")
def doctor_leave_progress(leave_id: int, user: User = Depends(require("doctor", "staff")), conn: sqlite3.Connection = Depends(get_conn)):
    """Where the leave workflow is: still waiting for decisions (which rebookings), or finished. A doctor sees only their own leaves."""
    leave = conn.execute("SELECT * FROM leaves WHERE leave_id=?", (leave_id,)).fetchone()
    if leave is None or (user.role == "doctor" and leave["doctor_id"] != user.doctor_id):
        raise Refusal("NOT_FOUND", "That leave does not exist.", http_status=404)
    state = leave_flow.progress(leave_id)
    if state is None:
        raise Refusal("NO_WORKFLOW", "This leave was made before workflows existed.", http_status=404)
    return state


@app.get("/staff/rebookings")
def staff_rebookings(include_decided: bool = False, user: User = Depends(require("staff")),
                     conn: sqlite3.Connection = Depends(get_conn)):
    return ops.list_rebookings(conn, include_decided)


@app.post("/staff/rebookings/{rebooking_id}/approve")
def staff_rebooking_approve(rebooking_id: int, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    return leave_flow.decide(conn, rebooking_id, user.name, True)


@app.post("/staff/rebookings/{rebooking_id}/reject")
def staff_rebooking_reject(rebooking_id: int, user: User = Depends(require("staff")), conn: sqlite3.Connection = Depends(get_conn)):
    return leave_flow.decide(conn, rebooking_id, user.name, False)


@app.get("/staff/events")
async def staff_events(request: Request, user: User = Depends(require("staff"))):
    """Live updates for the Staff dashboard (server-sent events). The server pushes one small event the moment an urgent case, an approval request,
    a rebooking or an appointment changes, so the dashboard does not have to ask every few seconds. An event only says WHAT kind of thing changed
    (never any patient detail): the dashboard then reads its own lists as usual. A `ping` every 15 seconds keeps the connection alive."""
    async def stream():
        queue = events.subscribe()
        try:
            yield "data: " + json.dumps({"type": "hello"}) + "\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    event = {"type": "ping"}
                yield "data: " + json.dumps(event) + "\n\n"
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/urgent-cases")
def urgent_cases(include_seen: bool = False, user: User = Depends(require("staff")),
                 conn: sqlite3.Connection = Depends(get_conn)):
    return service.list_urgent_cases(conn, include_seen)


@app.post("/urgent-cases/{urgent_id}/seen")
def urgent_case_seen(urgent_id: int, user: User = Depends(require("staff")),
                     conn: sqlite3.Connection = Depends(get_conn)):
    return service.mark_urgent_seen(conn, urgent_id, user.name)
