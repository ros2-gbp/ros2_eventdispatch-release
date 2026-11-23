"""Tests for the recording surface (eventdispatch.learning).

Run: python3 -m pytest tests/
"""
import threading
import time

import pytest

from eventdispatch.common1 import CommonEvent
from eventdispatch.core import Blackboard
from eventdispatch.learning import (
    PREEMPTED, RECORDINGS_KEY, RecordingEvent, RecordingLog,
    SimClockRecordingDispatch, WallClockRecordingDispatch)
from eventdispatch.sim import SimClockCSBQCVED, SimEvent


# -- events -------------------------------------------------------------
class Move(RecordingEvent):
    """Quotes args[0]; optionally reports an early finish args[1]."""
    def dispatch(self, ed, quote, actual=None):
        self.quote, self.actual = float(quote), actual
        return self.quote

    def run_outcome(self, ed, quote, actual=None):
        self.blackboard["ran"].append(self.get_id())
        if actual is not None:
            self.actual_duration = float(actual)
            self.wake = False
            return "BLOCKED"
        return "ARRIVED"


class QuietPreempt(Move):
    def on_preempt(self, ed):
        self.wake = False
        return {"reason": "test"}


class Tick(SimEvent):
    """A plain sim event: runs, never recorded."""
    def dispatch(self, ed, *args):
        return 1.0

    def finish(self, ed, *args):
        self.blackboard["ran"].append("tick")


class Sleep(RecordingEvent):
    """Wall clock: sleeps args[0] s, cooperatively interruptible."""
    def run_outcome(self, ed, secs):
        if self.wait_preempt(float(secs)):
            return "STOPPED_EARLY"
        return "SLEPT"

    def on_preempt(self, ed):
        self.wake = False


class Plain(CommonEvent):
    """A plain wall-clock event: runs on its thread, never recorded."""
    def dispatch(self, ed, *args):
        self.blackboard["ran"].append("plain")

    def finish(self, ed, *args):
        self.blackboard["plain_done"].set()


def make_bb(**classes):
    bb = Blackboard()
    bb["ran"] = []
    bb["plain_done"] = threading.Event()
    bb.update(classes)
    return bb


def sim(cls=SimClockRecordingDispatch):
    bb = make_bb(Move=Move, QuietPreempt=QuietPreempt, Tick=Tick)
    return bb, cls(bb, "ed")


# -- sim clock ------------------------------------------------------------
def test_sim_records_completion_with_quote():
    bb, ed = sim()
    assert isinstance(bb[RECORDINGS_KEY], RecordingLog)
    assert ed.do_dispatch(bb, ["Move", 3])
    ed.advance_one()
    (rec,) = ed.recordings.take(timeout=1)
    assert rec["event"] == "Move" and rec["outcome"] == "ARRIVED"
    assert rec["started_at"] == 0 and rec["t"] == 3 and rec["duration"] == 3
    assert rec["wake"] is True and "actual_duration" not in rec


def test_sim_plain_event_runs_unrecorded():
    bb, ed = sim()
    assert ed.do_dispatch(bb, ["Tick", 0])
    ed.advance_one()
    assert bb["ran"] == ["tick"]
    assert ed.recordings.pending == 0


def test_sim_requires_a_quote():
    bb, ed = sim()

    class NoQuote(RecordingEvent):
        def run_outcome(self, ed):
            return "X"
    bb["NoQuote"] = NoQuote
    assert not ed.do_dispatch(bb, ["NoQuote"])


def test_sim_settles_early_finish():
    bb, ed = sim()
    assert ed.do_dispatch(bb, ["Move", 5, 2])
    ed.advance_one()
    (rec,) = ed.recordings.take(timeout=1)
    assert rec["outcome"] == "BLOCKED" and rec["actual_duration"] == 2
    assert rec["wake"] is False
    assert ed.now == 2  # settled back from the quote


def test_sim_actual_beyond_quote_is_not_settled():
    bb, ed = sim()
    assert ed.do_dispatch(bb, ["Move", 2, 5])
    ed.advance_one()
    (rec,) = ed.recordings.take(timeout=1)
    assert rec["actual_duration"] == 5 and ed.now == 2


def test_sim_settle_refused_with_engine_adapter():
    class Engine(SimClockRecordingDispatch):
        def step_world(self, from_t, to_t):
            pass  # stands in for mj_step
    bb, ed = sim(Engine)
    assert ed.do_dispatch(bb, ["Move", 5, 2])
    with pytest.raises(RuntimeError, match="cannot settle"):
        ed.advance_one()


def test_sim_cancel_writes_preempted_and_never_runs():
    bb, ed = sim()
    assert ed.do_dispatch(bb, ["Move", 10])
    assert ed.do_dispatch(bb, ["QuietPreempt", 4])
    ids = list(ed._pending_map)
    move_id, quiet_id = ids[0], ids[1]
    ed.cancel(quiet_id)
    (pre,) = ed.recordings.take(timeout=1)
    assert pre["outcome"] == PREEMPTED and pre["event"] == "QuietPreempt"
    assert pre["wake"] is False and pre["notes"] == {"reason": "test"}
    assert pre["elapsed"] == 0 and pre["progress"] == 0
    assert ed.advance_one() == move_id
    assert bb["ran"] == [move_id]  # the cancelled one never ran
    (done,) = ed.recordings.take(timeout=1)
    assert done["event"] == "Move"


def test_recording_event_on_plain_sim_dispatch_is_unrecorded():
    bb = make_bb(Move=Move)
    ed = SimClockCSBQCVED(bb, "ed")
    assert ed.do_dispatch(bb, ["Move", 1])
    ed.advance_one()
    assert bb["ran"] and RECORDINGS_KEY not in bb


def test_sim_run_driver_end_to_end():
    bb, ed = sim()
    bb["ed_queue"].extend([["Move", 2], ["Tick", 0], ["Move", 1]])
    t = threading.Thread(target=ed.run, args=(bb, "ed"), daemon=True)
    t.start()
    got = []
    deadline = time.time() + 5
    while len(got) < 2 and time.time() < deadline:
        got += ed.recordings.take(timeout=0.5)
    bb["ed_hb"] = False
    with bb["ed_mutex"]:
        bb["ed_cv"].notify_all()
    t.join(timeout=2)
    assert [r["t"] for r in got] == [1, 2]
    assert "tick" in bb["ran"]


# -- wall clock -------------------------------------------------------------
def wall():
    bb = make_bb(Sleep=Sleep, Plain=Plain)
    return bb, WallClockRecordingDispatch(bb, "wed")


def test_wall_records_real_times_without_quote():
    bb, ed = wall()
    assert ed.do_dispatch(bb, ["Sleep", 0.05])
    (rec,) = ed.recordings.take(timeout=5)
    assert rec["outcome"] == "SLEPT" and rec["wake"] is True
    assert "duration" not in rec
    assert rec["t"] - rec["started_at"] >= 0.04


def test_wall_plain_event_runs_unrecorded():
    bb, ed = wall()
    assert ed.do_dispatch(bb, ["Plain", 0])
    assert bb["plain_done"].wait(5)
    assert bb["ran"] == ["plain"]
    assert ed.recordings.pending == 0


def test_wall_cancel_is_cooperative_and_recorded_on_return():
    bb, ed = wall()
    assert ed.do_dispatch(bb, ["Sleep", 30])
    event_id = next(iter(ed._live))
    time.sleep(0.05)
    t0 = time.monotonic()
    ed.cancel(event_id)
    (rec,) = ed.recordings.take(timeout=5)
    assert time.monotonic() - t0 < 5  # stopped early, not after 30 s
    assert rec["outcome"] == PREEMPTED
    assert rec["partial_outcome"] == "STOPPED_EARLY"
    assert rec["wake"] is False  # on_preempt ran last and won
    assert rec["elapsed"] >= 0.04 and "progress" not in rec
    assert not ed._live and not ed._cancelled


def test_wall_dispatch_return_value_is_ignored():
    bb, ed = wall()

    class Quotes(RecordingEvent):
        def dispatch(self, ed, *args):
            return 123.0

        def run_outcome(self, ed, *args):
            return "OK"
    bb["Quotes"] = Quotes
    assert ed.do_dispatch(bb, ["Quotes"])
    (rec,) = ed.recordings.take(timeout=5)
    assert rec["outcome"] == "OK" and "duration" not in rec
