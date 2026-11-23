"""Example learning: shake out the learning surface (learning.py).

Trivial RecordingEvents on both recording dispatches over a
Blackboard, following example_core / example_cs's shape. What this
demonstrates (RecordingEvent, SimClockRecordingDispatch and
WallClockRecordingDispatch from eventdispatch.learning):

  1. Sim clock: events quote durations; records land in the
     dispatch's recordings field in duration order, not enqueue
     order, and the harness drains them with take().
  2. A zero-arg event dispatches from the queue (RecordingEvent's
     deserialize drops the non-empty-tail check).
  3. A quote is a commitment the outcome may settle short of: an
     event whose run_outcome() sets actual_duration records it
     beside the quote, and the sim clock settles to
     started_at + actual_duration.
  4. Wall clock: the same kind of event runs run_outcome() on its
     own thread in real time; the record carries real times and
     no quote.
  5. Preemption: a cancelled event is recorded PREEMPTED. On the
     wall clock it stops cooperatively and its partial outcome is
     recorded; on_preempt() decides its wake.

Run:  python -m eventdispatch.example_learning
"""
from __future__ import annotations

import threading
import time

from eventdispatch.core import Blackboard
from eventdispatch.learning import (
    PREEMPTED, RecordingEvent, SimClockRecordingDispatch,
    WallClockRecordingDispatch)


class WorkEvent(RecordingEvent):
    """Quotes its duration. The outcome is DONE, or SETTLED when
    an actual duration shorter than the quote is reported."""

    def dispatch(self, ed, tag, duration, actual=None):
        return float(duration)

    def run_outcome(self, ed, tag, duration, actual=None):
        if actual is not None:
            self.actual_duration = float(actual)
            return "SETTLED"
        return "DONE"


class PingEvent(RecordingEvent):
    """Zero-arg event: quotes 0.5, outcome PONG."""

    def dispatch(self, ed):
        return 0.5

    def run_outcome(self, ed):
        return "PONG"


class WaitEvent(RecordingEvent):
    """Wall clock: waits up to `secs`, stopping early if preempted.
    A preempt does not wake the policy (on_preempt)."""

    def run_outcome(self, ed, secs):
        if self.wait_preempt(float(secs)):
            return "STOPPED_EARLY"
        return "WAITED"

    def on_preempt(self, ed):
        self.wake = False


def enqueue(ed, blackboard, payloads):
    blackboard[ed.cv_name].acquire()
    blackboard[ed.queue_name].extend(payloads)
    blackboard[ed.cv_name].notify(1)
    blackboard[ed.cv_name].release()


def take(ed, n, timeout=10.0):
    records = []
    deadline = time.time() + timeout
    while len(records) < n and time.time() < deadline:
        records.extend(ed.recordings.take(timeout=1.0))
    return records


def stop(ed, blackboard, thread):
    blackboard[ed.hb_key] = False
    with blackboard[ed.mutex_name]:
        blackboard[ed.cv_name].notify_all()
    thread.join()


def run_sim():
    blackboard = Blackboard()
    blackboard["WorkEvent"] = WorkEvent
    blackboard["PingEvent"] = PingEvent

    ed = SimClockRecordingDispatch(blackboard, "learning")
    blackboard["learning"] = ed
    thread = threading.Thread(
        target=ed.run, args=(blackboard, "learning", None, None))
    thread.start()

    # out-of-order quotes, plus a zero-arg event
    enqueue(ed, blackboard, [
        ["WorkEvent", "slow", 2.0],
        ["WorkEvent", "fast", 1.0],
        ["PingEvent"],
    ])
    records = take(ed, 3)
    assert [r["event"] for r in records] == [
        "PingEvent", "WorkEvent", "WorkEvent"], records
    assert [r["t"] for r in records] == [0.5, 1.0, 2.0], records
    assert records[0]["outcome"] == "PONG", records
    assert records[1]["args"][0] == "fast", records
    assert records[2]["args"][0] == "slow", records
    print("sim clock: duration order + zero-arg dispatch PASS")

    # settling short of the quote (clock stands at 2.0 now)
    enqueue(ed, blackboard, [["WorkEvent", "settles", 2.0, 0.5]])
    (rec,) = take(ed, 1)
    assert rec["started_at"] == 2.0, rec
    assert rec["duration"] == 2.0, rec
    assert rec["actual_duration"] == 0.5, rec
    assert rec["outcome"] == "SETTLED", rec
    assert ed.now == 2.5, ed.now
    print("sim clock: clock settling PASS")

    stop(ed, blackboard, thread)


def run_wall():
    blackboard = Blackboard()
    blackboard["WaitEvent"] = WaitEvent

    ed = WallClockRecordingDispatch(blackboard, "wall")
    blackboard["wall"] = ed
    thread = threading.Thread(
        target=ed.run, args=(blackboard, "wall", None, None))
    thread.start()

    enqueue(ed, blackboard, [["WaitEvent", 0.2]])
    (rec,) = take(ed, 1)
    assert rec["outcome"] == "WAITED" and "duration" not in rec, rec
    assert rec["t"] - rec["started_at"] >= 0.15, rec
    print("wall clock: real-time record PASS")

    enqueue(ed, blackboard, [["WaitEvent", 30.0]])
    deadline = time.time() + 5.0
    while not ed._live and time.time() < deadline:
        time.sleep(0.01)
    (event_id,) = list(ed._live)
    time.sleep(0.1)
    ed.cancel(event_id)
    (rec,) = take(ed, 1)
    assert rec["outcome"] == PREEMPTED, rec
    assert rec["partial_outcome"] == "STOPPED_EARLY", rec
    assert rec["wake"] is False, rec
    print("wall clock: cooperative preemption PASS")

    stop(ed, blackboard, thread)


def main():
    run_sim()
    run_wall()
    print("learning surface constructs and runs on both clocks")


if __name__ == "__main__":
    main()
