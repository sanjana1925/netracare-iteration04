"""The Staff assistant (stretch goal): a SECOND agent, for the front desk.

Same ideas as the patient agent, with the same safety, but a different job:

    START -> [agent] <----------+
                | tool calls    |
                v               |
             [tools] -----------+      (every change pauses at interrupt() for the staff member's Confirm / No)
                |
    no more tool calls -> END

  * Its tools are a thin layer over the same functions the Staff pages use (service.py, ops.py, leave_flow.py). A tool never checks a rule itself:
    the clinic API decides, and a refusal comes back as a normal result for the agent to explain.
  * The staff member's name comes from the login (config["configurable"]["staff_name"]); it is not a tool argument.
  * It can book for ANY patient (the booking still goes through the red-flag check and every clinic rule), cancel, approve or reject late-change
    requests and rebookings after leave, and mark urgent cases as seen. Nothing changes until the staff member presses Confirm.
  * There is no red-flag node: the person typing is Staff, not a patient describing a symptom. (A booking REASON is still checked by the API.)
"""
import asyncio
import json
import logging
from datetime import date as Date, timedelta
from typing import Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

from . import agent as patient_agent
from . import config, db, leave_flow, llm, ops, rag, service
from . import tools as ptools
from .rules import DAYS, Refusal

log = logging.getLogger("netracare.staff_agent")

STEP_LIMIT = 16
MAX_MESSAGES = 20


def _staff_name(cfg: RunnableConfig) -> str:
    return cfg["configurable"]["staff_name"]


_json = ptools._json
_refused = ptools._refused
fmt_date = ptools.fmt_date
fmt_time = ptools.fmt_time


def _confirm(action: str, summary: str) -> bool:
    return bool(interrupt({"action": action, "summary": summary}))


# ------------------------------------------------------------------------------------------------- read-only tools
@tool
def find_patient(query: str) -> str:
    """Find patients by name or mobile number (part of either). Returns up to 8 with their patient_id, name and mobile. Use it to get a patient_id."""
    q = (query or "").strip().lower()
    if len(q) < 2:
        return _json(result="error", message="Give at least two letters of the name or digits of the mobile number.")
    conn = db.connect()
    try:
        rows = [r for r in conn.execute("SELECT patient_id, name, mobile FROM patients ORDER BY name")
                if q in r["name"].lower() or q in r["mobile"]]
        return _json(result="ok" if rows else "none", patients=[dict(r) for r in rows[:8]])
    finally:
        conn.close()


@tool
def day_appointments(date: str) -> str:
    """Every appointment on one day (YYYY-MM-DD), all doctors, in time order, with its appointment_id, doctor, patient, speciality and status."""
    try:
        Date.fromisoformat(date)
    except ValueError:
        return _json(result="error", message="The date must be YYYY-MM-DD.")
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT a.appointment_id, a.start_time, a.status, a.speciality, a.visit_type, a.urgent, d.name AS doctor, p.name AS patient "
            "FROM appointments a JOIN doctors d USING(doctor_id) JOIN patients p USING(patient_id) WHERE a.date=? "
            "AND a.status IN ('booked','completed','proposed') ORDER BY a.start_time, a.appointment_id", (date,)).fetchall()
        return _json(result="ok", date=date, weekday=DAYS[Date.fromisoformat(date).weekday()], count=len(rows), appointments=[dict(r) for r in rows[:40]])
    finally:
        conn.close()


@tool
def pending_work() -> str:
    """What needs Staff right now: unseen urgent cases, late-change requests waiting for a decision, and rebookings after doctor leave waiting for a
    decision. Each item has the id to use with the decision tools."""
    conn = db.connect()
    try:
        urgent = [{"urgent_id": u["urgent_id"], "patient": u["patient_name"], "category": u["category"], "reason": u["reason"], "source": u["source"]}
                  for u in service.list_urgent_cases(conn)]
        approvals = [{"approval_id": a["approval_id"], "patient": a["patient_name"], "action": a["action"], "speciality": a["speciality"],
                      "doctor": a["doctor_name"], "date": a["date"], "start_time": a["start_time"],
                      "new_date": a["new_date"], "new_start_time": a["new_start_time"]} for a in service.list_approvals(conn)]
        rebookings = [{"rebooking_id": r["rebooking_id"], "patient": r["patient_name"], "status": r["status"], "speciality": r["speciality"],
                       "was": f"{r['original_date']} {r['original_start']} with {r['original_doctor']}",
                       "proposed": (f"{r['new_date']} {r['new_start']} with {r['new_doctor']}" if r["new_date"] else "No slot: call the patient")}
                      for r in ops.list_rebookings(conn)]
        return _json(result="ok", urgent_cases=urgent, approval_requests=approvals, rebookings=rebookings)
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------------- the do_* functions (after the confirmation)
def do_book(conn, patient_id, doctor_id, speciality, visit_type, date, start_time, reason) -> str:
    return ptools.do_book(conn, patient_id, doctor_id, speciality, visit_type, date, start_time, reason)


def do_cancel(conn, appointment_id) -> str:
    try:
        service.cancel(conn, appointment_id, "staff", None)
    except Refusal as r:
        return _refused(r)
    return _json(result="cancelled", appointment_id=appointment_id)


def do_decide_approval(conn, staff_name, approval_id, approve) -> str:
    try:
        row = (service.approve_request if approve else service.reject_request)(conn, approval_id, staff_name)
    except Refusal as r:
        return _refused(r)
    return _json(result="approved" if approve else "rejected", approval_id=approval_id, outcome=row["result"])


def do_decide_rebooking(conn, staff_name, rebooking_id, approve) -> str:
    try:
        leave_flow.decide(conn, rebooking_id, staff_name, approve)
    except Refusal as r:
        return _refused(r)
    return _json(result="approved" if approve else "rejected", rebooking_id=rebooking_id)


def do_urgent_seen(conn, staff_name, urgent_id) -> str:
    try:
        service.mark_urgent_seen(conn, urgent_id, staff_name)
    except Refusal as r:
        return _refused(r)
    return _json(result="seen", urgent_id=urgent_id)


# ------------------------------------------------------------------------------------------------- the action tools (each asks for Confirm first)
def _patient_name(conn, patient_id):
    row = conn.execute("SELECT name FROM patients WHERE patient_id=?", (patient_id,)).fetchone()
    return row["name"] if row else None


@tool
def staff_book(patient_id: str, doctor_id: str, speciality: str, visit_type: Literal["new", "follow_up"], date: str, start_time: str,
               reason: str, config: RunnableConfig) -> str:
    """Book an appointment FOR a patient (get the patient_id from find_patient and a start_time from find_slots). Needs the doctor_id, speciality,
    visit_type, date (YYYY-MM-DD), start_time (HH:MM) and the reason for the visit. The staff member confirms first. The clinic API (rules and the
    red-flag check) decides whether it is allowed."""
    conn = db.connect()
    try:
        pname, dname = _patient_name(conn, patient_id), ptools._doctor_name(conn, doctor_id)
        if pname is None:
            return _refused(Refusal("PATIENT_NOT_FOUND", "That patient does not exist.", http_status=404))
        if dname is None:
            return _refused(Refusal("DOCTOR_NOT_FOUND", "That doctor does not exist.", http_status=404))
        summary = f"Book {pname} with {dname}, {speciality} {ptools._visit_label(visit_type)}, {fmt_date(date)}, {fmt_time(start_time)}?"
        if not _confirm("staff_book", summary):
            return _json(result="declined", message="The staff member said no. Nothing was changed.")
        return do_book(conn, patient_id, doctor_id, speciality, visit_type, date, start_time, reason)
    finally:
        conn.close()


@tool
def staff_cancel(appointment_id: str, config: RunnableConfig) -> str:
    """Cancel any booked appointment by its appointment_id (from day_appointments). The staff member confirms first. Staff are not limited by the
    2-hour rule."""
    conn = db.connect()
    try:
        a = conn.execute("SELECT a.*, p.name AS patient, d.name AS doctor FROM appointments a JOIN patients p USING(patient_id) "
                         "JOIN doctors d USING(doctor_id) WHERE a.appointment_id=?", (appointment_id,)).fetchone()
        if a is None:
            return _refused(Refusal("NOT_FOUND", "That appointment does not exist.", http_status=404))
        summary = f"Cancel {a['patient']}'s {a['speciality']} appointment with {a['doctor']}, {fmt_date(a['date'])}, {fmt_time(a['start_time'])}?"
        if not _confirm("staff_cancel", summary):
            return _json(result="declined", message="The staff member said no. Nothing was changed.")
        return do_cancel(conn, appointment_id)
    finally:
        conn.close()


@tool
def decide_approval(approval_id: int, approve: bool, config: RunnableConfig) -> str:
    """Approve (approve=true) or reject (approve=false) a patient's late cancel or reschedule request, by its approval_id (from pending_work).
    The staff member confirms first. Approving carries the change out."""
    conn = db.connect()
    try:
        row = next((a for a in service.list_approvals(conn) if a["approval_id"] == approval_id), None)
        if row is None:
            return _refused(Refusal("NOT_FOUND", "There is no waiting request with that id.", http_status=404))
        what = "cancel" if row["action"] == "cancel" else "move"
        summary = (f"{'Approve' if approve else 'Reject'} {row['patient_name']}'s request to {what} their {row['speciality']} appointment "
                   f"({fmt_date(row['date'])}, {fmt_time(row['start_time'])})?")
        if not _confirm("decide_approval", summary):
            return _json(result="declined", message="The staff member said no. Nothing was changed.")
        return do_decide_approval(conn, _staff_name(config), approval_id, approve)
    finally:
        conn.close()


@tool
def decide_rebooking(rebooking_id: int, approve: bool, config: RunnableConfig) -> str:
    """Approve (approve=true) or reject (approve=false) the new time proposed after a doctor's leave, by its rebooking_id (from pending_work).
    The staff member confirms first. Approve makes the proposed time a booked appointment; reject frees it."""
    conn = db.connect()
    try:
        row = next((r for r in ops.list_rebookings(conn) if r["rebooking_id"] == rebooking_id), None)
        if row is None:
            return _refused(Refusal("NOT_FOUND", "There is no waiting rebooking with that id.", http_status=404))
        if row["status"] == "no_slot":
            return _refused(Refusal("NO_PROPOSAL", "There is no proposed time for this one: call the patient.", http_status=409))
        summary = (f"{'Approve' if approve else 'Reject'} the new time for {row['patient_name']}: {fmt_date(row['new_date'])}, "
                   f"{fmt_time(row['new_start'])} with {row['new_doctor']}?")
        if not _confirm("decide_rebooking", summary):
            return _json(result="declined", message="The staff member said no. Nothing was changed.")
        return do_decide_rebooking(conn, _staff_name(config), rebooking_id, approve)
    finally:
        conn.close()


@tool
def mark_urgent_seen(urgent_id: int, config: RunnableConfig) -> str:
    """Mark an urgent case as seen, by its urgent_id (from pending_work). The staff member confirms first."""
    conn = db.connect()
    try:
        row = next((u for u in service.list_urgent_cases(conn) if u["urgent_id"] == urgent_id), None)
        if row is None:
            return _refused(Refusal("NOT_FOUND", "There is no unseen urgent case with that id.", http_status=404))
        if not _confirm("mark_urgent_seen", f"Mark {row['patient_name']}'s urgent case ({row['category']}) as seen?"):
            return _json(result="declined", message="The staff member said no. Nothing was changed.")
        return do_urgent_seen(conn, _staff_name(config), urgent_id)
    finally:
        conn.close()


ACTION_TOOLS = {"staff_book", "staff_cancel", "decide_approval", "decide_rebooking", "mark_urgent_seen"}
TOOLS = [pending_work, find_patient, day_appointments, ptools.list_doctors, ptools.find_slots,
         staff_book, staff_cancel, decide_approval, decide_rebooking, mark_urgent_seen]


# ------------------------------------------------------------------------------------------------- the prompt
def build_system_prompt(staff_name: str) -> str:
    conn = db.connect()
    try:
        now = config.clinic_now()
        urgent = len(service.list_urgent_cases(conn))
        approvals = len(service.list_approvals(conn))
        rebookings = len(ops.list_rebookings(conn))
    finally:
        conn.close()
    return f"""You are the NetraCare Staff assistant. NetraCare is an eye clinic. You help the front-desk staff member {staff_name} run the day: find patients,
look at the day's appointments, find free times, book for a patient, cancel, decide late-change requests and rebookings after doctor leave, and mark
urgent cases as seen. Be brief and clear.

TODAY: {DAYS[now.weekday()]} {now.date().isoformat()}, {now.strftime('%H:%M')}. Bookings open up to {config.BOOKING_WINDOW_DAYS} days ahead. Clinic hours: Mon-Sat 9:00-13:00 and 14:00-18:00.
Waiting for Staff right now: {urgent} unseen urgent case(s), {approvals} late-change request(s), {rebookings} rebooking(s) after doctor leave.

HOW TO WORK
- Use tools. Never invent an id (patient, appointment, approval, rebooking or urgent case): get it from a tool first.
- To book for a patient: find_patient, then find_slots (never offer a time you have not seen in a find_slots result), then staff_book. A booking needs the
  patient's reason for the visit: ask for it if it was not given.
- Call ONE action tool at a time (staff_book, staff_cancel, decide_approval, decide_rebooking, mark_urgent_seen). The staff member sees a confirmation card;
  nothing changes until they press Confirm. Do NOT ask them to confirm in your own words: when you have everything the tool needs, CALL the tool.
  If a tool returns "declined", say nothing was changed and ask what they would like instead.
- The clinic API decides what is allowed. If a tool returns "refused", explain the message in plain words and suggest what to do. Do not try to work around it.
- Never say something was booked, cancelled, approved or rejected unless the tool result said so.
- To see what needs attention, call pending_work. Urgent cases are about patients with possible emergencies: list them first when asked what is urgent.
- Never give medical advice, name a likely condition or suggest a treatment. Only help with running the clinic."""


# ------------------------------------------------------------------------------------------------- the graph
def _one_action_at_a_time(reply):
    calls = getattr(reply, "tool_calls", None) or []
    seen, kept = False, []
    for c in calls:
        if c["name"] in ACTION_TOOLS:
            if seen:
                continue
            seen = True
        kept.append(c)
    return AIMessage(content=reply.content, tool_calls=kept, id=reply.id) if len(kept) != len(calls) else reply


def _window(messages):
    win = messages[-MAX_MESSAGES:]
    while win and not isinstance(win[0], HumanMessage):
        win = win[1:]
    return win


def agent_node(state: MessagesState, config: RunnableConfig) -> dict:
    prompt = [SystemMessage(content=build_system_prompt(config["configurable"]["staff_name"]))] + _window(state["messages"])
    ask = lambda: llm.get_model(0).bind_tools(TOOLS).invoke(prompt)
    reply = llm.invoke_with_backoff(ask)
    if not patient_agent._text(reply.content).strip() and not (getattr(reply, "tool_calls", None) or []):
        reply = llm.invoke_with_backoff(ask)
    return {"messages": [_one_action_at_a_time(reply)]}


async def aagent_node(state: MessagesState, config: RunnableConfig) -> dict:
    prompt = [SystemMessage(content=await asyncio.to_thread(build_system_prompt, config["configurable"]["staff_name"]))] + _window(state["messages"])
    ask = lambda: llm.get_model(0).bind_tools(TOOLS).ainvoke(prompt)
    reply = await llm.ainvoke_with_backoff(ask)
    if not patient_agent._text(reply.content).strip() and not (getattr(reply, "tool_calls", None) or []):
        reply = await llm.ainvoke_with_backoff(ask)
    return {"messages": [_one_action_at_a_time(reply)]}


def build_graph(checkpointer):
    g = StateGraph(MessagesState)
    g.add_node("agent", RunnableLambda(agent_node, afunc=aagent_node))
    g.add_node("tools", ToolNode(TOOLS))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)


_graph = None
_saver = None


def get_graph():
    global _graph, _saver
    if _graph is None:
        _saver = InMemorySaver()
        _graph = build_graph(_saver)
    return _graph


def reset() -> None:
    global _graph, _saver
    _graph = _saver = None


# ------------------------------------------------------------------------------------------------- one chat turn
def _cfg(staff_name: str, thread_id: str) -> dict:
    return {"configurable": {"thread_id": f"staff:{staff_name}:{thread_id}", "staff_name": staff_name}, "recursion_limit": STEP_LIMIT}


def _drop(cfg) -> None:
    try:
        _saver.delete_thread(cfg["configurable"]["thread_id"])
    except Exception:
        pass


def _compose(graph, cfg) -> dict:
    snap = graph.get_state(cfg)
    msgs = snap.values["messages"]
    start = max(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage))
    turn = msgs[start:]
    tool_names = [c["name"] for m in turn if isinstance(m, AIMessage) for c in (m.tool_calls or [])]
    pending = patient_agent._pending(graph, cfg)
    if pending:
        text = next((patient_agent._text(m.content) for m in reversed(turn) if isinstance(m, AIMessage) and m.content), "")
        return patient_agent._result("pending", text or "Please confirm this action.", pending=pending, tools=tool_names)
    reply = next((patient_agent._text(m.content) for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), "")
    return patient_agent._result("answer", reply or "Sorry, I have nothing to add.", tools=tool_names)


async def arun_turn(staff_name: str, thread_id: str, message: Optional[str] = None, confirm: Optional[bool] = None) -> dict:
    graph = get_graph()
    cfg = _cfg(staff_name, thread_id)
    try:
        waiting = patient_agent._pending(graph, cfg)
        if confirm is not None and not waiting:
            return patient_agent._result("error", "There is nothing waiting for your confirmation.")
        if waiting and confirm is None:
            await graph.ainvoke(Command(resume=False), cfg)   # a typed message while a card waits counts as No
        if confirm is not None:
            await graph.ainvoke(Command(resume=bool(confirm)), cfg)
        else:
            await graph.ainvoke({"messages": [HumanMessage(content=message)]}, cfg)
        return _compose(graph, cfg)
    except GraphRecursionError:
        _drop(cfg)
        return patient_agent._result("error", patient_agent.STEP_LIMIT_MESSAGE)
    except Exception:
        log.exception("staff agent turn failed")
        _drop(cfg)
        return patient_agent._result("error", rag.CHAT_ERROR)


def progress_text(name: str, args: dict) -> str:
    return {
        "pending_work": "Looking at what is waiting for Staff…",
        "find_patient": "Looking for the patient…",
        "day_appointments": "Looking at the day's appointments…",
        "list_doctors": "Checking the list of doctors…",
        "staff_book": "Getting the booking ready…",
        "staff_cancel": "Getting the cancellation ready…",
        "decide_approval": "Getting the decision ready…",
        "decide_rebooking": "Getting the decision ready…",
        "mark_urgent_seen": "Getting ready to mark it seen…",
    }.get(name) or patient_agent.progress_text(name, args)


async def astream_turn(staff_name: str, thread_id: str, message: Optional[str] = None, confirm: Optional[bool] = None):
    graph = get_graph()
    cfg = _cfg(staff_name, thread_id)
    try:
        waiting = patient_agent._pending(graph, cfg)
        if confirm is not None and not waiting:
            yield {"type": "final", **patient_agent._result("error", "There is nothing waiting for your confirmation.")}
            return
        if waiting and confirm is None:
            await graph.ainvoke(Command(resume=False), cfg)
    except Exception:
        log.exception("staff agent turn failed")
        _drop(cfg)
        yield {"type": "final", **patient_agent._result("error", rag.CHAT_ERROR)}
        return
    payload = Command(resume=bool(confirm)) if confirm is not None else {"messages": [HumanMessage(content=message)]}
    async for event in patient_agent.stream_graph(graph, cfg, payload, _compose, labeller=progress_text, drop=_drop):
        yield event
