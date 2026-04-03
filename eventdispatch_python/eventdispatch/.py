# (c) Meta Platforms, Inc. and affiliates. Confidential and proprietary.

# pyre-strict

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time, typing
from asyncio import AbstractEventLoop
from datetime import timedelta

from eventdispatch.common1 import CommonEvent
from eventdispatch.composite_semaphore import CSBQCVED
from eventdispatch.core import Blackboard

from facebook.zippydb_common.types import Code
from libfb.py.sitevar import get_sitevar
from typing import Any, Dict, List

# from facebook.zgateway.thrift_clients import ZGatewayService
from facebook.zippydb_proxy.thrift_clients import ZippyDBProxyService
from facebook.zippydb_proxy.thrift_types import (
    ZAbortTransaction,
    ZCommitTransaction,
    ZConsistencyType,
    ZDelete,
    ZGet,
    ZPut,
    ZReadOptions,
    ZShardSpecifier,
    ZStartTransaction,
    ZTransactionSpecifier,
    ZWrite,
    ZWriteData,
    ZWriteOptions,
)

from nodeapi.projects.robotics.nodes.robot_task_record import (
    NodeRobotTaskRecord,
    NodeRobotTaskRecordSource,
    NodeRobotTaskRecordStatus,
)

# from libfb.py.sitevar import get_sitevar

from robotics.api.robotics_api import RoboticsAPI
from robotics.api.robotics_api_constants import RoboticsApiConstants

from robotics.communication.data_observers.robotics_aggregated_data_observer_base import (
    RoboticsAggregatedDataObserverBase,
)

from servicerouter.py3 import ClientParams, get_sr_client

ZIPPDB_USE_CASE_ID = 52566

ROBOTICS_ID = 89002005597608

RETRY_PUT = 0
RETRY_COMMIT = 1
OK = 2
ABORTED = 3
FAIL = 4
RETRY_DEL = 5

AGV_EVENT_KEY = "/agv/events/data"

NOT_FOUND = 0
FOUND = 1
OTHER = 2

serialized_defaults = {"dispatch_enter,nha1:datahall_C": "111, 121"}

########################## helper functions

async def get_ztxn(self):
    async with get_sr_client(
        ZippyDBProxyService,
        tier="zgateway.prod.proxy.wildcard",
        params=self.client_params,
    ) as client:
        return await client.StartTransaction(
            ZStartTransaction(shardSpecifier=self.zshard)
        )

async def txn_read(
    zshard: ZShardSpecifier,
    logger: logging.Logger,
    transaction: ZTransactionSpecifier,
    client: typing.Any,
    key: str,
    kNotFound: str = "not_found",
) -> tuple[int, str]:

    k = key.encode("utf-8")
    zget = ZGet(
        shardSpecifier=zshard,
        transactionSpecifier=transaction,
        ttl=True,
        key=k,
    )
    get_result = await client.Get(
        zget, ZReadOptions(consistencyType=ZConsistencyType.STRONG)
    )
    logger.info(f"txn_read zget key {key}, code={get_result.result.code}")

    if get_result.result.code == Code.kNotFound:
        return NOT_FOUND, kNotFound
    elif get_result.result.code == Code.kOk:
        return FOUND, get_result.value.decode("utf-8")

    return OTHER, str(get_result.result.code)

async def txn_write(
    zshard: ZShardSpecifier,
    logger: logging.Logger,
    transaction: ZTransactionSpecifier,
    client: typing.Any,
    key: list[str],
    val: list[str],
    dispatcher: typing.Any,
    events: list[typing.Any],
) -> int:
    try:
        for i in range(len(key)):
            k = key[i].encode("utf-8")
            v = val[i].encode("utf-8")
            zput = ZPut(
                key=k,
                value=v,
            )
            write_request = ZWrite(
                shardSpecifier=zshard,
                transactionSpecifier=transaction,
                data=ZWriteData(zput=zput),
                ttl=int(timedelta(hours=6).total_seconds()),
            )
            put_result = await client.Write(
                write_request,
                ZWriteOptions(consistencyType=ZConsistencyType.STRONG),
            )

            logger.warn(f"zget key {k}, code={put_result.code}")
            if put_result.code == Code.kRetryableError:
                logger.warn("put_result kRetryableError!")
                return RETRY_PUT
            elif put_result.code == Code.kOk:
                logger.warn("put_result kOk!")

        # for cb in cbs_inside_txn:
        for event_left, event_right in events:
            await dispatcher(
                zshard, logger, transaction, client, event_left, event_right
            )

        commit_result = await client.CommitTransaction(
            ZCommitTransaction(
                transactionSpecifier=transaction,
                shardSpecifier=zshard,
            ),
            writeOptions=ZWriteOptions(
                consistencyType=ZConsistencyType.STRONG, retryNumber=2
            ),
        )
        if commit_result.code == Code.kRetryableError:
            logger.warn("commit_result kRetryableError!")
            return RETRY_COMMIT
        elif commit_result.code == Code.kOk:
            return OK
        else:
            zabort_transaction = ZAbortTransaction(
                shardSpecifier=zshard, transactionSpecifier=transaction
            )
            logger.warn(f"zabort_transaction!!! {commit_result.code}")
            await client.AbortTransaction(
                zabort_transaction,
                ZWriteOptions(consistencyType=ZConsistencyType.STRONG),
            )
            return ABORTED
    except Exception as e:
        logger.warn(f"txn_write failed: {e}")
        return FAIL

async def txn_del(
    zshard: ZShardSpecifier,
    logger: logging.Logger,
    transaction: ZTransactionSpecifier,
    client: typing.Any,
    key: str,
) -> int:
    k = key.encode("utf-8")
    try:
        zdelete = ZDelete(
            key=k,
        )

        write_request = ZWrite(
            shardSpecifier=zshard,
            data=ZWriteData(zdelete=zdelete),
        )
        result = await client.Write(
            write_request,
            ZWriteOptions(consistencyType=ZConsistencyType.STRONG),
        )

        logger.warn(f"zdelete key {k}, code={result.code}")
        if result.code == Code.kRetryableError:
            logger.warn("result kRetryableError!")
            return RETRY_DEL
        elif result.code == Code.kOk:
            logger.warn("result kOk!")

        return OK
    except Exception as e:
        logger.warn(f"txn_del failed: {e}")
        return FAIL

def zdb_lock_guard(self, keys, ztxn = None):
    '''
    Create a ztransaction if not given
    '''
    if ztxn is None:
        ztxn = asyncio.run(self.get_ztxn())

def zdb_lock_release(zclient, zshard, ztxn):
    try:
        commit_result = await client.CommitTransaction(
            ZCommitTransaction(
                transactionSpecifier=transaction,
                shardSpecifier=zshard,
            ),
            writeOptions=ZWriteOptions(
                consistencyType=ZConsistencyType.STRONG, retryNumber=2
            ),
        )
        if commit_result.code == Code.kRetryableError:
            logger.warn("commit_result kRetryableError!")
            return RETRY_COMMIT
        elif commit_result.code == Code.kOk:
            return OK
        else:
            zabort_transaction = ZAbortTransaction(
                shardSpecifier=zshard, transactionSpecifier=transaction
            )
            logger.warn(f"zabort_transaction!!! {commit_result.code}")
            await client.AbortTransaction(
                zabort_transaction,
                ZWriteOptions(consistencyType=ZConsistencyType.STRONG),
            )
            return ABORTED
    except Exception as e:
        logger.warn(f"txn_write failed: {e}")
        return FAIL

##########################

class ObserverEvent(CommonEvent):
    def log(self, *args):  # pyre-ignore[2, 3]
        self.blackboard["logger"].warn(*args)

class LockGuardedEvent(ObserverEvent):
    def dispatch(self, event_dispatch, *args, **kwargs):  # pyre-ignore[2, 3]
        pass

    def finish(self, event_dispatch, *args, **kwargs):  # pyre-ignore[2, 3]
        pass

##########################

class EnterEvent(ObserverEvent):
    def dispatch(self, event_dispatch, *args, **kwargs):  # pyre-ignore[2, 3]
        self.log("EnterEvent dispatch! {}".format(",".join(args)))

        now = time.time()

        # # since dispatching happens in a sibling thread
        # # no need for coroutine async defs / awaits
        # new_node_record = (
        #     NodeRobotTaskRecord.create()
        #     .set_creation_time(now)
        #     .set_last_modified_time(now)
        #     .set_created_by_id(RoboticsApiConstants.API_USER_ID)
        #     .set_last_modified_by_id(RoboticsApiConstants.API_USER_ID)
        #     .set_description("EnterEvent eviction")
        #     .set_data_center("atn7")
        #     .set_suite("7a")
        #     .set_target_waypoints()
        #     .set_source(NodeRobotTaskRecordSource.TASK_SCHEDULER)
        #     .set_netgram_workflow(self.node_record.netgram_workflow)
        #     .set_expiration_time(self.node_record.expiration_time)
        #     .set_is_repeat_workflow(True)
        #     .set_priority(self.node_record.priority)
        #     .set_intern_owner_id(self.node_record.intern_owner_id)
        # )

        # # set edges to robot & reservation & workflow
        # node_robot = await self.node_record.query_robot_nodes().first_enforce()
        # await NodeRobotTaskRecord.edge_robot.set(new_node_record, node_robot)
        # node_reservation = await self.node_record.query_reservation_nodes().first()
        # if node_reservation:
        #     await NodeRobotTaskRecord.edge_reservation.set(
        #         new_node_record, node_reservation
        #     )
        # node_workflow = await self.node_record.query_workflow_nodes().first()
        # if node_workflow:
        #     await NodeRobotTaskRecord.edge_workflow.set(new_node_record, node_workflow)

        # # log the event where task record for the repeat workflow is created
        # await ArdioSchedulerCommonUtils.log_robotics_event(
        #     ArdioSchedulerLoggerConstants.TASK_MANAGER_SCRIPT_REPEAT_WORKFLOW_RECORD_CREATED,
        #     {
        #         "request_time": str(self.ts),
        #         "reservation_id": (
        #             str(node_reservation.id) if node_reservation else None
        #         ),
        #         "task_record_id": str(self.node_record.id),
        #         "repeat_workflow_task_record_id": str(new_node_record.id),
        #         "comment": f"created repeat workflow task record {new_node_record.id} for robot {node_robot.id}",
        #     },
        #     node_robot.id,
        # )

    def finish(self, event_dispatch, *args, **kwargs):  # pyre-ignore[2, 3]
        self.log("EnterEvent finish!")


class ExitEvent(ObserverEvent):
    def dispatch(self, event_dispatch, *args, **kwargs):  # pyre-ignore[2, 3]
        self.log("ExitEvent dispatch! {}".format(",".join(args)))

    def finish(self, event_dispatch, *args, **kwargs):  # pyre-ignore[2, 3]
        self.log("ExitEvent finish!")


class LocationState:
    def __init__(
        self,
        location: str,
        state: str,
    ) -> None:
        self.location = location
        self.state = state

    def to_dict(self) -> dict[str, str]:
        return {
            "location": self.location,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> LocationState:
        return cls(
            data["location"] if "location" in data else "UNKNOWN",
            data["state"] if "state" in data else "UNKNOWN",
        )

    def encode(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def decode(cls, data: str) -> LocationState:
        try:
            return cls.from_dict(json.loads(data))
        except Exception:
            return cls.from_dict({"location": "", "state": ""})





class ObserverCSBQCVED(CSBQCVED):
    def __init__(
        self,
        blackboard: Blackboard,
        name: str,
        observer: RoboticsSynchronizationObserver,
    ) -> None:
        super(ObserverCSBQCVED, self).__init__(blackboard, name + "_dispatch")

        self.observer = observer

    def internal_log(self, msg, params=None) -> None: # pyre-ignore[2]
        self.observer._logger.info(msg)

class RoboticsSynchronizationObserver(RoboticsAggregatedDataObserverBase):
    """
    This observer looks for AGV events
    and translates them to scheduler updates
    """

    def __init__(self) -> None:
        super().__init__()

        self._logger: logging.Logger = logging.getLogger(self.__class__.__name__)
        self._logger.warning("RoboticsSynchronizationObserver stood up")

        self._loop: AbstractEventLoop = asyncio.new_event_loop()

        self._robotics_api = RoboticsAPI()
        self.fod_config: Dict[str, Any] = get_sitevar("ROBOTICS_FOD_CONFIG")

        self.zshard = ZShardSpecifier(useCase=ZIPPDB_USE_CASE_ID, shard=0)
        self.client_params = ClientParams().setConfigScopeName(  # pyre-ignore[4]
            "use_sticky_hosts"
        )

        # 0. Create `Blackboard` instance(s)
        self.blackboard = Blackboard()
        self.blackboard["logger"] = self._logger
        # TODO(charlie) zippdb itself can be a fleet-wide blackboard

        # 1. Populate the `Blackboard` with `Event` declarations (name : type pairs)
        # blackboard["dispatch_exit"] = DispatchExit
        self.blackboard["dispatch_enter"] = EnterEvent
        self.blackboard["dispatch_exit"] = ExitEvent

        # 2. Create ED instance(s) with their individual `name` strings
        self.ed1 = ObserverCSBQCVED(self.blackboard, "ed1", self)
        self.blackboard["ed1"] = self.ed1

        # 3. Stand up their `run` targets as threads
        self.blackboard["ed1_thread"] = threading.Thread(
            target=self.ed1.run,
            args=(
                self.blackboard,
                "ed1",
                None,
            ),
        )
        self.blackboard["ed1_thread"].start()

        ###############################

        # self.blackboard[self.ed1.cv_name].acquire()
        # self.blackboard[self.ed1.queue_name].extend(
        #     [
        #         ["dispatch_enter", "task_record,111"],
        #     ]
        # )
        # self.blackboard[self.ed1.cv_name].notify(1)
        # self.blackboard[self.ed1.cv_name].release()

    async def dispatch_helper(
        self,
        zshard: ZShardSpecifier,
        logger: logging.Logger,
        transaction: ZTransactionSpecifier,
        client: typing.Any,
        serialized_left: str,
        serialized_right: str,
    ) -> None:
        res, value = await txn_read(
            zshard, logger, transaction, client, serialized_left
        )

        if serialized_left in serialized_defaults:
            res = FOUND
            value = serialized_defaults[serialized_left]

        if res == NOT_FOUND:
            logger.warn(f"{serialized_left} kNotFound, noop")
        elif res == FOUND:
            logger.warn(f"{serialized_left} found!, {value}")

            # here is where the serialized (specific left)
            # gets decomposed to a generic event class + args
            tokens = serialized_left.split(",")
            tokens.append(serialized_right)
            tokens.append(value)

            self.blackboard[self.ed1.cv_name].acquire()
            self.blackboard[self.ed1.queue_name].extend([tokens])
            self.blackboard[self.ed1.cv_name].notify(1)
            self.blackboard[self.ed1.cv_name].release()

            if serialized_left not in serialized_defaults:
                # drain the event for the entire fleet
                result = await txn_del(
                    self.zshard,
                    self._logger,
                    transaction,
                    client,
                    serialized_left,
                )
                if result != OK:
                    logger.warn(f"{serialized_left} couldn't drain from zippdb!")
        else:
            logger.warn(f"{serialized_left} othercode!, {value}")

    async def _process(self, aggregated_data: typing.Any) -> None:
        if ROBOTICS_ID in aggregated_data:
            data = aggregated_data.pop(ROBOTICS_ID)



            if AGV_EVENT_KEY not in data:
                return
            data = json.loads(data.pop(AGV_EVENT_KEY))

            if "AGV" not in data:
                return
            agv = data["AGV"]

            if "Location" not in data:
                return
            location = data["Location"]
            if location == "Unknown":
                return

            if "Building" not in data:
                return
            building = data["Building"]
            if building == "Unknown":
                return

            latest_location = "{}:{}".format(building, location)

            ###################
            # this iterable
            # see transitions and selectively dispatches in a single zippdb txn context

            async with get_sr_client(
                ZippyDBProxyService,
                tier="zgateway.prod.proxy.wildcard",
                params=self.client_params,
            ) as client:
                transaction = await client.StartTransaction(
                    ZStartTransaction(shardSpecifier=self.zshard)
                )

                res, value = await txn_read(
                    self.zshard, self._logger, transaction, client, agv
                )

                if res == NOT_FOUND:
                    self._logger.warn(f"{agv} kNotFound, writing")

                    location_state = LocationState(latest_location, "SEEN")
                    result = await txn_write(
                        self.zshard,
                        self._logger,
                        transaction,
                        client,
                        [agv],
                        [location_state.encode()],
                        self.dispatch_helper,
                        [],
                    )
                    self._logger.warn(f"txn_write result={result}")
                elif res == FOUND:
                    old_locationstate = LocationState.decode(value)
                    old_location = old_locationstate.location

                    if old_location != latest_location:
                        self._logger.warn(
                            f"location changed: {old_location} -> {latest_location}"
                        )

                        location_state = LocationState(latest_location, "SEEN")
                        result = await txn_write(
                            self.zshard,
                            self._logger,
                            transaction,
                            client,
                            [agv],
                            [location_state.encode()],
                            self.dispatch_helper,
                            [
                                ("dispatch_exit,{}".format(old_location), agv),
                                ("dispatch_enter,{}".format(latest_location), agv),
                            ],
                        )
                        self._logger.warn(f"txn_write result={result}")
                    else:
                        self._logger.info(f"location unchanged, {old_location}")
                else:
                    self._logger.warn(f"{agv} some other result, noop {value}")
