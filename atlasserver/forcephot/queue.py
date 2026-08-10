"""Queue ordering: assigning each unfinished task its position in the execution order.

Lives outside views.py so that the task runner can import it without pulling in DRF, bokeh and the
rest of the web stack, and so that the two processes cannot drift on what "queue position" means.
"""

import datetime
import operator
import typing as t

from django.core.cache import caches
from django.db import models
from django.db import transaction

from atlasserver.forcephot.models import Task

# Set by the web app whenever it changes the set of queued tasks, consumed by the task runner.
# A flag rather than a queue: the answer to "does this need renumbering" does not accumulate.
RECALC_FLAG_CACHEKEY: t.Final = "queue-positions-dirty"

# Renumber at least this often even when the flag says nothing changed. Covers the task runner's
# own dispatches (which move the running task to the front) and any flag lost to a cache eviction
# or a wiped cache directory, so a stale ordering can never persist.
RECALC_MAX_INTERVAL_SECONDS: t.Final = 30.0


def request_recalc() -> None:
    """Ask the task runner to renumber the queue.

    Cheaper than renumbering here: calculate_queue_positions() locks every queued row, so calling
    it inline made a submission wait for the whole queue and serialised concurrent submitters
    behind one another. The runner already polls this table twice a second, and it is the only
    process that changes which task is running, so it is the natural owner of the ordering.
    """
    caches["default"].set(RECALC_FLAG_CACHEKEY, True, timeout=None)


def consume_recalc_request() -> bool:
    """Return whether a renumbering was requested, clearing the request.

    Clears before the caller renumbers, not after: a submission that lands during the renumbering
    then leaves the flag set and gets its own pass, rather than being swallowed by a clear that
    happens after its changes were already missed.
    """
    cache = caches["default"]
    requested = bool(cache.get(RECALC_FLAG_CACHEKEY))
    if requested:
        cache.delete(RECALC_FLAG_CACHEKEY)

    return requested


def next_queuepos_relative() -> int:
    """Return a queue position at the back of the current queue.

    Gives a task submitted between two renumberings a position that sorts correctly and displays
    about right, instead of the NULL that reads as "no position yet" on the queue page.
    """
    maxpos = Task.objects.filter(finishtimestamp__isnull=True, is_archived=False).aggregate(
        models.Max("queuepos_relative")
    )["queuepos_relative__max"]

    return 0 if maxpos is None else maxpos + 1


def calculate_queue_positions() -> None:
    """Calculate and assign the queue positions (determining the order of execution in the task runner) for all queued tasks."""
    with transaction.atomic():
        # Lock the queued rows and read them once. Without the lock, two concurrent
        # recalculations can each renumber from a snapshot that is missing the other's changes
        # and end up assigning duplicate queue positions.
        queuedtasks = list(
            Task.objects.select_for_update()
            .filter(finishtimestamp__isnull=True, is_archived=False)
            .order_by("user_id", "timestamp", "id")
        )

        # to get position in current pass, check if job currently running (the one started last).
        # attrgetter rather than a lambda: the generator's None filter cannot narrow the
        # attribute type inside a lambda, so a lambda key would not type check
        runningtask = max(
            (tsk for tsk in queuedtasks if tsk.starttimestamp is not None),
            key=operator.attrgetter("starttimestamp"),
            default=None,
        )
        runningtaskid = runningtask.id if runningtask is not None else None
        runningtask_userid = runningtask.user_id if runningtask is not None else None

        queuedtaskcount = len(queuedtasks)

        unassigned_taskids = [t.id for t in queuedtasks]
        unassigned_task_userids = [t.user_id for t in queuedtasks]

        # work through passes (max one task per user in each pass) assigning queue positions from 0 (next) upwards
        queuepos: int = 0
        passnum: int = 0
        # collected and written in one statement at the end: issuing an UPDATE per task meant a
        # round trip per task while holding a lock on every queued row, so a deep queue made every
        # submission slow and serialised concurrent submitters behind it
        queuepos_updates: dict[int, int] = {}
        while unassigned_taskids:
            useridsassigned_currentpass = set()

            if passnum == 0 and runningtaskid is not None:
                # currently running task will be assigned position 0
                try:
                    index = unassigned_taskids.index(runningtaskid)
                    unassigned_taskids.pop(index)
                    unassigned_task_userids.pop(index)
                    useridsassigned_currentpass.add(runningtask_userid)
                    queuepos_updates[runningtaskid] = 0
                    queuepos = 1
                except ValueError:  # the task disappeared between the two queries?
                    runningtaskid = None

            # collect the tasks not assigned in this pass rather than popping from
            # the lists during iteration (which would skip elements)
            remaining_taskids: list[int] = []
            remaining_task_userids: list[int] = []
            for taskid, task_userid in zip(unassigned_taskids, unassigned_task_userids, strict=True):
                if task_userid not in useridsassigned_currentpass and (
                    passnum != 0 or runningtask_userid is None or (task_userid > runningtask_userid)
                ):
                    queuepos_updates[taskid] = queuepos
                    useridsassigned_currentpass.add(task_userid)
                    queuepos += 1
                else:
                    remaining_taskids.append(taskid)
                    remaining_task_userids.append(task_userid)

            unassigned_taskids = remaining_taskids
            unassigned_task_userids = remaining_task_userids

            # bail out rather than spin forever if a pass somehow assigns nothing. Not an assert:
            # those are stripped under python -O, which is exactly when a hung request would be
            # hardest to explain
            if passnum >= (2 * queuedtaskcount + 1):
                msg = f"queue position assignment made no progress after {passnum} passes over {queuedtaskcount} tasks"
                raise RuntimeError(msg)

            passnum += 1

        if queuepos_updates:
            # task_modified_datetime is written explicitly: it is an auto_now field, and auto_now
            # is applied by Model.save(), not by a bulk write. Without it a reordering would be
            # invisible to get_tasklist_etag() and a user could be served a stale queue position.
            now = datetime.datetime.now(datetime.UTC)
            Task.objects.bulk_update(
                [
                    Task(id=taskid, queuepos_relative=newpos, task_modified_datetime=now)
                    for taskid, newpos in queuepos_updates.items()
                ],
                ["queuepos_relative", "task_modified_datetime"],
            )
