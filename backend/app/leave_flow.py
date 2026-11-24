"""Doctor leave as a LangGraph workflow (stretch goal).

Marking leave is not one step: the doctor's appointments are cancelled and given new times, and then every moved patient has to be settled
(Staff approve or reject, or the patient chooses a time in the chat). That can take hours or days, and the server may restart in between.
So the whole thing is a small LangGraph graph that PAUSES with `interrupt()` while it waits, and keeps its place in a SQLite file:

    START -> [mark] -> [wait] --(someone decided one rebooking: loop while any are left)--> END
                          |
                          pause here with interrupt(): the graph is saved to SQLite and can be resumed days later, even after a restart

  mark   plain code (ops.mark_leave): record the leave, cancel the affected appointments, propose a new time for each
  wait   pauses; each decision that arrives (approve, reject or the patient's own choice) resumes it once; it ends when none are left

The DECISION itself (the database change, with every clinic rule) is made by ordinary code in ops.py BEFORE the graph is told, so a refused
decision never touches the workflow. The graph keeps the progress: what is still waiting and what has been decided.
The workflow lives in its own SQLite file next to the clinic database (`<database>.workflows`).
"""
import sqlite3
import threading
import uuid
from typing import Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import config, db, ops


class FlowState(TypedDict, total=False):
    doctor_id: str
    date: str
    session: str
    result: dict          # what ops.mark_leave returned (the leave and its proposals)
    pending: list         # rebooking ids still waiting for a decision
    decided: list         # [{"rebooking_id", "outcome"}] in the order they were decided


def mark_node(state: FlowState) -> dict:
    conn = db.connect()
    try:
        res = ops.mark_leave(conn, state["doctor_id"], state["date"], state["session"])
    finally:
        conn.close()
    return {"result": res, "pending": [r["rebooking_id"] for r in res["rebookings"] if r["status"] == "pending"], "decided": []}


def wait_node(state: FlowState) -> dict:
    pending = list(state.get("pending") or [])
    if not pending:
        return {}
    decision = interrupt({"waiting_for": pending})        # the graph stops here; it is saved, and resumed by `notify`
    rid = decision["rebooking_id"]
    return {"pending": [p for p in pending if p != rid],
            "decided": list(state.get("decided") or []) + [{"rebooking_id": rid, "outcome": decision["outcome"]}]}


def _more_to_do(state: FlowState) -> str:
    return "wait" if state.get("pending") else "done"


def build_graph(checkpointer):
    g = StateGraph(FlowState)
    g.add_node("mark", mark_node)
    g.add_node("wait", wait_node)
    g.add_edge(START, "mark")
    g.add_edge("mark", "wait")
    g.add_conditional_edges("wait", _more_to_do, {"wait": "wait", "done": END})
    return g.compile(checkpointer=checkpointer)


class _Flow:
    """One workflow store: a SQLite file, its checkpointer, the graph, and the leave -> thread table."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("CREATE TABLE IF NOT EXISTS leave_threads (leave_id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL)")
        self.graph = build_graph(SqliteSaver(self.conn))
        self.lock = threading.RLock()

    def thread_for(self, leave_id: int) -> Optional[str]:
        row = self.conn.execute("SELECT thread_id FROM leave_threads WHERE leave_id=?", (leave_id,)).fetchone()
        return row[0] if row else None

    def remember(self, leave_id: int, thread_id: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO leave_threads (leave_id, thread_id) VALUES (?,?)", (leave_id, thread_id))

    def close(self) -> None:
        self.conn.close()


_flows: dict = {}
_flows_lock = threading.Lock()


def _path() -> str:
    return config.db_path() + ".workflows"


def _flow() -> _Flow:
    path = _path()
    with _flows_lock:
        if path not in _flows:
            _flows[path] = _Flow(path)
        return _flows[path]


def reset() -> None:
    """Forget the open workflow files (tests do this to simulate a server restart: the next call opens the SQLite file again)."""
    with _flows_lock:
        for f in _flows.values():
            f.close()
        _flows.clear()


def start(doctor_id: str, date: str, session: str) -> dict:
    """Mark leave through the workflow. Returns exactly what `ops.mark_leave` returns. The graph is now paused, waiting for decisions."""
    flow = _flow()
    thread = uuid.uuid4().hex
    cfg = {"configurable": {"thread_id": thread}}
    with flow.lock:
        flow.graph.invoke({"doctor_id": doctor_id, "date": date, "session": session}, cfg)   # a refused leave raises here, nothing is saved
        result = flow.graph.get_state(cfg).values["result"]
        flow.remember(result["leave_id"], thread)
    return result


def notify(rebooking_id: int, outcome: str) -> None:
    """Tell the workflow that one rebooking has been decided (`outcome`: approved, rejected or chosen). Resumes the paused graph once.
    If this rebooking has no workflow (for example it was made before workflows existed), nothing happens."""
    conn = db.connect()
    try:
        row = conn.execute("SELECT leave_id FROM rebookings WHERE rebooking_id=?", (rebooking_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return
    flow = _flow()
    with flow.lock:
        thread = flow.thread_for(row["leave_id"])
        if thread is None:
            return
        cfg = {"configurable": {"thread_id": thread}}
        state = flow.graph.get_state(cfg)
        if not state.next or rebooking_id not in (state.values.get("pending") or []):
            return
        flow.graph.invoke(Command(resume={"rebooking_id": rebooking_id, "outcome": outcome}), cfg)


def progress(leave_id: int) -> Optional[dict]:
    """Where a leave's workflow is: still waiting (and for whom), or finished. None if this leave has no workflow."""
    flow = _flow()
    with flow.lock:
        thread = flow.thread_for(leave_id)
        if thread is None:
            return None
        state = flow.graph.get_state({"configurable": {"thread_id": thread}})
        values = state.values or {}
        return {"leave_id": leave_id, "finished": not state.next, "waiting_for": list(values.get("pending") or []),
                "decided": list(values.get("decided") or [])}


# --------------------------------------------------------------------------------------------------------------
# The three ways a rebooking gets settled. Each makes the database change first (ordinary code, every rule checked),
# and only if that worked does it tell the workflow, so a refused decision never touches the paused graph.
# --------------------------------------------------------------------------------------------------------------
def decide(conn, rebooking_id: int, staff_name: str, approve: bool) -> dict:
    row = ops.decide_rebooking(conn, rebooking_id, staff_name, approve)
    notify(rebooking_id, "approved" if approve else "rejected")
    return row


def choose(conn, patient_id: str, rebooking_id: int, doctor_id: str, date: str, start_time: str) -> dict:
    row = ops.choose_rebooking(conn, patient_id, rebooking_id, doctor_id, date, start_time)
    notify(rebooking_id, "chosen")
    return row
