"""Queue ordering: assigning each unfinished task its position in the execution order.

Lives outside views.py so that the task runner can import it without pulling in DRF, bokeh and the
rest of the web stack, and so that the two processes cannot drift on what "queue position" means.
"""

import datetime
import operator
import statistics
import time
import typing as t

from django.core.cache import caches
from django.db import models
from django.db import transaction

from atlasserver.forcephot.models import Task

if t.TYPE_CHECKING:
    from collections.abc import Iterable

# Bumped by the web app when the queued set changes; the runner watches it and remembers the value
# it last acted on. A counter rather than a flag it clears, because clearing loses a request that
# arrives between the read and the delete. Simultaneous bumps can collapse into one increment,
# which does not matter: the question is whether the value changed, not by how much.
RECALC_GENERATION_CACHEKEY: t.Final = "queue-positions-generation"

# Renumber at least this often even when the flag says nothing changed. Covers the task runner's
# own dispatches (which move the running task to the front) and any flag lost to a cache eviction
# or a wiped cache directory, so a stale ordering can never persist.
RECALC_MAX_INTERVAL_SECONDS: t.Final = 30.0

# How often the runner asks whether a renumbering was requested. Its loop runs twice a second, and
# in production this flag is a file in the cache directory, so consulting it every pass was an open
# and an unpickle twice a second to answer "no" almost every time.
#
# The added delay is not visible: a submitted task is given a provisional position at the back of
# the queue before the request returns (see next_queuepos_relative), so what waits is only the
# round-robin reordering among users.
RECALC_CHECK_INTERVAL_SECONDS: t.Final = 2.0

# How far back typical_runtime_seconds() looks. A day rather than the stats page's week: this
# figure tells a waiting user how long their task will take, so it should follow current conditions
# -- a slow night, a backlog, a change on the remote host -- rather than average them away.
TYPICAL_RUNTIME_WINDOW_HOURS: t.Final = 24

# At most this many recent tasks are read to compute the medians. The window alone is not a bound:
# a busy day can finish tens of thousands of tasks, and the median of the most recent few thousand
# is the same answer for a great deal less memory.
TYPICAL_RUNTIME_SAMPLE_LIMIT: t.Final = 2000

# Below this many samples of a request type, no figure is reported for it and the queue page shows
# no estimate at all. A median of two tasks is not knowledge, and a wrong estimate is worse than
# none: it is the number the user plans their afternoon around.
TYPICAL_RUNTIME_MIN_SAMPLES: t.Final = 5

# The medians move slowly and are read by every open queue page, so they are worth caching. Short
# enough that a queue which has just become slow is reflected within minutes.
TYPICAL_RUNTIME_CACHE_SECONDS: t.Final = 300

TYPICAL_RUNTIME_CACHEKEY: t.Final = "typical_runtime_seconds_v1"

# How long each process keeps the medians in its own memory. The shared cache is a file, and a
# read of it opens, reads and unpickles that file. The status endpoint asks for the medians at
# each poll of each open tab, on every page of the site. Thus that file read became a cost on
# each request. One write interval of the task runner is short, and the medians change each 300
# seconds at most.
TYPICAL_RUNTIME_MEMO_SECONDS: t.Final = 15.0

# The memo of this process: the time at which it becomes too old, and the medians. Tests that
# clear the shared cache must also set this back to None; see clear_typical_runtime_memo.
_typical_runtime_memo: tuple[float, dict[str, float]] | None = None


def request_recalc() -> None:
    """Ask the task runner to renumber the queue.

    Cheaper than renumbering here: calculate_queue_positions() locks every queued row, so calling
    it inline made a submission wait for the whole queue and serialised concurrent submitters
    behind one another. The runner already polls this table twice a second, and it is the only
    process that changes which task is running, so it is the natural owner of the ordering.
    """
    cache = caches["default"]
    # Not incr(): it rewrites the key with the cache's default timeout, so the counter set here to
    # never expire started expiring after the first increment. The runner then read 0 after five
    # quiet minutes, and a request that re-created the key at 1 could equal the value it had last
    # recorded and be dropped. Two callers can race here and both write the same value, which
    # does not matter: the runner watches for a change, it does not count submissions.
    cache.set(RECALC_GENERATION_CACHEKEY, int(cache.get(RECALC_GENERATION_CACHEKEY) or 0) + 1, timeout=None)


def recalc_generation() -> int:
    """Return the current request counter, for a caller to compare against the one it last saw.

    Read before renumbering and remembered afterwards, so a request that lands *during* a
    renumbering leaves a value the next pass still sees as new, rather than being swallowed.

    A cleared cache restarts the count, which reads as a change and costs one extra renumbering.
    """
    return int(caches["default"].get(RECALC_GENERATION_CACHEKEY) or 0)


def next_queuepos_relative() -> int:
    """Return a queue position at the back of the current queue.

    Not only cosmetic. The task runner dispatches in `order_by("queuepos_relative")`, and NULL
    sorts first, so a task left unnumbered between two renumberings would be picked up ahead of
    everything already waiting. Renumbering used to happen inside the submitting request, so the
    window did not exist; now it does, and this closes it.
    """
    maxpos = Task.queued().aggregate(models.Max("queuepos_relative"))["queuepos_relative__max"]

    return 0 if maxpos is None else maxpos + 1


def typical_runtime_seconds() -> dict[str, float]:
    """Return the median recent run time, in seconds, of each request type that has enough samples.

    This is what the queue page multiplies by the number of tasks ahead to estimate a wait, so it
    lives here beside the position numbering it is read with rather than in the web layer: it takes
    no request, answers a question about how fast the queue drains, and the runner can import this
    module without pulling in DRF and the rest of the web stack (see the note at the top of it).

    Keyed by request type because they are not comparable: an IMGZIP request retrieves up to a
    thousand images where an FP task fits one light curve, so a single figure across both would
    over-promise for one and under-promise for the other.

    The median, not the mean. TASK_MAXTIME_SECONDS lets a task run for four hours before it is
    killed, and a mean has no defence against that -- one stuck task would add minutes to the
    estimate shown to every waiting user for the rest of the day. The stats page does use a mean,
    which is right for the load factor it feeds (total occupancy genuinely includes the four hours)
    and wrong for a per-task promise.

    Failed tasks are counted. They usually fail quickly, which pulls the figure down, but what the
    estimate needs to know is how fast the queue drains -- and a task that errors frees its slot
    just as surely as one that succeeds.
    """
    global _typical_runtime_memo  # noqa: PLW0603

    if _typical_runtime_memo is not None and time.monotonic() < _typical_runtime_memo[0]:
        return _typical_runtime_memo[1]

    cached = caches["usagestats"].get(TYPICAL_RUNTIME_CACHEKEY, default=None)
    if cached is not None:
        _typical_runtime_memo = (time.monotonic() + TYPICAL_RUNTIME_MEMO_SECONDS, cached)
        return cached

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=TYPICAL_RUNTIME_WINDOW_HOURS)

    medians = {}
    for request_type in Task.RequestType.values:
        # One query per type, each with its own sample limit, rather than one shared limit applied
        # before grouping. Under a shared limit a busy day of one type fills the whole sample and
        # crowds the others out: a type with plenty of completions in the window still falls under
        # the minimum and gets no figure -- and the long-running types, where a wait estimate is
        # worth the most, are the low-volume ones.
        #
        # Windowed and ordered on finishtimestamp, which is what is being measured. On submission
        # time a queue backlogged for longer than the window excludes every task completing now,
        # so the estimate would disappear exactly during the backlog it exists to describe.
        # task_maint_idx is (request_type, finishtimestamp), so this pair is index-covered.
        #
        # Both timestamp columns are nullable on the model and so are typed as optional however the
        # queryset was filtered; the isnull=False pair is what actually guarantees them. The cast
        # says so once, next to the filter that earns it.
        recent = t.cast(
            "Iterable[tuple[datetime.datetime, datetime.datetime]]",
            Task.objects.filter(request_type=request_type, finishtimestamp__gt=cutoff, starttimestamp__isnull=False)
            .order_by("-finishtimestamp")
            .values_list("starttimestamp", "finishtimestamp")[:TYPICAL_RUNTIME_SAMPLE_LIMIT],
        )

        # the same quantity as Task.runtime(), computed from two columns rather than from a model
        # instance: this reads thousands of rows and does not need them built into objects
        runtimes = [(finished - started).total_seconds() for started, finished in recent]

        if len(runtimes) >= TYPICAL_RUNTIME_MIN_SAMPLES:
            medians[request_type] = round(statistics.median(runtimes), 1)

    caches["usagestats"].set(TYPICAL_RUNTIME_CACHEKEY, medians, timeout=TYPICAL_RUNTIME_CACHE_SECONDS)
    _typical_runtime_memo = (time.monotonic() + TYPICAL_RUNTIME_MEMO_SECONDS, medians)

    return medians


def clear_typical_runtime_memo() -> None:
    """Discard the medians that this process holds in its memory.

    For the tests. A test that clears the usagestats cache expects the next call to compute new
    medians, and the memo would give it the medians of the test before it.
    """
    global _typical_runtime_memo  # noqa: PLW0603
    _typical_runtime_memo = None


def calculate_queue_positions() -> None:
    """Assign every queued task its position, which is the order the task runner executes them in."""
    with transaction.atomic():
        # Lock the queued rows and read them once. Without the lock, two concurrent
        # recalculations can each renumber from a snapshot that is missing the other's changes
        # and end up assigning duplicate queue positions.
        queuedtasks = list(Task.queued().select_for_update().order_by("user_id", "timestamp", "id"))

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

        # (task id, user id) pairs: one list, so that the two cannot drift apart
        unassigned = [(t.id, t.user_id) for t in queuedtasks]

        # work through passes (max one task per user in each pass) assigning queue positions from 0 (next) upwards
        queuepos: int = 0
        passnum: int = 0
        # collected and written in one statement at the end: issuing an UPDATE per task meant a
        # round trip per task while holding a lock on every queued row, so a deep queue made every
        # submission slow and serialised concurrent submitters behind it
        queuepos_updates: dict[int, int] = {}
        while unassigned:
            useridsassigned_currentpass = set()

            if passnum == 0 and runningtaskid is not None:
                # currently running task will be assigned position 0
                if any(taskid == runningtaskid for taskid, _userid in unassigned):
                    unassigned = [(taskid, userid) for taskid, userid in unassigned if taskid != runningtaskid]
                    useridsassigned_currentpass.add(runningtask_userid)
                    queuepos_updates[runningtaskid] = 0
                    queuepos = 1
                else:
                    # the task disappeared between the two queries?
                    runningtaskid = None

            # collect the tasks not assigned in this pass rather than popping from
            # the list during iteration (which would skip elements)
            remaining: list[tuple[int, int]] = []
            for taskid, task_userid in unassigned:
                if task_userid not in useridsassigned_currentpass and (
                    passnum != 0 or runningtask_userid is None or (task_userid > runningtask_userid)
                ):
                    queuepos_updates[taskid] = queuepos
                    useridsassigned_currentpass.add(task_userid)
                    queuepos += 1
                else:
                    remaining.append((taskid, task_userid))

            unassigned = remaining

            # bail out rather than spin forever if a pass somehow assigns nothing. Not an assert:
            # those are stripped under python -O, which is exactly when a hung request would be
            # hardest to explain
            if passnum >= (2 * queuedtaskcount + 1):
                msg = f"queue position assignment made no progress after {passnum} passes over {queuedtaskcount} tasks"
                raise RuntimeError(msg)

            passnum += 1

        # Only the rows that actually move. This used to write every queued row every time, which
        # was harmless when it ran on submit or delete, but the task runner now calls it on a
        # 30-second backstop: an unchanged queue was rewriting every row, and with it every row's
        # task_modified_datetime -- which get_tasklist_etag() aggregates, so every user with a
        # queued task had their ETag invalidated twice a minute and every open queue page was
        # pushed from a cheap 304 into a full serialisation on its next poll.
        currentpositions = {tsk.id: tsk.queuepos_relative for tsk in queuedtasks}
        moved = {taskid: newpos for taskid, newpos in queuepos_updates.items() if currentpositions[taskid] != newpos}

        if moved:
            # task_modified_datetime is written explicitly: it is an auto_now field, and auto_now
            # is applied by Model.save(), not by a bulk write. Without it a reordering would be
            # invisible to get_tasklist_etag() and a user could be served a stale queue position.
            now = datetime.datetime.now(datetime.UTC)
            Task.objects.bulk_update(
                [
                    Task(id=taskid, queuepos_relative=newpos, task_modified_datetime=now)
                    for taskid, newpos in moved.items()
                ],
                ["queuepos_relative", "task_modified_datetime"],
            )
