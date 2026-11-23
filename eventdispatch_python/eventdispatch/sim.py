"""Simulated-clock dispatch for the eventdispatch framework.

SimClockCSBQCVED is CSBQCVED driven by a simulated clock instead of
wall-clock threads. It sits above whatever computes the physics --
handwritten sim, MuJoCo, PyBullet -- stepped explicitly by the
caller: the dispatch owns event time, the engine owns world state.

The library's two thread layers are replaced:

  per event: no EventThread is spawned for an event that quotes.
      The seam is do_dispatch --
      the funnel through which every queue item that quotes
      reaches the clock (the CS pair never quotes: prior_cb
      dispatches it threaded, as the base does).
      Overridden here to keep its contract (deserialize, construct,
      True/False) and change only the vehicle: the event's dispatch()
      is called synchronously for its duration quote, the completion
      is scheduled on the clock, and at completion time the library's
      own dispatch_finish fires finish() in duration order.
  per queue: run() is overridden as an advance driver, so the
      spin-up site is unchanged -- Thread(target=ed.run) starts the
      driver instead of the drain loop. The driver pumps one drain
      cycle (prior_cb, do_dispatch, post_cb -- hooks untouched, so
      the composite-semaphore semantics carry into sim time), fires
      the soonest completion, and blocks on the queue condition
      variable only when idle (heap and queue both empty), waking
      on a producer's notify. It never sleeps toward a completion:
      the driver is the only thread touching the clock, the heap,
      or event execution, so operation order stays heap order.
      The class is self-sufficient -- no external pump.

do_dispatch is the only dispatch behavior replaced; run() is the
only other override. The completion heap and advance_one() are
additions, driven from inside run().

prior_cb / post_cb run inside every drain cycle; post_cb is
inherited untouched. prior_cb is overridden in exactly one
respect: where the base blocks on the cs condition variable
until every expected CSWait registers, this one returns the
gate as a verdict (True / False) and run() breaks the drain
cycle on False -- the outstanding set is held across cycles,
so the drain defers until registration (which happens on the
CSWait's own thread, below) without ever stalling the driver.

CSWait and CSRelease are clockless elements, and they stay in
threads, outside the sim. A CSWait completes by condition, not
duration: its dispatch parks its own thread on the composite
semaphore until a CSRelease releases it, so it cannot quote a
completion time for the heap; a CSRelease is an instantaneous
signal whose finish is a no-op. prior_cb therefore dispatches
the pair on the base class's threaded vehicle -- a new
EventThread, in wall-clock thread space, never reading the
clock -- before any quoting happens: CSWaits in the base's
partition, CSReleases once the gate is open (a release may
only fire after its waiters have registered). What crosses
into the sim is only the consequence: CSWait.finish appends
its pending event to the queue, and the driver drains and
runs that event in-sim like any other. Every other event
lives within the sim: quoted, heaped, and finished in
duration order.

SimEvent is the Event base class for this dispatch: dispatch()
quotes a duration, finish() is the completion step. The base
stamps started_at / quoted_duration at dispatch and exposes the
dispatch clock through the now() member function, so the same
event interface is implemented against every simulator.
Engine adapters subclass SimClockCSBQCVED and override
step_world() only.
"""
from __future__ import annotations

import heapq
import itertools

from eventdispatch.common1 import CommonEvent, bcolors, wrap_with_prints
from eventdispatch.composite_semaphore import CSBQCVED, CSRelease, CSWait
from eventdispatch.core import wrap_instance_method


class SimEvent(CommonEvent):
    """Event base class for simulated-clock systems.

    Sim-time protocol (subclasses override both):

      dispatch(ed, ...) -> duration. Scheduling only: quote the
          duration, stash the args. No outcome sampling, no
          mutation.
      finish(ed, ...)   -> the completion step. Run the outcome
          model at completion time, against fresh state, apply the
          event's effects, and emit the release signal.

    A cancelled event never reaches finish(): it never mutates
    state.
    """

    def __init__(self, event_id, *args, **kwargs):
        super(SimEvent, self).__init__(event_id, *args, **kwargs)
        # stamped by SimClockCSBQCVED.do_dispatch at quote time
        self.started_at = None
        self.quoted_duration = None

    def now(self):
        """Current simulated time, read from the running dispatch."""
        if self.event_dispatch is None:
            raise RuntimeError(
                "SimEvent.now(): event has not been dispatched")
        return self.event_dispatch.now

    def elapsed(self):
        """Simulated time since this event was dispatched."""
        if self.started_at is None:
            return None
        return self.now() - self.started_at

    def progress(self):
        """Elapsed fraction of the quoted duration, in [0, 1] at
        completion; None when the duration is 0 or unset."""
        if not self.quoted_duration:
            return None
        return self.elapsed() / self.quoted_duration

    def dispatch(self, ed, *args, **kwargs):
        raise NotImplementedError(
            "sim-time protocol: quote duration")

    def finish(self, ed, *args, **kwargs):
        raise NotImplementedError(
            "sim-time protocol: finish is the completion step -- "
            "run the outcome model, emit the release signal")


class SimClockCSBQCVED(CSBQCVED):
    """CSBQCVED on a simulated clock (see module docstring).

    Pure dispatch machinery: no policy, no world, no iterables.
    Self-sufficient: run() is the advance driver. post_cb is
    inherited untouched; prior_cb reuses the base's CS
    partition and returns the CS gate as a verdict instead of
    blocking; the CS pair is dispatched threaded inside
    prior_cb, outside the sim.
    """

    def __init__(self, blackboard, name):
        super(SimClockCSBQCVED, self).__init__(blackboard, name)
        self.now = 0                    # simulated clock
        self._pending = []              # heap of [complete_at, seq, event_id]
        self._pending_map = {}          # event_id -> (event, dispatch_args)
        self._seq = itertools.count()
        self._cancelled = set()
        self._cs_outstanding = set()  # CSWait ids dispatched, unregistered

    # -- the dispatch seam -------------------------------------------
    def do_dispatch(self, blackboard, serialized_class_args):
        '''Same contract as BlackboardQueueCVED.do_dispatch
        (deserialize, construct, True/False); only the vehicle
        changes: quote inline, schedule on the clock.
        The CS pair never reaches here: prior_cb dispatches
        both on the base's threaded vehicle (see prior_cb).
        '''
        if len(serialized_class_args) == 0:
            return False

        try:
            constructor_args, dispatch_args = blackboard[
                serialized_class_args[0]].deserialize(
                    self,
                    blackboard,
                    serialized_class_args[1:])
            event = blackboard[serialized_class_args[0]](
                *constructor_args)
            event.event_dispatch = self
            # event.dispatch's return is mapped to a duration
            # here (the int of the older sim lineage). Units are
            # the clock's -- ticks, seconds -- the clock is agnostic.
            duration = event.dispatch(self, *dispatch_args)
            if duration is None or duration < 0:
                raise ValueError(
                    "sim-time protocol: dispatch() must return a "
                    "duration >= 0, got {!r}".format(duration))
            event.started_at = self.now
            event.quoted_duration = duration
            heapq.heappush(self._pending, [
                self.now + duration,
                next(self._seq), event.get_id()])
            self._pending_map[event.get_id()] = (event, dispatch_args)
        except Exception as e:
            self.internal_log(self.ed_id
                + " SimClockCSBQCVED: failed dispatch %s, exception %s" % (
                str(serialized_class_args), str(e)))
            return False

        return True

    # -- clock --------------------------------------------------------
    def step_world(self, from_t, to_t):
        """Advance the world from from_t to to_t.

        Base: no world -- the clock alone is the mechanism. Engine
        adapters override: MuJoCo steps mj_step, PyBullet steps
        stepSimulation, a handwritten sim steps its step(dt).
        """
        pass

    def advance_one(self):
        """Fire the soonest pending completion.

        Steps the world to the completion time, sets the clock,
        and fires the library's dispatch_finish (finish() + release
        of the event id). Returns the completed event's id, or None
        when nothing live is pending.
        """
        while self._pending:
            complete_at, _, event_id = heapq.heappop(self._pending)
            if event_id in self._cancelled:
                self._cancelled.discard(event_id)
                self.release_event_id(event_id)
                continue
            event, dispatch_args = self._pending_map.pop(event_id)
            self.step_world(self.now, complete_at)
            self.now = complete_at
            bb = event.blackboard
            if bb is not None and "sim" in bb:
                bb["sim"].t = self.now
            self.dispatch_finish(event, *dispatch_args)
            return event_id
        return None

    def cancel(self, event_id):
        """Preempt a pending event. It never reaches finish()."""
        self._cancelled.add(event_id)
        self._pending_map.pop(event_id, None)

    @property
    def has_pending(self) -> bool:
        return bool(self._pending_map)

    @property
    def in_flight_count(self) -> int:
        return len(self._pending_map)

    def time_to_next(self):
        """Simulated time until the next live completion, or None."""
        for complete_at, _, event_id in sorted(self._pending):
            if event_id not in self._cancelled:
                return complete_at - self.now
        return None

    # -- the CS gate (replaces CSBQCVED.prior_cb's blocking wait) -----
    def prior_cb(self, blackboard):
        """The CS gate, returned as a verdict instead of a block.

        The partition itself is the base's (super): this batch's
        CSWaits are dispatched threaded there, overlapping ones
        rejected. Where the base then blocks on cs_cv until
        every expected identifier registers, this returns False
        while any accepted identifier is still unregistered, and
        run() breaks the drain cycle on False. The outstanding
        set is stateful across cycles: a CSWait dispatched in
        one cycle keeps the gate closed in the next until it
        registers. (A rejected CSWait does not gate -- the base
        counts it in its expected set anyway, one divergence,
        deliberately.)

        CSReleases, which the base leaves to the drain, are
        dispatched threaded here once the gate is open: they
        complete by condition, not duration, so they must never
        reach do_dispatch's quoting path, and a release may
        only fire after its waiters have registered. Returns
        True when open.
        """
        _, accepted_ids, _ = super()._dispatch_cs_waits(blackboard)

        self._cs_outstanding |= accepted_ids
        self._cs_outstanding -= blackboard["volatile"]["cs_set"]
        if len(self._cs_outstanding) > 0:
            self.internal_log(
                "SimClockCSBQCVED: CS gate closed, awaiting {}".format(
                    self._cs_outstanding))
            return False

        releases = [i for i in blackboard[self.queue_name]
                    if i[0] in ["CSRelease"]]
        if releases:
            blackboard[self.queue_name] = [
                i for i in blackboard[self.queue_name]
                if i[0] not in ["CSRelease"]]
            for release in releases:
                super().do_dispatch(blackboard, release)
        return True

    # -- the advance driver (replaces BlackboardQueueCVED.run) --------
    def run(self, blackboard,
        prefix,
        empty_cv_name = None,
        debug_color = None):
        assert(blackboard is not None)

        if (debug_color is not None):
            wrap_instance_method(self, 'log',
                wrap_with_prints(debug_color, bcolors.ENDC))
            wrap_instance_method(self, 'internal_log',
                wrap_with_prints(debug_color, bcolors.ENDC))

        while(blackboard[self.hb_key]):
            blackboard[self.mutex_name].acquire()
            # park only at true idle: nothing queued, nothing in flight
            while blackboard[self.hb_key] and (
                len(blackboard[self.queue_name]) == 0) and (
                len(self._pending_map) == 0):
                blackboard[self.cv_name].wait()

            if len(blackboard[self.queue_name]) == 0 and (
                len(self._pending_map) == 0):
                # woken with no work: shutdown
                blackboard[self.mutex_name].release()
                break

            ##### drain cycle: prior_cb gates; False breaks it #####
            gate_open = self.prior_cb(blackboard)

            if gate_open:
                while len(blackboard[self.queue_name]) > 0:
                    serialized_class_args = blackboard[self.queue_name].pop(0)

                    if not self.do_dispatch(blackboard, serialized_class_args):
                        self.internal_log(self.name + " failed dispatching something: {}".format(
                            serialized_class_args))
                        continue

                self.post_cb(blackboard)
            ############################################################

            blackboard[self.mutex_name].release()

            # fire one completion; its finish() may enqueue successors,
            # which the next cycle quotes at the new now
            if len(self._pending_map) > 0:
                self.advance_one()
            elif not gate_open:
                # gated, nothing in flight: park on cs_cv until a
                # registration notifies it (the timeout backstops
                # shutdown, which signals the queue cv instead)
                with blackboard["volatile"]["cs_cv_l"]:
                    blackboard["volatile"]["cs_cv"].wait(0.1)

            if empty_cv_name is not None:
                if len(blackboard[self.queue_name]) == 0 and\
                    empty_cv_name in blackboard:
                    self.internal_log("SimClockCSBQCVED: notifying " + empty_cv_name)
                    blackboard[empty_cv_name].acquire()
                    blackboard[empty_cv_name].notify_all()
                    blackboard[empty_cv_name].release()

        self.internal_log(self.name + " shutdown")
