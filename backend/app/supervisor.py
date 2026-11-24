"""The supervisor chat (stretch goal): the single agent split into a TEAM.

    START -> [redflag] -> [supervisor] --"info"-----> [info_agent]    <--> [info_tools]      (only search_clinic_docs and list_doctors: it cannot change anything)
                |             \\
                v (red flag)    --"booking"--> [booking_agent] <--> [booking_tools]  (the appointment tools, with Confirm / No, plus the two read-only helpers)
               END

  * The red-flag node is the same code as the single agent's: every message is checked first, before anyone else sees it.
  * The supervisor is one small AI call that reads the last few messages and picks who answers THIS message. If it cannot decide (an AI error), the
    booking agent answers: it has the read-only helpers too, so nothing is lost.
  * A confirmation card that is waiting belongs to the booking agent: the answer to the card goes straight back to it, with no routing call.
  * The patient's ID still comes from the login, every change still waits for Confirm, and the clinic API still decides.

It is used when a chat request says `"mode": "supervisor"`. The single agent stays the default. `compare_agents.py` runs both on the same messages.
"""
import asyncio
from typing import Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from . import agent, config, db, llm, service, tools as toolbox
from .rules import DAYS

INFO_TOOLS = [toolbox.search_clinic_docs, toolbox.list_doctors]
BOOKING_TOOLS = [toolbox.search_clinic_docs, toolbox.list_doctors, toolbox.find_slots, toolbox.my_appointments, toolbox.book_appointment,
                 toolbox.cancel_appointment, toolbox.reschedule_appointment, toolbox.my_rebooking_options, toolbox.choose_rebooking]


class SupState(agent.ChatState):
    route: Optional[str]       # "info" or "booking": who answers this message


# ------------------------------------------------------------------------------------------------- the supervisor (a router)
class RouteResult(BaseModel):
    route: Literal["info", "booking"] = Field(description='"booking" if the patient wants to do something with appointments, otherwise "info"')


_route_parser = PydanticOutputParser(pydantic_object=RouteResult)
_ROUTE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You route a patient's message at an eye clinic to one of two assistants. Pick who should answer the LAST message.
- "booking": anything about the patient's appointments or the calendar: book, cancel, reschedule or change a time, free times or "when can I come",
  "my appointments", a moved appointment or a doctor on leave, choosing a new time, or a problem the patient wants to come in for.
- "info": questions about the clinic (timings, fees, what to bring, procedures, which doctors it has, policies) and which kind of doctor sees a problem,
  with no request to book or change anything.
If the assistant has just asked the patient for something to finish a booking (a reason, a day, a time, a choice), a short reply belongs to "booking".

{format_instructions}"""),
    ("human", "Recent messages:\n{history}\n\nLast message: {message}"),
]).partial(format_instructions=_route_parser.get_format_instructions())


def _history_text(messages: list) -> tuple[str, str]:
    """The last few spoken messages as plain text (no tool calls) and the last patient message."""
    spoken = []
    for m in messages[-7:]:
        if isinstance(m, HumanMessage):
            spoken.append("patient: " + agent._text(m.content))
        elif isinstance(m, AIMessage) and agent._text(m.content).strip() and not m.tool_calls:
            spoken.append("assistant: " + agent._text(m.content)[:300])
    last = next((agent._text(m.content) for m in reversed(messages) if isinstance(m, HumanMessage)), "")
    return "\n".join(spoken[:-1]) or "(none)", last


def supervisor_node(state: SupState, config: RunnableConfig) -> dict:
    history, last = _history_text(state["messages"])
    try:
        res = llm.run_chain(lambda m: _ROUTE_PROMPT | m | _route_parser, {"history": history, "message": last})
        return {"route": res.route}
    except Exception:
        return {"route": "booking"}          # the booking agent also has the read-only helpers, so a failed route loses nothing


async def asupervisor_node(state: SupState, config: RunnableConfig) -> dict:
    history, last = _history_text(state["messages"])
    try:
        res = await llm.ainvoke_with_backoff(lambda: (_ROUTE_PROMPT | llm.get_model(0) | _route_parser).ainvoke({"history": history, "message": last}))
        return {"route": res.route}
    except Exception:
        return {"route": "booking"}


# ------------------------------------------------------------------------------------------------- the two agents
_INFO_ROLE = """ROLE: you are the INFORMATION assistant of a two-assistant team. You answer questions about the clinic and say which kind of doctor sees a
problem. You can only look things up (search_clinic_docs, list_doctors): you cannot book, cancel or change anything. If the patient wants to book, cancel
or change an appointment, tell them in one friendly sentence to say what they would like to book and the booking assistant will help.

Rules:
- ALWAYS call search_clinic_docs before answering ANY question about the clinic (opening days and hours, holidays, fees, cancelling or changing rules, what to
  bring, dilation, procedures, which doctors do what, doctors' OPD days). Never answer these from memory. Write a complete standalone question.
  If it returns "idk", pass its message on WORD FOR WORD. Do not write a "Sources" line: the app adds sources itself.
- When the patient describes a problem, call search_clinic_docs with the patient's own sentence first, unchanged, then "Which speciality sees this?",
  and reply with the speciality and its doctors, saying "the doctor will assess you at your visit". Add nothing the patient did not say to that question.
- NEVER name a condition the patient might have, never say what a symptom means, never suggest a treatment, medicine, eye drops or product.
- Understand English and Hinglish, but always reply in English. Only discuss NetraCare. If the problem is clearly NOT about the eyes, say that NetraCare is
  an eye clinic and only sees eye problems.
- Be brief, warm and clear."""

_BOOKING_ROLE = """ROLE: you are the BOOKING assistant of a two-assistant team. You handle everything about the patient's own appointments. (A colleague answers
general questions about the clinic; you also have search_clinic_docs for quick lookups such as which speciality to book or a booking rule.)
"""


def _info_prompt(pid: str) -> str:
    conn = db.connect()
    try:
        name = service.patient_history(conn, pid)["patient"]["name"]
    finally:
        conn.close()
    now = config.clinic_now()
    return f"{_INFO_ROLE}\n\nTODAY: {DAYS[now.weekday()]} {now.date().isoformat()}, {now.strftime('%H:%M')}. The patient is {name}."


def _booking_prompt(pid: str) -> str:
    return _BOOKING_ROLE + "\n" + agent.build_system_prompt(pid)


def _make_agent_nodes(tools: list, prompt_fn, one_action: bool):
    def node(state: SupState, config: RunnableConfig) -> dict:
        pid = config["configurable"]["patient_id"]
        prompt = [SystemMessage(content=prompt_fn(pid))] + agent._window(state["messages"])
        ask = lambda: llm.get_model(0).bind_tools(tools).invoke(prompt)
        reply = llm.invoke_with_backoff(ask)
        if not agent._text(reply.content).strip() and not (getattr(reply, "tool_calls", None) or []):
            reply = llm.invoke_with_backoff(ask)
        return {"messages": [agent._one_action_at_a_time(reply) if one_action else reply]}

    async def anode(state: SupState, config: RunnableConfig) -> dict:
        pid = config["configurable"]["patient_id"]
        prompt = [SystemMessage(content=await asyncio.to_thread(prompt_fn, pid))] + agent._window(state["messages"])
        ask = lambda: llm.get_model(0).bind_tools(tools).ainvoke(prompt)
        reply = await llm.ainvoke_with_backoff(ask)
        if not agent._text(reply.content).strip() and not (getattr(reply, "tool_calls", None) or []):
            reply = await llm.ainvoke_with_backoff(ask)
        return {"messages": [agent._one_action_at_a_time(reply) if one_action else reply]}

    return RunnableLambda(node, afunc=anode)


def _pick(state: SupState) -> str:
    return "info_agent" if state.get("route") == "info" else "booking_agent"


def _after(tools_node: str):
    def route(state: SupState) -> str:
        last = state["messages"][-1]
        return tools_node if getattr(last, "tool_calls", None) else END
    return route


def build_graph(checkpointer):
    g = StateGraph(SupState)
    g.add_node("redflag", RunnableLambda(agent.redflag_node, afunc=agent.aredflag_node))
    g.add_node("supervisor", RunnableLambda(supervisor_node, afunc=asupervisor_node))
    g.add_node("info_agent", _make_agent_nodes(INFO_TOOLS, _info_prompt, one_action=False))
    g.add_node("info_tools", ToolNode(INFO_TOOLS))
    g.add_node("booking_agent", _make_agent_nodes(BOOKING_TOOLS, _booking_prompt, one_action=True))
    g.add_node("booking_tools", ToolNode(BOOKING_TOOLS))
    g.add_edge(START, "redflag")
    g.add_conditional_edges("redflag", agent._after_redflag, {"end": END, "agent": "supervisor"})
    g.add_conditional_edges("supervisor", _pick, {"info_agent": "info_agent", "booking_agent": "booking_agent"})
    g.add_conditional_edges("info_agent", _after("info_tools"), {"info_tools": "info_tools", END: END})
    g.add_edge("info_tools", "info_agent")
    g.add_conditional_edges("booking_agent", _after("booking_tools"), {"booking_tools": "booking_tools", END: END})
    g.add_edge("booking_tools", "booking_agent")
    return g.compile(checkpointer=checkpointer)
