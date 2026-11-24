"""Compare the SINGLE agent with the SUPERVISOR team on the same six messages, with the real model. Run:  python compare_agents.py

For each message it prints whether the answer was right, how many AI calls it took and how long it took. A temporary database is used, so
nothing in your real database changes. About 65 AI calls in total (it is a small, deliberate comparison, not a stress test).

What "right" means here (checked by code, not by the AI):
  information questions: the answer comes from the clinic documents (the expected words appear, or the fixed "I don't know"), and no booking tool was called
  booking questions:     the expected tool was called, and an action only produced a Confirm card (nothing was changed)
"""
import os
import sys
import tempfile
import time
import uuid

os.environ["NETRACARE_DB"] = os.path.join(tempfile.mkdtemp(), "compare.db")
os.environ["CLINIC_NOW"] = "2026-11-02T10:00"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from app import agent, db, llm  # noqa: E402

ACTIONS = {"book_appointment", "cancel_appointment", "reschedule_appointment", "choose_rebooking"}
IDK = "I don't know. Our clinic documents don't cover that. Please ask Staff at the front desk."

CASES = [
    ("info", "How much is a new consultation?",
     lambda r: r["kind"] == "answer" and "800" in r["reply"] and not set(r["tool_calls"]) & ACTIONS),
    ("info", "My eyes feel sandy and tired after a whole day on the laptop",
     lambda r: "Dry eye" in r["reply"] and "Fatima Khan" in r["reply"] and not set(r["tool_calls"]) & ACTIONS),
    ("info", "Is there parking at the clinic?",
     lambda r: r["kind"] == "idk" and r["reply"] == IDK),
    ("booking", "What appointments do I have?",
     lambda r: "my_appointments" in r["tool_calls"] and r["kind"] == "answer"),
    ("booking", "Find me a free retina time with Dr. Shah on Friday morning",
     lambda r: "find_slots" in r["tool_calls"] and r["kind"] == "answer" and not set(r["tool_calls"]) & ACTIONS),
    ("booking", "Book my retina follow-up with Dr. Shah this Friday morning, the earliest time. Reason: diabetic eye review",
     lambda r: r["kind"] == "pending" and r["pending_action"]["action"] == "book"),
]


def run(mode: str):
    agent.reset()
    rows = []
    for kind, message, ok in CASES:
        before, t0 = llm.call_count(), time.time()
        r = agent.run_turn("P01", uuid.uuid4().hex, message=message, mode=mode)
        rows.append({"kind": kind, "message": message, "ok": bool(ok(r)), "calls": llm.call_count() - before, "seconds": round(time.time() - t0, 1),
                     "tools": r["tool_calls"]})
    return rows


def main():
    db.init_db(with_seed_appointments=True, with_past_visits=True)
    results = {m: run(m) for m in ("single", "supervisor")}
    print(f"{'message':60s} {'mode':11s} {'right':6s} {'AI calls':9s} {'seconds':8s} tools")
    for i, (_, message, _) in enumerate(CASES):
        for m in ("single", "supervisor"):
            r = results[m][i]
            print(f"{message[:58]:60s} {m:11s} {'yes' if r['ok'] else 'NO':6s} {r['calls']:<9d} {r['seconds']:<8} {', '.join(r['tools'])}")
    print()
    for m in ("single", "supervisor"):
        rs = results[m]
        print(f"{m:11s} right {sum(r['ok'] for r in rs)} of {len(rs)} | AI calls {sum(r['calls'] for r in rs)} | seconds {round(sum(r['seconds'] for r in rs), 1)}")


if __name__ == "__main__":
    main()
