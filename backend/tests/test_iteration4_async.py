"""Async and live updates, with the REAL model (nothing is faked).

What is checked:
  * the live-update hub (plain code): events reach every listener, from any thread, and a stuck listener never slows anyone down
  * GET /staff/events: only Staff may listen
  * Staff are told about an urgent case from the chat, and about a booking, by a PUSHED event, before the patient even gets the reply
  * the server keeps answering other people while a chat is waiting for the AI model (the AI calls are awaited, not blocking)
  * two chats at the same time overlap instead of queueing

The pushed-event tests start the real app in a real server (uvicorn, in a thread of this test), because a live stream needs a real connection.
"""
import asyncio
import json
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn

from app import events
from helpers import login_patient, login_staff
from test_iteration2 import book_body

CHEMICAL = "Floor cleaner went in my eye this morning"


# ------------------------------------------------------------------------------------------------- the hub (plain code)
def test_a_listener_receives_what_is_published():
    async def go():
        q = events.subscribe()
        try:
            events.publish("urgent_case", category="chemical_injury")
            return await asyncio.wait_for(q.get(), timeout=2)
        finally:
            events.unsubscribe(q)
    got = asyncio.run(go())
    assert got == {"type": "urgent_case", "category": "chemical_injury"}


def test_publishing_from_another_thread_reaches_the_listener():
    async def go():
        q = events.subscribe()
        try:
            threading.Thread(target=lambda: events.publish("approval", change="requested")).start()
            return await asyncio.wait_for(q.get(), timeout=2)
        finally:
            events.unsubscribe(q)
    assert asyncio.run(go())["type"] == "approval"


def test_nobody_listening_is_fine_and_a_stopped_listener_gets_nothing():
    events.publish("appointments", change="booked")      # no listener: nothing happens, no error

    async def go():
        q = events.subscribe()
        events.unsubscribe(q)
        events.publish("appointments", change="booked")
        await asyncio.sleep(0.05)
        return q.empty(), events.listener_count()
    empty, _ = asyncio.run(go())
    assert empty


def test_a_stuck_listener_never_blocks_the_publisher():
    async def go():
        q = events.subscribe()
        try:
            for _ in range(events.QUEUE_SIZE + 50):      # more events than the queue holds, and nobody reads
                events.publish("appointments", change="booked")
            await asyncio.sleep(0.1)
            return q.qsize()
        finally:
            events.unsubscribe(q)
    assert asyncio.run(go()) == events.QUEUE_SIZE


# ------------------------------------------------------------------------------------------------- a real server for the live tests
@pytest.fixture()
def live(client4):
    """The real app in a real uvicorn server (same temporary database as `client4`). Returns the base URL."""
    from app.main import app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "the test server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _login(base, role, **kw):
    r = httpx.post(base + "/login", json={"role": role, **kw}, timeout=30)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


class Listener:
    """A Staff dashboard's live stream: runs in a thread and keeps every event with the time it arrived."""

    def __init__(self, base, headers):
        self.events, self.error, self._stop = [], None, threading.Event()
        self._thread = threading.Thread(target=self._run, args=(base, headers), daemon=True)
        self._thread.start()

    def _run(self, base, headers):
        try:
            with httpx.stream("GET", base + "/staff/events", headers=headers, timeout=httpx.Timeout(60, read=60)) as r:
                for line in r.iter_lines():
                    if self._stop.is_set():
                        break
                    if line.startswith("data:"):
                        self.events.append((time.time(), json.loads(line[5:].strip())))
        except Exception as e:   # the test ends the stream by closing the server
            self.error = e

    def wait_for(self, kind, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            for t, ev in self.events:
                if ev["type"] == kind:
                    return t, ev
            time.sleep(0.05)
        raise AssertionError(f"no '{kind}' event within {timeout}s; got {[e for _, e in self.events]}")

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------------------------------------------- GET /staff/events
def test_only_staff_may_listen_to_the_live_stream(live, patients):
    patient = _login(live, "patient", mobile=patients["P20"]["mobile"], name=patients["P20"]["name"])
    assert httpx.get(live + "/staff/events", headers=patient, timeout=10).status_code == 403
    assert httpx.get(live + "/staff/events", timeout=10).status_code == 401
    doctor = _login(live, "doctor", doctor_id="D1")
    assert httpx.get(live + "/staff/events", headers=doctor, timeout=10).status_code == 403


def test_staff_are_told_about_an_urgent_case_before_the_patient_gets_the_reply(live, patients):
    staff = Listener(live, _login(live, "staff", name="Front Desk"))
    try:
        staff.wait_for("hello", timeout=10)
        patient = _login(live, "patient", mobile=patients["P20"]["mobile"], name=patients["P20"]["name"])
        r = httpx.post(live + "/chat", headers=patient, json={"thread_id": uuid.uuid4().hex, "message": CHEMICAL}, timeout=120)
        replied = time.time()
        assert r.status_code == 200 and r.json()["kind"] == "emergency"
        arrived, event = staff.wait_for("urgent_case", timeout=10)
        assert event["category"]
        assert arrived <= replied + 0.5, "the event should be pushed as soon as the case is saved, not after the reply"
        assert "Floor cleaner" not in json.dumps(event), "an event never carries what the patient wrote"
    finally:
        staff.stop()


def test_staff_are_told_when_an_appointment_is_booked(live, patients):
    staff = Listener(live, _login(live, "staff", name="Front Desk"))
    try:
        staff.wait_for("hello", timeout=10)
        patient = _login(live, "patient", mobile=patients["P20"]["mobile"], name=patients["P20"]["name"])
        r = httpx.post(live + "/appointments", headers=patient, json=book_body(), timeout=120)
        assert r.status_code == 201, r.text
        _, event = staff.wait_for("appointments", timeout=10)
        assert event["change"] == "booked" and "reason" not in event
    finally:
        staff.stop()


# ------------------------------------------------------------------------------------------------- async: nobody waits for somebody else's AI call
def _chat(base, headers, message):
    t = time.time()
    r = httpx.post(base + "/chat", headers=headers, json={"thread_id": uuid.uuid4().hex, "message": message}, timeout=180)
    assert r.status_code == 200, r.text
    return time.time() - t


def test_the_server_keeps_answering_while_a_chat_waits_for_the_ai(live, patients):
    patient = _login(live, "patient", mobile=patients["P01"]["mobile"], name=patients["P01"]["name"])
    done = {}
    t = threading.Thread(target=lambda: done.update(seconds=_chat(live, patient, "What time does the clinic open on Saturday?")))
    t.start()
    time.sleep(1.0)                                   # the chat is now waiting for the AI model
    answers = []
    for _ in range(5):
        started = time.time()
        assert httpx.get(live + "/doctors", timeout=10).status_code == 200
        answers.append(time.time() - started)
        time.sleep(0.2)
    still_waiting = t.is_alive()
    t.join()
    assert still_waiting, "the chat finished too quickly to prove anything; it must still be waiting while the other calls are answered"
    assert max(answers) < 1.0, f"other requests were held up while the chat waited: {answers}"


def test_two_chats_at_the_same_time_are_worked_on_at_the_same_time(live, patients):
    """Proved by counting, not by a stopwatch: while the server works on both chats, the number of chats in progress reaches 2. If one chat
    blocked the server, the second could only start after the first had finished and the maximum would stay at 1."""
    from app import agent
    p1 = _login(live, "patient", mobile=patients["P01"]["mobile"], name=patients["P01"]["name"])
    p2 = _login(live, "patient", mobile=patients["P02"]["mobile"], name=patients["P02"]["name"])
    agent.reset_concurrency()
    results = []
    threads = [threading.Thread(target=lambda h=h: results.append(_chat(live, h, "How much is a new consultation?"))) for h in (p1, p2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 2
    assert agent.max_in_flight == 2, f"the two chats never ran at the same time (most at once: {agent.max_in_flight})"
