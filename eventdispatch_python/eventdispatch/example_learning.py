"""Example learning: shake out the learning surface (learning.py).

Trivial TrainingEvents on a TrainingDispatch over a Blackboard,
following example_core / example_cs's shape. What this
demonstrates, on the finished surface (TrainingEvent /
TrainingDispatch from eventdispatch.learning):

  1. Events quote durations; completions land in the dispatch's
     completions field in duration order, not enqueue order, and
     the harness drains them with take().
  2. A zero-arg event dispatches from the queue (TrainingEvent's
     deserialize drops the non-empty-tail check).
  3. A quote is a commitment the outcome may settle short of: an
     event whose run_outcome() sets actual_duration records the
     settled duration beside the quote, and the dispatch clock
     settles to started_at + actual_duration.

Run:  python -m eventdispatch.example_learning
"""
from __future__ import annotations

import threading
import time

from eventdispatch.core import Blackboard
from eventdispatch.learning import TrainingDispatch, TrainingEvent


class WorkEvent(TrainingEvent):
    """Quotes its duration. The outcome is DONE, or SETTLED when
    an actual duration shorter than the quote is reported."""

    def dispatch(self, ed, tag, duration, actual=None):
        return float(duration)

    def run_outcome(self, ed, tag, duration, actual=None):
        if actual is not None:
            self.actual_duration = float(actual)
            return "SETTLED"
        return "DONE"


class PingEvent(TrainingEvent):
    """Zero-arg event: quotes 0.5, outcome PONG."""

    def dispatch(self, ed):
        return 0.5

    def run_outcome(self, ed):
        return "PONG"


def enqueue(ed, blackboard, payloads):
    blackboard[ed.cv_name].acquire()
    blackboard[ed.queue_name].extend(payloads)
    blackboard[ed.cv_name].notify(1)
    blackboard[ed.cv_name].release()


def main():
    blackboard = Blackboard()
    blackboard["WorkEvent"] = WorkEvent
    blackboard["PingEvent"] = PingEvent

    ed = TrainingDispatch(blackboard, "learning")
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
    records = []
    deadline = time.time() + 10.0
    while len(records) < 3 and time.time() < deadline:
        records.extend(ed.completions.take(timeout=1.0))
    assert [r["event"] for r in records] == [
        "PingEvent", "WorkEvent", "WorkEvent"], records
    assert [r["t"] for r in records] == [0.5, 1.0, 2.0], records
    assert records[0]["outcome"] == "PONG", records
    assert records[1]["args"][0] == "fast", records
    assert records[2]["args"][0] == "slow", records
    print("learning surface: duration order + zero-arg dispatch PASS")

    # settling short of the quote (clock stands at 2.0 now)
    enqueue(ed, blackboard, [["WorkEvent", "settles", 2.0, 0.5]])
    records = ed.completions.take(timeout=10.0)
    assert len(records) == 1, records
    rec = records[0]
    assert rec["started_at"] == 2.0, rec
    assert rec["duration"] == 2.0, rec
    assert rec["actual_duration"] == 0.5, rec
    assert rec["outcome"] == "SETTLED", rec
    assert ed.now == 2.5, ed.now
    print("learning surface: clock settling PASS")

    blackboard[ed.hb_key] = False
    with blackboard[ed.mutex_name]:
        blackboard[ed.cv_name].notify_all()
    thread.join()
    print("learning surface constructs and runs:",
          type(ed).__name__)


if __name__ == "__main__":
    main()
