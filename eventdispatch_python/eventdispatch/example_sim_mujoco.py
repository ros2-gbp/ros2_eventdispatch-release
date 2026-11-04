"""Example sim (MuJoCo): stand up SimClockCSBQCVED against MuJoCo.

What this demonstrates:

  1. The queue discipline is unchanged: events are enqueued as
     serialized [name, args] items; the driver drains them.
  2. SimEvents quote durations (seconds here -- the clock is
     unit-agnostic); the completion heap fires finish() in
     duration order, not enqueue order.
  3. The MuJoCo adapter adds only world stepping: step_world()
     turns a clock advance into mj_step calls, so mjData.time
     stays slaved to the dispatch clock (event.now()).
  4. finish() reads fresh engine state at completion time.

Three timed events are enqueued at t=0 with durations 0.3 / 0.1 /
0.2 s against a free-falling ball; they must complete in the order
B (0.1), C (0.2), A (0.3), each observing the ball lower than the
last, at the analytic free-fall heights.

Run:  python -m eventdispatch.example_sim_mujoco   (requires mujoco)
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import mujoco

from eventdispatch.core import Blackboard
from eventdispatch.sim import SimClockCSBQCVED, SimEvent

MJCF = """
<mujoco>
  <option timestep="0.005"/>
  <worldbody>
    <geom type="plane" size="5 5 0.1" rgba="0.5 0.5 0.5 1"/>
    <body pos="0 0 1">
      <freejoint/>
      <geom type="sphere" size="0.1" mass="1"/>
    </body>
  </worldbody>
</mujoco>
"""


class MuJoCoSimClock(SimClockCSBQCVED):
    """SimClockCSBQCVED + MuJoCo world stepping (the only addition)."""

    def __init__(self, blackboard, name, model, data):
        super().__init__(blackboard, name)
        self._mj_model = model
        self._mj_data = data

    def step_world(self, from_t, to_t):
        steps = int(round(
            (to_t - from_t) / self._mj_model.opt.timestep))
        for _ in range(steps):
            mujoco.mj_step(self._mj_model, self._mj_data)


class TimedEvent(SimEvent):
    """Quotes a duration; at completion, records the ball's state."""

    def dispatch(self, ed, tag, duration):
        self.log("TimedEvent {}: quoted {}s at now={}".format(
            tag, duration, self.now()))
        return float(duration)

    def finish(self, ed, tag, duration):
        data = self.blackboard["mj_data"]
        z = float(data.qpos[2])
        vz = float(data.qvel[2])
        self.log("TimedEvent {}: finish at now={}, ball z={:.4f}, "
                 "vz={:.3f}".format(tag, self.now(), z, vz))
        self.blackboard["results"].append((tag, self.now(), z, vz))
        return {"event": tag, "outcome": "DONE"}


def main():
    model = mujoco.MjModel.from_xml_string(MJCF)
    data = mujoco.MjData(model)

    blackboard = Blackboard()
    blackboard["TimedEvent"] = TimedEvent
    blackboard["mj_data"] = data
    blackboard["results"] = []
    blackboard["sim"] = SimpleNamespace(t=0.0)  # clock sync target

    ed = MuJoCoSimClock(blackboard, "sim1", model, data)
    blackboard["sim1"] = ed
    thread = threading.Thread(
        target=ed.run, args=(blackboard, "sim1", None, None))
    thread.start()

    # enqueue three events at t=0, deliberately out of duration order
    blackboard[ed.cv_name].acquire()
    blackboard[ed.queue_name].extend([
        ["TimedEvent", "A", 0.3],
        ["TimedEvent", "B", 0.1],
        ["TimedEvent", "C", 0.2],
    ])
    blackboard[ed.cv_name].notify(1)
    blackboard[ed.cv_name].release()

    deadline = time.time() + 10.0
    while len(blackboard["results"]) < 3 and time.time() < deadline:
        time.sleep(0.01)

    blackboard[ed.hb_key] = False
    with blackboard[ed.mutex_name]:
        blackboard[ed.cv_name].notify_all()
    thread.join()

    results = blackboard["results"]
    assert [r[0] for r in results] == ["B", "C", "A"], results
    assert [r[1] for r in results] == [0.1, 0.2, 0.3], results
    # free fall from z=1: z(t) = 1 - 4.905 t^2 (no floor contact yet)
    for tag, t, z, vz in results:
        assert abs(z - (1.0 - 4.905 * t * t)) < 0.01, (tag, t, z)
    assert abs(float(data.time) - 0.3) < 1e-6, data.time
    assert blackboard["sim"].t == 0.3, blackboard["sim"].t
    print("example_sim_mujoco: PASS -- completion order B, C, A at "
          "now = 0.1 / 0.2 / 0.3; mjData.time = {}; ball z at "
          "completion = {}".format(
              float(data.time), [round(r[2], 4) for r in results]))


if __name__ == "__main__":
    main()
