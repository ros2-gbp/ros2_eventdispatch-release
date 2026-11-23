# ros2_eventdispatch

**ROS(ROS2)** are solutions to the *inter-process communication* problem, among other metaphors

[python-eventdispatch](https://python-eventdispatch.readthedocs.io/en/latest/) ([github](https://github.com/cyan-at/python-eventdispatch)) uses ROS2 as a message bus for robotics use-cases.

It does so with 3 packages in this upstream super-repo.
1. eventdispatch_python (provides the **eventdispatch** library)
2. eventdispatch_ros2_interfaces (ROSEvent.msg/srv)
3. eventdispatch_ros2 (**ed_node**, **stage.launch**)

---

# ed_node

Instantiates a class child of **CSBQCVED**, **Node**

```
class ROS2QueueCVED(CSBQCVED, Node):
```

```
CSBQCVED :
Composite
Semaphore
Blackboard
Queue
Condition
Variable
Event
Dispatch
```

1. `~/dispatch` service, subscription
2. `~/dispatch_list` service, subscription (batch version)
3. `events_module_path` ros2 parameter

The `events_module_path` is a folder that is expected to have an `events.py` file that defines the following variables:

```
from events import event_dict, initial_events, events_module_update_blackboard, on_shutdown, make_validator
```

(note that events.py can be symlinked, and import each other or refactored for reuse)

* **event_dict**

This is a mapping `str -> Class` for the blackboard

* **initial_events**

list[[event primitives]], initial boundary conditions to dispatch

* **events_module_update_blackboard**

populate the blackboard with assets, ROS adapters, etc.

* **on_shutdown**

intended to be a graceful handler, but ROS shutdown is blunt

* **make_validator**

return a class that checks requests, stub to add extra logic

---

# stage.launch

Convienence launch file, exposing the 2 most important launch args to `ed_node`

1. node_name
2. events_module_path

The idea is to stand up various `ed_node` instances via `stage.launch` as needed.

Since `ed` is such a compact framework, it may be more likely to expand capability by growing the `Event` subclasses in a blackboard than spawning more instances, but is an option anyways.