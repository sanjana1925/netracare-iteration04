"""Live updates for the Staff dashboard: a tiny in-memory publish / subscribe hub (asyncio).

Code that changes something Staff care about (a new urgent case, an approval request, a rebooking, an appointment) calls `publish(...)`
AFTER it has saved the change. Every Staff browser that is listening gets one small event at once and re-reads its own lists, so the
dashboard no longer has to ask the server every few seconds. The events carry no patient details: only what kind of thing changed.

`publish` can be called from ordinary (threaded) code and from async code; each listener lives on the event loop that created it.
"""
import asyncio
import threading

_listeners: list = []          # (event loop, queue) pairs, one per open dashboard
_lock = threading.Lock()
QUEUE_SIZE = 100


def subscribe() -> asyncio.Queue:
    """Start listening (call from async code). Give the queue back to `unsubscribe` when done."""
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    with _lock:
        _listeners.append((asyncio.get_running_loop(), q))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _listeners[:] = [(loop, x) for loop, x in _listeners if x is not q]


def _put(q: asyncio.Queue, event: dict) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:   # a stuck browser must never slow the clinic down: it just misses this event (it re-reads on the next one)
        pass


def publish(kind: str, **info) -> None:
    """Tell every open Staff dashboard that something of this kind changed. Safe to call from any thread, and when nobody is listening."""
    event = {"type": kind, **info}
    with _lock:
        targets = list(_listeners)
    for loop, q in targets:
        try:
            loop.call_soon_threadsafe(_put, q, event)
        except RuntimeError:   # that event loop has already closed
            pass


def listener_count() -> int:
    with _lock:
        return len(_listeners)
