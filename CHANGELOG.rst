^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package eventdispatch_python
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.3.0 (2026-11-23)
------------------
* Breaking: the learning surface is rebuilt on recording
* RecordingEvent replaces TrainingEvent; a sibling of SimEvent (both ClockedEvent)
* SimClockRecordingDispatch replaces TrainingDispatch; settling raises when step_world() drives an engine
* New WallClockRecordingDispatch: recording events run run_outcome() on their own thread, in real time
* Recordings field (key "recordings", RecordingLog) replaces the completions field; PREEMPTED records, on_preempt hook
* Removed TrainingEvent, TrainingDispatch, LearningEvent, LearningDispatch, CompletionLog, COMPLETIONS_KEY
* example_learning rewritten for both clocks; test/test_recording.py added
* Contributors: Charlie Yan

0.2.28 (2026-04-03)
------------------
* Event baseclass daemon=True
* Contributors: Charlie Yan

0.2.26 (2025-12-14)
------------------
* ROSEvents msg / srv
* Contributors: Charlie Yan

0.2.25 (2025-12-08)
------------------
* bugfix
* Contributors: Charlie Yan

0.2.24 (2025-12-06)
------------------
* initial commit
* Contributors: Charlie Yan
