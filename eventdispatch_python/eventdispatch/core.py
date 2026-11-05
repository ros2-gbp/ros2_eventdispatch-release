'''
Created on 2025-05-23

@author: Charlie Yan

Copyright (c) 2025, Charlie Yan
License: Apache-2.0 (see LICENSE for details)
'''
from __future__ import print_function

import sys, time

import threading, collections
from threading import Condition, Lock, Thread

def wrap_instance_method(instance, method_name, wrapper_with_args):
    '''instance.<method_name> = wrapper_with_args(instance.<method_name>)'''
    wrapped_method = wrapper_with_args(getattr(instance, method_name))
    setattr(instance, method_name, wrapped_method)

def call_when_switch_turned_on(obj, switch, switch_lock):
    '''
    decorator factory: the wrapped call raises while obj.<switch>
    is off; the check and the call happen under obj.<switch_lock>
    '''
    def decorator(func):  # func should return void
        def wrapper(*args, **kwargs):
            lock_obj = getattr(obj, switch_lock)
            lock_obj.acquire()  # optionally, non-blocking acquire
            switch_state = getattr(obj, switch)
            if (not switch_state):  # tells you lock state @ this call, it may be released immediately after
                lock_obj.release() # 2019-01-09 SUPER #IMPORTANT
                raise Exception("call_when_switch_turned_on: off, doing nothing")
            res = func(*args, **kwargs)
            lock_obj.release()
            return res
        return wrapper
    return decorator

class Blackboard(dict):
    '''
    a dict with per-key condition variables

    register_payload(key) rents a cv set for a key; setting that
    key afterwards appends the value to the set's queue and
    notifies its waiters (open-the-mouth-before-you-feed-it).
    release_cv(key) returns the set to the pool.
    '''
    def __init__(self, *args, **kwargs):
        # self.mutex = Lock()
        self.update(dict(*args, **kwargs))  # use the free update to set keys

        # free cvs
        self.cv_pool_lock = Lock()
        self.cv_pool = []
        self.cvs = {}

    def __setitem__(self, key, value):
        # with self.mutex:
        #     super(Blackboard, self).__setitem__(key, value)

        super(Blackboard, self).__setitem__(key, value)
        if key in self.cvs:
            print("blackboard notifying",
                key,
                value)
            self.cvs[key]['queue'].append(value)
            with self.cvs[key]['l']:
                self.cvs[key]['cv'].notify_all()

    # def __getitem__(self, key):
    #     with self.mutex:
    #         res = super().__getitem__(key)
    #     return res

    # def __delitem__(self, key):
    #     with self.mutex:
    #         super().__delitem__(key)

    def release_cv(self, key):
        '''return key's cv set to the pool'''
        print("RELEASING CV", key)
        with self.cv_pool_lock:
            if key not in self.cvs:
                print("RELEASING MISSING CV!", key)
            else:
                cv_set = self.cvs.pop(key)
                cv_set['count'] = 0
                cv_set.pop('match_target')
                self.cv_pool.append(cv_set)

    def register_payload(self, payload, match_target = None):
        '''rent a cv set for payload (recycled from the pool, or new)'''
        with self.cv_pool_lock:
            if len(self.cv_pool) > 0:
                cv_set = self.cv_pool.pop(0)
            else:
                lock = threading.Lock()
                cv_set = {
                    'l' : lock,
                    'cv' : threading.Condition(lock),
                    'count' : 0,
                    'queue' : [],
                    # pattern: if the mouth is not open before you feed it, using a queue is one solution
                    # the other solution is to enforce the sync using some blocking logic
                }
            cv_set["match_target"] = match_target
            self.cvs[payload] = cv_set
        return cv_set

class EventThread(threading.Thread):
    """
    a thread, with callback, oneshot, delay, terminate additions
    """
    def __init__(self,
        callback = None,
        oneshot = True,
        delay_secs = None,
        *args, **kwargs):
        super(EventThread, self).__init__(*args, **kwargs)
        self.callback = callback
        self.oneshot = oneshot
        self.delay_secs = delay_secs

        # IMPORTANT, threading library
        # wants to call a function _stop()
        # so we must name this to not override that
        self._stop_event = threading.Event()

    def terminate(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.isSet()

    def run(self):
        # print "starting up stoppable thread"
        if self.delay_secs is not None:
            time.sleep(self.delay_secs)
        super(EventThread, self).run()
        # NOTE: events must deal with exceptions internally
        if self.oneshot:
            self.terminate()
        if self.callback:
            self.callback()

class Event(object):
    '''
    an interface / abstract-base-class
    child classes must override deserialize, dispatch, and finish
    '''
    def __init__(self, event_id, *args, **kwargs):
        self.event_id = event_id
        # note: on construction, does NOT have/need a blackboard
        # when dispatched, it MAY have a blackboard (access to actors)
        self.blackboard = None

        # note: on construction, does NOT have/need an ED
        # when dispatched, it MUST have an ED (access to dispatch, events)
        self.event_dispatch = None
        # not fixed, can be changed across different dispatches

        self.daemon = True # default to shutdown cleanly, children override

    # methods for the child to override
    def get_id(self):
        '''the id this dispatch instance knows this event by'''
        # return self.__class__.__name__ + "@" + str(self.event_id)
        return self.event_id

    @staticmethod
    def deserialize(ed, blackboard, *args, **kwargs):
        '''
        MUST override: turn a serialized form into
        (constructor_args, dispatch_args); involves no instance
        '''
        raise NotImplementedError

    def dispatch(self, event_dispatch, *args, **kwargs):
        '''
        MUST override: the work itself; runs in its own thread.
        Do risky / uncertain stuff here.
        Args can come from dispatch, the constructor, or the blackboard.
        '''
        raise NotImplementedError

    def finish(self, event_dispatch, *args, **kwargs):
        '''
        MUST override: called when dispatch's thread returns.
        Deal with the outcomes here; only the event decides
        what is dispatched next.
        '''
        raise NotImplementedError

class EventDispatch(object):
    '''
    the core mechanism: dispatch(event) spawns an EventThread
    running event.dispatch; when that thread returns, its callback
    dispatch_finish calls event.finish and releases the event id.

    dispatch is wrapped at construction with a switch guard: an
    event can set self.dispatch_switch off to stop all further
    dispatching until it is cleared -- the safety axis.
    '''
    def __init__(self, blackboard = None, ed_id = None):
        self.thread_registry = {}
        self.mutex_registry = {}

        self.event_id_pool = set()
        self.event_id_pool_all = set()

        self.dispatch_mutex = threading.Lock()
        self.dispatch_switch_mutex = threading.Lock()
        self.dispatch_switch = True

        wrap_instance_method(self, 'dispatch',
            call_when_switch_turned_on(
            self, "dispatch_switch",
            "dispatch_switch_mutex")) # [example] decorator
        # safety mechanism:
        # if any event sets the switch off
        # no other events are dispatched
        # until the switch is cleared

        if (blackboard is not None and ed_id is not None):
            # give an ED a blackboard on which other EDs live
            # for when there is no ROS infrastructure for example
            self.blackboard = blackboard
            self.ed_id = ed_id
            self.blackboard[ed_id] = self # register self on blackboard

    # something child ED class can override
    # for Event.deserialize to form some event_id
    def reserve_event_id(self):
        '''an id unique within this dispatch instance; recycled'''
        if len(self.event_id_pool) > 0:
            return self.event_id_pool.pop()
        else:
            new_id = len(self.event_id_pool_all)
            self.event_id_pool_all.add(new_id)
            return new_id

    def release_event_id(self, event_id):
        '''return event_id to the pool'''
        self.event_id_pool.add(event_id)

    def dispatch(self, event, *args, **kwargs):
        '''spawn the event's thread (guarded by the dispatch switch)'''
        with self.dispatch_mutex:
            # print("dispatching %s" % (event.get_id())) # debug
            self.thread_registry[event.get_id()] = EventThread(
                target=lambda args = args, kwargs = kwargs:\
                    event.dispatch(self, *args, **kwargs),
                # note that the event is dispatched with a reference
                # to the event dispatch, giving access / control
                # over other events
                callback=lambda event = event, args = args, kwargs = kwargs:\
                    self.dispatch_finish(event, *args, **kwargs),
                daemon=event.daemon
            )
            self.thread_registry[event.get_id()].start()
            # NOTE: events must deal with exceptions internally

    def dispatch_finish(self, event, *args, **kwargs):
        '''thread callback: unregister, call finish, release the id'''
        with self.dispatch_mutex:
            event_id = event.get_id()
            self.thread_registry.pop(event_id, None)

        # self.log("finishing %s" % (event.get_id())) # debug
        event.finish(self, *args, **kwargs)
        # ONLY the event defines what is dispatched next
        # this includes multiple subsequent concurrent events

        self.release_event_id(event_id)
