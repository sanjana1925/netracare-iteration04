"""The booking agent: a LangGraph StateGraph built by hand (no prebuilt agent), so every node can be drawn and explained.

        START -> [redflag] --red flag / check failed--> END
                    |
                    v ok
                 [agent] <------------------+
                    | tool calls            |
                    v                       |
                 [tools] -------------------+      (book / cancel / reschedule pause at interrupt() for the patient's Confirm)
                    |
        no more tool calls -> END

Who decides what:
  the LLM      which tools to call, which free time to offer, how to explain a refusal
  your API     whether a booking / cancel / reschedule is allowed (the iteration 1 rules)
  the patient  whether to go ahead (Confirm / No card)
  your CODE    red flags, which patient is acting, the sources, and "I don't know"

Memory: a LangGraph InMemorySaver keyed by thread_id. The browser sends only the new message and the thread_id.
The agent sees at most the last 20 messages. Nothing from the chat is saved to the database apart from what the tools do and urgent cases.
"""
import json
import logging
import re
from datetime import timedelta
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command

from . import config, db, llm, rag, service, tools as toolbox
from .rules import DAYS

log = logging.getLogger("netracare.agent")

MAX_MESSAGES = 20          # messages sent to the model
STEP_LIMIT = 16            # graph steps per message; each agent<->tools loop is 2 steps
MAX_MESSAGE_CHARS = 500
CHECK_FAILED = "We couldn't check your message just now. Please try again, or call the clinic if it's urgent."
STEP_LIMIT_MESSAGE = "Sorry, I can't finish that just now. Please try again, or ask Staff at the front desk."


class ChatState(MessagesState):
    halt: bool                 # the red-flag node ended this turn
    emergency: Optional[dict]
    kind: Optional[str]        # "emergency" | "check_failed" | None


# ---------------------------------------------------------------------------------------------------
# The system prompt: rebuilt from the database for EVERY message (so "next Saturday" is never a guess)
# ---------------------------------------------------------------------------------------------------
def build_system_prompt(patient_id: str) -> str:
    conn = db.connect()
    try:
        now = config.clinic_now()
        hist = service.patient_history(conn, patient_id)
        p = hist["patient"]
        holidays = {r["date"]: r["name"] for r in conn.execute("SELECT date, name FROM holidays")}
        calendar = []
        for i in range(0, config.BOOKING_WINDOW_DAYS + 1):
            d = now.date() + timedelta(days=i)
            label = "closed (Sunday)" if d.weekday() == 6 else (f"holiday: {holidays[d.isoformat()]}" if d.isoformat() in holidays else "open")
            calendar.append(f"{DAYS[d.weekday()]} {d.isoformat()}: {label}")
        upcoming = [f"- {r['appointment_id']}: {r['speciality']} with {r['doctor_name']}, {r['date']} {r['start_time']} ({r['visit_type']})"
                    for r in conn.execute(
                        "SELECT a.*, d.name AS doctor_name FROM appointments a JOIN doctors d USING(doctor_id) "
                        "WHERE a.patient_id=? AND a.status='booked' AND a.date >= ? ORDER BY a.date, a.start_time",
                        (patient_id, now.date().isoformat()))]
        # past visits: date, doctor, speciality, visit type ONLY. No doctor notes and no health profile: the agent doesn't need them.
        past = [f"- {v['date']}: {v['speciality']} with {v['doctor_name']} ({v['visit_type']})" for v in hist["visits"]]
    finally:
        conn.close()
    return f"""You are the NetraCare patient assistant. NetraCare is an eye clinic. You help the logged-in patient find free times and book, cancel
or reschedule their OWN appointments, and answer questions about the clinic. Be brief, warm and clear.

TODAY: {DAYS[now.weekday()]} {now.date().isoformat()}, {now.strftime('%H:%M')}. Bookings open up to 30 days ahead.
CALENDAR (next 30 days):
{chr(10).join(calendar)}
Clinic hours: Mon-Sat 9:00-13:00 and 14:00-18:00. A new consultation takes 20 minutes, a follow-up 10.

THE PATIENT: {p['name']}, age {p['age'] if p['age'] is not None else 'unknown'}.
Upcoming appointments:
{chr(10).join(upcoming) or '(none)'}
Past visits:
{chr(10).join(past) or '(none)'}

HOW TO WORK
- Use tools. Find free times with find_slots BEFORE offering any, and NEVER offer a time you have not seen in a find_slots result.
  Call find_slots at most twice for one patient message. If it shows the doctor is not at the clinic on the day the patient asked for,
  do not search day after day: use ONE find_slots call over the next 7 days to see their nearest free days, then tell the patient why
  that day is not possible and offer those days.
- Ask for the reason for the visit before booking. Do not invent a reason.
- When the patient describes a symptom or problem, DO NOT ask questions about possible causes and NEVER name or hint at an eye condition
  (do not ask "do you have cataract / dry eye / glaucoma ..."). Do not ask them to describe it further. Immediately call search_clinic_docs
  (the patient's own sentence first, unchanged, then "Which speciality sees this?"), then reply with the speciality and its
  doctors' names, say "the doctor will assess you at your visit", and offer to find free times. Example: "A Glaucoma specialist can assess
  this. At NetraCare that is Dr. Meera Iyer. The doctor will assess you at your visit. Would you like me to find a time?"
- Write the question you give search_clinic_docs in the clinic's own plain words, close to how a document would say it, and keep the
  patient's own key words. For a problem: "I want to stop wearing glasses. Which speciality sees this?" (not
  "get rid of my glasses for good"). When the patient already asked a complete question about the clinic, search with that same question.
- A parent or carer describing a child's (or someone else's) eye problem is a NetraCare topic: do the same routing, and put the child's
  age or "a child" in the question (for example "My 5 year old's left eye turns inwards. Which speciality sees this?").
  Appointments here are made under the logged-in patient's own account: only if they ask to book for the other person, say the front
  desk can help with that.
- When the patient describes a NEW problem, do not bring up their other appointments or past visits unless they ask about them, and do not
  repeat the problem as a name or label ("double vision can be assessed ..."): say "this". Never write a speciality's name for a problem
  that is not in a search result.
- Add nothing the patient did not say to that question (no age, no "adults"): use their own words only. When they only describe a problem,
  do NOT call my_appointments or list their appointments: call search_clinic_docs and answer. Mention their appointments only if they ask.
- "Not sure what's wrong", "just want my eyes looked at" or "a check-up" are problems too: search with the patient's sentence and
  "Which speciality sees this?" and answer with the speciality and doctors. Never answer with a list of their appointments unless they asked.
- If the problem is clearly NOT about the eyes (a skin rash, a sore arm, a cough), do not search and suggest nothing: say that NetraCare is
  an eye clinic and only sees eye problems, and offer to help with anything about their eyes or the clinic.
- Pick the speciality first. A patient under 16 always books Paediatric (Dr. Vikram Menon, D5), whatever the problem.
- Never book a different speciality than the one the patient asked for. If that speciality's doctor is not at the clinic on the day they
  asked (for example a Saturday), say so and offer that same speciality's nearest free days instead. Do not switch to another speciality or doctor.
- Always end with a reply for the patient or a tool call: never answer with nothing.
- If the day the patient asked for cannot be used (a holiday, a Sunday, the doctor is away) and you want to offer a DIFFERENT day or time, offer it
  in words first ("9 November is a holiday. Would Tuesday 10 November at 9:00 AM with Dr. X work?") and call the booking tool only after they agree.
- Never say an appointment is booked, cancelled or moved unless the tool result said so. After the patient answers No, or types a message instead
  of pressing Confirm, nothing has changed: say so, and ask what they would like instead.
- When the patient has ASKED you to book and their request already says what they want ("the earliest time", "or else the earliest weekday
  that week"), choose the time yourself from find_slots and call the booking tool at once. Do not ask "would you like me to book it?": the
  confirmation card does that. If they only DESCRIBE a problem and have not asked to book, never call a booking tool: name the speciality
  and its doctors and ask whether they would like you to find a time.
  Keep ALL the patient's own words in the question you give search_clinic_docs (for example "sandy and tired eyes after a whole day on the laptop").
- A question about whether or when the patient CAN cancel, change or book ("Can I cancel one hour before?", "How late can I change it?") is a
  question about clinic rules: call search_clinic_docs and answer from it. Call cancel_appointment only when they ask you to cancel.
- Understand English and Hinglish (Hindi written in English letters, for example "aankh mein jalan rehta hai"), but always reply in English,
  naming the speciality and its doctors in English, in the same form as for any other problem.
  If the patient describes a problem instead of naming a speciality, call search_clinic_docs to find which speciality sees it,
  and use that speciality. Never say what the problem might be.
- Follow-up or new: book a follow-up with the doctor who saw the patient LAST in that speciality (see past visits). If the patient has no
  past visit in that speciality, it is a NEW consultation (20 min): say so plainly.
- "That week" means the Monday to Saturday week that contains the date the patient named (not the week after). When a patient asks for a
  weekend day "or else the earliest weekday that week", look at that same week first, and skip holidays and days the doctor is away.
- When the patient wants to CANCEL an appointment and book another one IN ITS PLACE ("cancel it and book me with Dr. X on <day> instead"),
  that is ONE reschedule: use find_slots, then reschedule_appointment (one confirmation). NEVER call cancel_appointment in that case, and
  never call book_appointment for it: use only reschedule_appointment.
- Never offer a day earlier than the day the patient asked for. When they name a date, look from that date forward (if it is a holiday or a
  closed day, the next open day after it, not an earlier one).
- If the patient has exactly ONE upcoming appointment and asks to change its time or day ("make it the afternoon", "a different day",
  "earlier"), that means reschedule THAT appointment: look up free times with find_slots (same doctor, speciality and visit type, on the day
  they mean) and call reschedule_appointment. Do NOT ask whether they want a new booking instead. Only if they have several upcoming
  appointments and it is unclear which one they mean, ask which one.
- A patient may have only one upcoming booking per speciality. If they already have one, say so (date and doctor) and offer to change it.
- Call ONE action tool at a time (book_appointment, cancel_appointment or reschedule_appointment). The patient will see a confirmation card;
  nothing changes until they confirm. DO NOT ask the patient to confirm in your own words ("please confirm", "shall I go ahead?"):
  as soon as you have everything the tool needs (doctor, speciality, visit type, date, time and the reason for a booking), CALL the tool.
  The card IS the confirmation. If a tool returns "declined", the patient pressed No: say that nothing was changed ("Okay, I haven't changed anything.") and ask what they would like instead. Never say a time was unavailable because of a "declined". Appointments are identified by ID:
  use the IDs above or from my_appointments.
- The clinic API decides what is allowed. If a tool returns "refused", explain the message in plain words and offer free times from find_slots,
  or say clearly when nothing is free in the next 30 days. Do not try to work around a refusal.
- If a tool returns "to_staff", say the request has gone to Staff and the appointment stays booked until Staff approve it.
- ALWAYS call search_clinic_docs before answering ANY question about the clinic: opening days and hours, holidays, fees, cancelling or
  changing rules, what to bring, dilation, what to do before or after a test or procedure, which doctors do what, and which days a doctor
  sees patients. Never answer these from memory or from the calendar above, even if you think you know: the app can only show a source when
  you searched. (Use list_doctors only to get doctor IDs for booking.) Write a complete standalone question.
  If it returns "idk", pass its message on WORD FOR WORD. Do not write a "Sources" line: the app adds sources itself. Costs are indicative only.
- NEVER name a condition the patient might have, never say what a symptom means, never suggest a treatment, medicine, eye drops or product.
  A doctor will assess the patient at the visit.
- Only discuss NetraCare. Politely decline anything else. You only ever see and change THIS patient's appointments.
- If something sounds urgent, do not book: the app handles emergencies before you see the message."""


# ---------------------------------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------------------------------
def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)


def redflag_node(state: ChatState, config: RunnableConfig) -> dict:  # LangGraph injects the run config by this exact name
    """Your code, not the LLM: check_red_flags on the patient's typed message. A red flag ends the turn with no tool calls."""
    last = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
    pid = config["configurable"]["patient_id"]
    try:
        flag = llm.check_red_flags(_text(last.content))
    except llm.RedFlagCheckError:
        return {"messages": [AIMessage(content=CHECK_FAILED)], "halt": True, "emergency": None, "kind": "check_failed"}
    if flag.red_flag:
        conn = db.connect()
        try:
            urgent_id = service.save_urgent_case(conn, pid, _text(last.content), flag.category, flag.why, source="chat")
        finally:
            conn.close()
        return {"messages": [AIMessage(content=llm.EMERGENCY_RESPONSE)], "halt": True, "kind": "emergency",
                "emergency": {"message": llm.EMERGENCY_RESPONSE, "category": flag.category, "urgent_id": urgent_id}}
    return {"halt": False, "emergency": None, "kind": None}


def _window(messages: list) -> list:
    """The last 20 messages, never starting in the middle of a tool exchange (a tool result needs its tool call)."""
    win = messages[-MAX_MESSAGES:]
    while win and not isinstance(win[0], HumanMessage):
        win = win[1:]
    return win


def agent_node(state: ChatState, config: RunnableConfig) -> dict:
    pid = config["configurable"]["patient_id"]
    prompt = [SystemMessage(content=build_system_prompt(pid))] + _window(state["messages"])
    ask = lambda: llm.get_model(0).bind_tools(toolbox.TOOLS).invoke(prompt)  # a new model each try, so a retry takes the next key
    reply = llm.invoke_with_backoff(ask)
    if not _text(reply.content).strip() and not (getattr(reply, "tool_calls", None) or []):
        reply = llm.invoke_with_backoff(ask)  # the model sometimes returns nothing at all: ask once more before giving up
    # One action at a time: if the model asked for several changes, keep only the first (read-only calls may all stay).
    calls = getattr(reply, "tool_calls", None) or []
    seen_action, kept = False, []
    for c in calls:
        if c["name"] in toolbox.ACTION_TOOLS:
            if seen_action:
                continue
            seen_action = True
        kept.append(c)
    if len(kept) != len(calls):
        reply = AIMessage(content=reply.content, tool_calls=kept, id=reply.id)
    return {"messages": [reply]}


def _after_redflag(state: ChatState) -> str:
    return "end" if state.get("halt") else "agent"


def build_graph(checkpointer):
    g = StateGraph(ChatState)
    g.add_node("redflag", redflag_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", ToolNode(toolbox.TOOLS))
    g.add_edge(START, "redflag")
    g.add_conditional_edges("redflag", _after_redflag, {"end": END, "agent": "agent"})
    g.add_conditional_edges("agent", tools_condition)   # tool calls -> "tools", otherwise END
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
    """Forget every conversation (used by tests; a server restart does the same)."""
    global _graph, _saver
    _graph = _saver = None


def draw_graph_mermaid() -> str:
    return get_graph().get_graph().draw_mermaid()


# ---------------------------------------------------------------------------------------------------
# One chat turn
# ---------------------------------------------------------------------------------------------------
def _pending(graph, cfg):
    snap = graph.get_state(cfg)
    for t in snap.tasks:
        if t.interrupts:
            v = t.interrupts[0].value
            return v if isinstance(v, dict) else {"summary": str(v), "action": "action"}
    return None


def _drop_thread(cfg) -> None:
    try:
        _saver.delete_thread(cfg["configurable"]["thread_id"])
    except Exception:
        pass


def clean_thread_id(thread_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", thread_id or ""):
        raise ValueError("bad thread id")
    return thread_id


def run_turn(patient_id: str, thread_id: str, message: Optional[str] = None, confirm: Optional[bool] = None) -> dict:
    """Run one chat turn. Returns {kind, reply, sources, emergency, pending_action, tool_calls}.

    The thread is namespaced by the patient's ID taken from the login, so one patient can never reach another's conversation."""
    graph = get_graph()
    cfg = {"configurable": {"thread_id": f"{patient_id}:{thread_id}", "patient_id": patient_id}, "recursion_limit": STEP_LIMIT}
    try:
        waiting = _pending(graph, cfg)
        if confirm is not None and not waiting:
            return _result("error", "There is nothing waiting for your confirmation.")
        if waiting and confirm is None:
            graph.invoke(Command(resume=False), cfg)  # a typed message while an action waits counts as No: nothing changes
        if confirm is not None:
            graph.invoke(Command(resume=bool(confirm)), cfg)
        else:
            graph.invoke({"messages": [HumanMessage(content=message)]}, cfg)
        return _compose(graph, cfg)
    except GraphRecursionError:
        log.warning("agent hit the step limit")
        _drop_thread(cfg)
        return _result("error", STEP_LIMIT_MESSAGE)
    except Exception:
        log.exception("agent turn failed")  # the patient sees a friendly message; the traceback goes to the server log
        _drop_thread(cfg)  # a half-finished tool exchange must not poison the next turn
        return _result("error", rag.CHAT_ERROR)


def _result(kind, reply, sources=None, emergency=None, pending=None, tools=None) -> dict:
    return {"kind": kind, "reply": reply, "sources": sources or [], "emergency": emergency, "pending_action": pending,
            "tool_calls": tools or []}


def _compose(graph, cfg) -> dict:
    snap = graph.get_state(cfg)
    msgs = snap.values["messages"]
    start = max(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage))  # this user turn (including pre-confirm tool calls)
    turn = msgs[start:]
    kind = snap.values.get("kind")
    tool_names = [c["name"] for m in turn if isinstance(m, AIMessage) for c in (m.tool_calls or [])]
    pending = _pending(graph, cfg)
    if pending:  # waiting for Confirm / No
        text = next((_text(m.content) for m in reversed(turn) if isinstance(m, AIMessage) and m.content), "")
        return _result("pending", text or "Please confirm this action.", pending=pending, tools=tool_names)
    if kind in ("emergency", "check_failed"):
        return _result(kind, _text(turn[-1].content), emergency=snap.values.get("emergency"), tools=tool_names)

    reply = next((_text(m.content) for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), "")
    searches = []
    emergency = None
    for m in turn:
        if isinstance(m, ToolMessage):
            try:
                data = json.loads(_text(m.content))
            except ValueError:
                continue
            if m.name == "search_clinic_docs":
                searches.append(data)
            if isinstance(data, dict) and data.get("emergency"):
                emergency = data["emergency"]  # a red-flag reason given to the booking API itself
    sources = list(dict.fromkeys(s for d in searches if d.get("result") == "answered" for s in d.get("sources", [])))
    kind = "answer"
    if searches and not sources and all(d.get("result") == "idk" for d in searches):
        reply, kind = rag.IDK_MESSAGE, "idk"  # "I don't know" is decided and worded by code, word for word
    if emergency:
        kind = "emergency"
        reply = f"{llm.EMERGENCY_RESPONSE}\n\n{reply}".strip()
    return _result(kind, reply, sources=sources, emergency=emergency, tools=tool_names)
