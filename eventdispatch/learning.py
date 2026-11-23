"""Learning surface of the eventdispatch framework.

Every language implementation of the framework keeps one module like
this one: the surface that learning / training code programs against.
Training code imports from here, never from core internals.

Recording is what makes learning possible on this framework: an
event that opts in has its completion written as a record, and the
records are what a training or inference loop reads.

  RecordingEvent   -- the Event base class that opts in to recording.
                      A sibling of SimEvent (both are ClockedEvent,
                      eventdispatch.common1): it says nothing about
                      how time passes.
  RecordingMixin   -- the dispatch side: writes completion and
                      PREEMPTED records to the recordings field.
  SimClockRecordingDispatch  -- RecordingMixin on SimClockCSBQCVED
                      (eventdispatch.sim): quoted durations, a
                      simulated clock. Training runs here.
  WallClockRecordingDispatch -- RecordingMixin on CSBQCVED
                      (eventdispatch.composite_semaphore): each
                      recording event's run_outcome() runs on its
                      own thread, in real time. Inference and
                      calibration on hardware run here.
  RecordingLog     -- the recordings field's type.

Events that do not inherit RecordingEvent run untouched on both
recording dispatches and are never recorded.

Recording protocol (the contract RecordingEvent subclasses follow):

  dispatch(ed) -> scheduling only: stash the args, and return a
      duration quote in the clock's units. No outcome sampling,
      no mutation. The quote is required on the sim clock (it is
      what the heap schedules on) and ignored on the wall clock.
  run_outcome(ed) -> the outcome. The work: run the outcome model
      (sim clock, at completion time, against fresh state) or do
      the real work (wall clock, on the event's thread); apply the
      event's effects; return what happened.
  wake -> the event's own decision: class default True,
      settable per outcome from run_outcome(). A record whose wake
      is True always wakes the policy -- binding on the harness,
      not a hint. The dispatch never decides wake.
  on_preempt(ed) -> optional. Called when the event is preempted,
      after run_outcome() on the wall clock (so it wins); it may
      set self.wake and may return a dict of notes for the record.
      It must not mutate world state: a preempted sim event never
      ran, and must leave no effects.

The event does not record itself: the recording dispatch writes the
record. finish() is not part of the recording protocol; on a
dispatch that does not record, RecordingEvent.finish() runs
run_outcome() and returns the outcome, with no record.

The recordings field (blackboard key RECORDINGS_KEY) is a
RecordingLog the dispatch registers at construction. Records land
there in order: event id, class name, dispatch args, start / end
times, wake flag, outcome -- plus, on the sim clock, the quoted
duration, and actual_duration when the event set it. A preempted
recording event gets a record too, with outcome PREEMPTED, its
elapsed time (and, on the sim clock, its progress fraction), and on
the wall clock whatever run_outcome() returned as partial_outcome.
A training or inference loop consumes the log by take() -- block for
the next record, drain everything that has landed -- and engages the
policy on every record whose wake is set. Draining on use is the
field's hygiene: it has exactly one consumer, so the buffer never
holds more than one decision interval's records. The dispatch
itself never engages a policy; notification stops at the field.

Clock settling (sim clock only): a quote is a commitment the outcome
may settle short of. When run_outcome() sets self.actual_duration
below the quote (a navigation that blocks mid-plan is the canonical
case), SimClockRecordingDispatch settles the clock back to
started_at + actual_duration. Settling is only allowed while
step_world() is the base no-op -- i.e. when the events move the
world themselves. With an engine adapter (MuJoCo, PyBullet) the
world has already been stepped to the quote, and settling back
would step that interval twice, so it raises instead. An
actual_duration beyond the quote is recorded and ignored (the quote
is the commitment).

Preemption (cancel):
  sim clock  -- cancel(event_id) removes the pending completion; the
      event never runs. The PREEMPTED record is written at cancel
      time, with elapsed and progress.
  wall clock -- cancel(event_id) asks the running event to stop: it
      sets the event's preempt request and terminates its thread's
      stop flag. Stopping is the event's job (thread cooperation):
      run_outcome() polls preempt_requested() / waits on
      wait_preempt(), or cancels its own action, then returns its
      partial outcome. The PREEMPTED record is written when the
      event actually returns, with the real elapsed time. There is
      no timeout: an event that never returns writes no record.
"""
from __future__ import annotations

import threading
import time

from eventdispatch.common1 import ClockedEvent
from eventdispatch.composite_semaphore import CSBQCVED
from eventdispatch.core import EventThread
from eventdispatch.sim import SimClockCSBQCVED

RECORDINGS_KEY = "recordings"

PREEMPTED = "PREEMPTED"


class RecordingLog:
    """The recordings field: a condition-signalled buffer of
    records, in order, drained on use.

    Writers append (the recording dispatch, on whichever thread
    the completion lands). The one reader -- the harness --
    consumes by take(): records leave the buffer as they are read,
    so the buffer stays bounded by one decision interval no matter
    how long the run. snapshot() is a non-consuming peek, for tests
    and diagnostics only; it is not a read path.
    """

    def __init__(self):
        self._records = []
        self._cv = threading.Condition()

    def append(self, record):
        with self._cv:
            self._records.append(record)
            self._cv.notify_all()

    def take(self, timeout=None):
        """Block until at least one record exists, then drain and
        return everything buffered, in order. Returns [] on
        timeout."""
        with self._cv:
            if not self._cv.wait_for(lambda: len(self._records) > 0,
                                     timeout):
                return []
            records, self._records = self._records, []
            return records

    @property
    def pending(self):
        """Records buffered right now (not yet taken)."""
        with self._cv:
            return len(self._records)

    def snapshot(self):
        """Everything buffered, as a list copy. Does not drain."""
        with self._cv:
            return list(self._records)


class RecordingEvent(ClockedEvent):
    """Event base class that opts in to recording (module
    docstring). Subclasses override dispatch() and run_outcome(),
    and optionally on_preempt(); never finish().

    wake: class-level default True; run_outcome() / on_preempt()
    may set self.wake. A record whose wake is True always wakes the
    policy.

    actual_duration: None unless the outcome consumed less than
    quoted (sim clock settling, module docstring). Recorded when
    set, on either clock.

    deserialize drops CommonEvent's non-empty-tail check: a
    zero-arg event (an empty tail) is dispatchable from the queue.
    """

    wake = True
    actual_duration = None

    def __init__(self, event_id, *args, **kwargs):
        super(RecordingEvent, self).__init__(event_id, *args, **kwargs)
        self._preempt = threading.Event()

    @staticmethod
    def deserialize(ed, blackboard, *args, **kwargs):
        tokens = args[0] if args else []
        return (ed.reserve_event_id(), blackboard), tuple(tokens)

    def dispatch(self, ed, *args, **kwargs):
        """Stash the args and return a duration quote. The default
        quotes nothing, which only the wall clock accepts; events
        that run on the sim clock override this."""
        return None

    def run_outcome(self, ed, *args, **kwargs):
        raise NotImplementedError(
            "recording protocol: run_outcome() does the work and "
            "returns the outcome")

    def on_preempt(self, ed):
        """Optional preemption hook (module docstring). May set
        self.wake; may return a dict of notes for the record; must
        not mutate world state."""
        return None

    def finish(self, ed, *args, **kwargs):
        """Only reached on a dispatch that does not record:
        run the outcome, record nothing."""
        return self.run_outcome(ed, *args, **kwargs)

    # -- wall clock thread cooperation -----------------------------
    def preempt_requested(self):
        """True once the dispatch has asked this event to stop."""
        return self._preempt.is_set()

    def wait_preempt(self, timeout=None):
        """Block until a preempt is requested or timeout passes;
        True if requested. Use as an interruptible sleep."""
        return self._preempt.wait(timeout)


class RecordingMixin:
    """The dispatch side of recording (module docstring).

    Mixed in ahead of a dispatch class that provides `now` and
    the completion path (dispatch_finish). Recording events get
    records; every other event passes through untouched. The
    clock-specific parts are the hooks _outcome_of, _clock_fields
    and _settle, implemented by each recording dispatch.
    """

    def __init__(self, blackboard, name):
        super(RecordingMixin, self).__init__(blackboard, name)
        if RECORDINGS_KEY not in blackboard:
            blackboard[RECORDINGS_KEY] = RecordingLog()
        self._last_finished = None

    @property
    def recordings(self) -> RecordingLog:
        return self.blackboard[RECORDINGS_KEY]

    # -- records ----------------------------------------------------
    def _record(self, event, args, outcome, **fields):
        record = {
            "event_id": event.get_id(),
            "event": type(event).__name__,
            "args": [list(a) if isinstance(a, (list, tuple))
                     else a for a in args],
            "started_at": event.started_at,
            "t": self.now,
        }
        record.update(self._clock_fields(event))
        record["wake"] = bool(event.wake)
        record["outcome"] = outcome
        if event.actual_duration is not None:
            record["actual_duration"] = event.actual_duration
        record.update(fields)
        self.blackboard[RECORDINGS_KEY].append(record)
        self._last_finished = self._note(type(event).__name__, outcome)
        return record

    def _record_preempted(self, event, args, **fields):
        notes = event.on_preempt(self)
        if isinstance(notes, dict) and notes:
            fields["notes"] = notes
        fields["elapsed"] = event.elapsed()
        progress = event.progress()
        if progress is not None:
            fields["progress"] = progress
        return self._record(event, args, PREEMPTED, **fields)

    @staticmethod
    def _note(name, outcome):
        if isinstance(outcome, dict):
            outcome = outcome.get("outcome")
        if isinstance(outcome, str) and outcome:
            return "{} {}".format(name, outcome)
        return name

    # -- the completion path ----------------------------------------
    def dispatch_finish(self, event, *args, **kwargs):
        if not isinstance(event, RecordingEvent):
            return super(RecordingMixin, self).dispatch_finish(
                event, *args, **kwargs)
        event_id = event.get_id()
        with self.dispatch_mutex:
            self.thread_registry.pop(event_id, None)
        outcome = self._outcome_of(event, *args, **kwargs)
        if self._was_preempted(event):
            self._record_preempted(event, args, partial_outcome=outcome)
        else:
            self._record(event, args, outcome)
            self._settle(event)
        self.release_event_id(event_id)
        return outcome

    # -- hooks (per clock) ------------------------------------------
    def _outcome_of(self, event, *args, **kwargs):
        raise NotImplementedError

    def _clock_fields(self, event):
        return {}

    def _was_preempted(self, event):
        return False

    def _settle(self, event):
        pass

    # -- cycle annotation -------------------------------------------
    def post_cb(self, blackboard):
        """The base cycle marker, annotated with the recording
        event whose record landed most recently (it may lag the
        finish by one cycle); plain marker otherwise."""
        note = getattr(self, "_last_finished", None)
        if note is None:
            super(RecordingMixin, self).post_cb(blackboard)
            return
        self._last_finished = None
        self.internal_log("POST_CB! (finished: {})".format(note))


class SimClockRecordingDispatch(RecordingMixin, SimClockCSBQCVED):
    """Recording on the simulated clock (module docstring).

    Recording events must quote a duration; run_outcome() runs at
    completion time on the driver thread. Records carry the quoted
    duration. Settling is guarded: allowed only while step_world()
    is the base no-op.
    """

    def _outcome_of(self, event, *args, **kwargs):
        return event.run_outcome(self, *args, **kwargs)

    def _clock_fields(self, event):
        return {"duration": event.quoted_duration}

    def _settle(self, event):
        if event.actual_duration is None or event.started_at is None:
            return
        settled = event.started_at + event.actual_duration
        if settled > self.now:
            return  # beyond the quote: recorded, never settled
        if settled == self.now:
            return
        if (type(self).step_world
                is not SimClockCSBQCVED.step_world):
            raise RuntimeError(
                "{}: cannot settle the clock for {} (event {}) back "
                "from {} to {}: step_world() steps an engine, which "
                "has already advanced to the quote. Quote shorter, "
                "or keep settling to worlds the events move "
                "themselves.".format(
                    type(self).__name__, type(event).__name__,
                    event.get_id(), self.now, settled))
        if self._pending and settled > self._pending[0][0]:
            return
        self.now = settled
        bb = event.blackboard
        if bb is not None and "sim" in bb:
            bb["sim"].t = settled

    def cancel(self, event_id):
        """Preempt a pending event: it never runs. A recording
        event gets its PREEMPTED record now."""
        entry = self._pending_map.get(event_id)
        if entry is not None and isinstance(entry[0], RecordingEvent):
            event, dispatch_args = entry
            self._record_preempted(event, dispatch_args)
        super(SimClockRecordingDispatch, self).cancel(event_id)


class WallClockRecordingDispatch(RecordingMixin, CSBQCVED):
    """Recording on the wall clock (module docstring).

    A recording event's dispatch() runs inline (it stashes the
    args; its return is ignored), then run_outcome() runs on the
    event's own thread; when it returns, the record is written.
    Any other event is dispatched exactly as CSBQCVED does. `now`
    is time.monotonic(), in seconds. No timeout, no quote.
    """

    def __init__(self, blackboard, name):
        super(WallClockRecordingDispatch, self).__init__(blackboard, name)
        self._live = {}                 # event_id -> event
        self._cancelled = set()
        self._live_lock = threading.Lock()

    @property
    def now(self):
        return time.monotonic()

    def dispatch(self, event, *args, **kwargs):
        if not isinstance(event, RecordingEvent):
            return super(WallClockRecordingDispatch, self).dispatch(
                event, *args, **kwargs)
        event.event_dispatch = self
        event.dispatch(self, *args, **kwargs)  # stash; quote ignored
        event.started_at = self.now
        event_id = event.get_id()
        with self._live_lock:
            self._live[event_id] = event

        def work(event=event, args=args, kwargs=kwargs):
            event._outcome = event.run_outcome(self, *args, **kwargs)

        with self.dispatch_mutex:
            self.thread_registry[event_id] = EventThread(
                target=work,
                callback=lambda event=event, args=args, kwargs=kwargs:
                    self.dispatch_finish(event, *args, **kwargs),
                daemon=event.daemon)
            self.thread_registry[event_id].start()

    def _outcome_of(self, event, *args, **kwargs):
        return getattr(event, "_outcome", None)

    def _was_preempted(self, event):
        event_id = event.get_id()
        with self._live_lock:
            self._live.pop(event_id, None)
            if event_id in self._cancelled:
                self._cancelled.discard(event_id)
                return True
        return False

    def cancel(self, event_id):
        """Ask a running recording event to stop (thread
        cooperation); its PREEMPTED record is written when it
        returns. A running plain event's thread gets its stop
        flag set, as terminate() does."""
        with self._live_lock:
            event = self._live.get(event_id)
            if event is not None:
                self._cancelled.add(event_id)
                event._preempt.set()
        with self.dispatch_mutex:
            thread = self.thread_registry.get(event_id)
        if thread is not None:
            thread.terminate()
