"""STRETCH: plays all 24 rows of data4/agent_scenarios.csv through POST /chat with the REAL model and checks the DATABASE.

Needs your key (the REAL model; nothing is faked). Run:  python -m pytest tests/test_agent_scenarios_live.py -v -s

Each scenario starts from a fresh database (data pack 1 + stretch seed + data pack 2) on the fixed clock CLINIC_NOW = 2026-11-02 10:00.
`turns` are separated by ' || ': plain text is typed, [confirm] / [decline] press the card button (skipped if no card is waiting).
Checked: the database result (appointment booked / cancelled / unchanged / sent to Staff), the result kind, the expected tools
(in order, others allowed), and the safety rules. Wording ("must_say") is printed for you to read, not asserted.
A real model can be non-deterministic, so a single failure is a prompt to read the printed transcript, not necessarily a bug.
"""
import csv
import uuid

import pytest

from app import llm
from helpers import DATA4, login_patient, login_staff


with open(DATA4 / "agent_scenarios.csv", newline="", encoding="utf-8") as f:
    SCENARIOS = list(csv.DictReader(f))

@pytest.fixture(autouse=True)
def extra_pace():
    """Each case here makes several model calls (red-flag check, agent loop, answer): leave extra room under the provider's rate limit."""
    yield
    import time
    time.sleep(float(__import__("os").environ.get("LIVE_PACE_SECONDS", "8")))


SCORE = {"pass": 0}


def _all(client, status=None):
    rows = client.get("/appointments", headers=login_staff(client)).json()
    return [r for r in rows if status is None or r["status"] == status]


def _fields(a):
    return f"{a['doctor_id']} {a['speciality']} {a['visit_type']} {a['date']} {a['start_time']}"


@pytest.mark.parametrize("s", SCENARIOS, ids=[s["scenario_id"] for s in SCENARIOS])
def test_scenario(client4, rag_index, patients, s):
    staff = login_staff(client4)
    pid = s["patient_id"]
    h = login_patient(client4, patients[pid])
    before = {a["appointment_id"]: a["status"] for a in _all(client4)}
    thread, tools_used, last, transcript = uuid.uuid4().hex, [], None, []

    for turn in [t.strip() for t in s["turns"].split("||")]:
        if turn in ("[confirm]", "[decline]"):
            if not (last and last.get("pending_action")):
                continue  # "If no confirmation is waiting, skip that step"
            body = {"thread_id": thread, "confirm": turn == "[confirm]"}
        else:
            body = {"thread_id": thread, "message": turn}
        last = client4.post("/chat", headers=h, json=body).json()
        tools_used += last.get("tool_calls", [])
        transcript.append((turn, last["kind"], last["reply"][:300]))

    print(f"\n--- {s['scenario_id']} {s['patient_name']}: must say: {s['must_say']}")
    for t, k, r in transcript:
        print(f"   patient: {t[:100]}\n   [{k}] {r}")

    after = _all(client4)
    changed_booked = [a for a in after if a["status"] == "booked" and before.get(a["appointment_id"]) != "booked"]
    result, appt = s["expected_result"], s["expected_appointment"]

    # expected tools, in order (other tools may be called as well)
    want = [t.strip() for t in s["expected_tools"].split(";") if t.strip()]
    it = iter(tools_used)
    assert all(any(t == u for u in it) for t in want), f"{s['scenario_id']}: expected tools {want} in order, got {tools_used}"

    if result == "booked":
        want_fields = [x.strip() for x in appt.replace(" or ", "|").split("|")] if " or " in appt else [appt]
        if " or " in appt:  # "D1 or D3 General new date time"
            d, rest = appt.split(" or ", 1)
            other, tail = rest.split(" ", 1)
            want_fields = [f"{d} {tail}", f"{other} {tail}"]
        assert any(_fields(a) in want_fields and a["patient_id"] == pid for a in changed_booked), \
            f"{s['scenario_id']}: expected {want_fields}, new bookings: {[_fields(a) for a in changed_booked]}"
    elif result == "rescheduled":
        old_id, new = appt.split(" cancelled; ")
        assert next(a for a in after if a["appointment_id"] == old_id.strip())["status"] == "cancelled"
        assert any(_fields(a) == new.strip() and a["patient_id"] == pid for a in changed_booked), f"{s['scenario_id']}: {new}"
    elif result == "cancelled":
        assert next(a for a in after if a["appointment_id"] == appt.split()[0])["status"] == "cancelled"
    elif result == "to_staff":
        aid = appt.split()[0]
        assert next(a for a in after if a["appointment_id"] == aid)["status"] == "booked"
        assert len(client4.get("/staff/approvals", headers=staff).json()) == 1
    elif result == "no_change":
        assert {a["appointment_id"]: a["status"] for a in after} == before, f"{s['scenario_id']}: the database must not change"
    elif result == "emergency":
        assert last["kind"] == "emergency" and not changed_booked
        cases = client4.get("/urgent-cases", headers=staff).json()
        assert len(cases) == 1 and cases[0]["source"] == "chat"
    elif result == "idk":
        assert last["kind"] == "idk" and last["sources"] == []
    elif result == "answered":
        assert last["kind"] == "answer"
    # whatever happened, nothing may break a clinic rule: every booking in the DB passed the API (checked again here for the new ones)
    for a in changed_booked:
        assert a["date"] >= "2026-11-02" and a["start_time"][3:] in ("00", "10", "20", "30", "40", "50")
    SCORE["pass"] += 1


def test_report():
    print(f"\nScenarios passed: {SCORE['pass']}/24")
