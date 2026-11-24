"""Queue ordering: assigning each unfinished task its position in the execution order.

Lives outside views.py so that the task runner can import it without pulling in DRF, bokeh and the
rest of the web stack, and so that the two processes cannot drift on what "queue position" means.
"""

import collections
import datetime as dt
import itertools
import operator
import statistics
import time
import typing as t

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.db import IntegrityError
from django.db import models
from django.db import transaction

from atlasserver.forcephot.models import Task
from atlasserver.forcephot.models import UsageDay

if t.TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Collection
    from collections.abc import Iterable
    from collections.abc import Mapping

# Bumped by the web app when the queued set changes; the runner watches it and remembers the value
# it last acted on. A counter rather than a flag it clears, because clearing loses a request that
# arrives between the read and the delete. Simultaneous bumps can collapse into one increment,
# which does not matter: the question is whether the value changed, not by how much.
RECALC_GENERATION_CACHEKEY: t.Final = "queue-positions-generation"

# The name of the Django group whose members have priority in the queue. The function
# calculate_queue_positions puts each task of a member before each task of the other users.
# Migration 0010 makes the group, and an administrator changes its members on the page of the
# group in the Django admin.
HIGH_PRIORITY_GROUP: t.Final = "High priority"

# The usage of a user is the run time of the attempts of that user, where older run time counts
# less: run time of this number of days ago counts half, and run time of two times this number of
# days ago counts a quarter. The value is the default half-life of the fair share of Slurm
# (PriorityDecayHalfLife).
USAGE_HALF_LIFE_DAYS: t.Final = 7

# The number of days of usage records that the usage of a user includes, and that the maintenance
# of the task runner keeps. Run time of 5 half-lives ago counts 3 %.
USAGE_RETENTION_DAYS: t.Final = 5 * USAGE_HALF_LIFE_DAYS

# How much the wait of a task decreases its key in the queue order: each second of wait takes
# this number of seconds from the key. This is the age factor of the priority of Slurm. With the
# decay of the usage, a user with much usage never gets to zero, and without this term the tasks
# of such a user can wait for as long as other users have tasks.
#
# The value is low, as the weight of the age is in a Slurm configuration: the fair share must
# decide the order in the usual condition. On the production server in November 2026, the oldest
# tasks of the three users with the most usage had waited for 320 hours, and each of those users
# had 135 hours of run time in 7 days. With 0.25, a wait of 320 hours is 80 hours of usage: such
# a task stays behind the tasks of a user with no usage, and it goes before the tasks that a user
# with more than 55 hours of usage submits now.
WAIT_CREDIT: t.Final = 0.25

# The wait of a task is in its key as its submission time after this time. The key is thus the
# same number at each renumbering, and the order of two tasks does not change as time goes on.
WAIT_EPOCH: t.Final = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

# An account that is younger than this number of days is a new account. The usage of a new
# account in the queue order is not less than the median usage of the users; see
# new_account_usage. Registration is open, and without this rule a user with much usage gets the
# first place in the queue again with each new account.
NEW_ACCOUNT_DAYS: t.Final = USAGE_HALF_LIFE_DAYS

# A user with less finished run time than this number of tasks gets the expected run time of a
# task from the tasks of all users; see expected_task_seconds. The mean of some tasks is not a
# good estimate.
EXPECTED_RUNTIME_MIN_TASKS: t.Final = 5

# The expected run time of a task, in seconds, when the usage records have too few tasks for a
# mean. The value is the mean run time of a task on the production server in November 2026.
DEFAULT_TASK_SECONDS: t.Final = 420.0

# A task whose attempt failed gets no queue position for this number of seconds after the end of
# that attempt, and thus it cannot start. The time is two times longer after each next failed
# attempt, to the maximum. A task that always fails thus does not keep a slot: it has a wait of
# 10 minutes between two attempts.
#
# A failed attempt adds its real run time to the usage of the user, as each attempt does, and
# it adds no other cost. While the remote host is away, each attempt of each user fails. A fixed
# cost for each failed attempt then removes the differences in usage between the users.
RETRY_WAIT_SECONDS: t.Final = 30.0
RETRY_WAIT_MAX_SECONDS: t.Final = 600.0

# Renumber at least this often even when the flag says nothing changed. Covers a task that lost
# its position at a start and did not finish (a task with no position cannot start), and any flag
# lost to a cache eviction or a wiped cache directory, so a stale ordering can never persist.
RECALC_MAX_INTERVAL_SECONDS: t.Final = 30.0

# How often the runner asks whether a renumbering was requested. Its loop runs twice a second, and
# in production this flag is a file in the cache directory, so consulting it every pass was an open
# and an unpickle twice a second to answer "no" almost every time.
#
# The added delay is not visible: a submitted task is given a provisional position at the back of
# the queue before the request returns (see next_queuepos_relative), so what waits is only the
# move of the task to its place in the fair share order.
RECALC_CHECK_INTERVAL_SECONDS: t.Final = 2.0

# The number of rows that one statement of calculate_queue_positions() locks. MariaDB changes an IN
# list of 1000 values or more into a join (in_predicate_conversion_threshold). It can then lock
# the rows in an order that is not the order of the ids.
QUEUE_LOCK_BATCH_SIZE: t.Final = 500

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


def utcnow() -> dt.datetime:
    """Return the time now. A test replaces this function to set the day of the usage."""
    return dt.datetime.now(dt.UTC)


def priority_userids() -> set[int]:
    """Return the ids of the users who are members of the high priority group.

    The result is an empty set when the group does not exist, and then no task has priority.

    This is a query at each call and not a value that the process keeps. An administrator can
    change the members while the task runner runs, and the next renumbering must see the change.
    """
    return set(get_user_model().objects.filter(groups__name=HIGH_PRIORITY_GROUP).values_list("pk", flat=True))


def inactive_userids() -> set[int]:
    """Return the ids of the accounts that are not active.

    An administrator makes an account inactive in the admin site to stop its use of the server.
    An account that waits for the confirmation of its address is also inactive, and it has no
    task. The queue order gives no position to a task of these accounts; see
    calculate_queue_positions.

    This is a query at each call, as for priority_userids: the next renumbering must see a
    change that an administrator makes.
    """
    return set(get_user_model().objects.filter(is_active=False).values_list("pk", flat=True))


class Usage(t.NamedTuple):
    """The usage of one user: the run time and the number of the attempts, with the decay."""

    # the sum of the run times, in seconds
    seconds: float
    # the number of the attempts. The decay makes it a fraction.
    tasks: float


def charge_usage(userid: int, seconds: float, when: dt.datetime | None = None) -> None:
    """Add one attempt with this run time to the usage record of the user for the day of `when`.

    The task runner calls this at the end of each attempt: a finish, an attempt that failed, and
    a task that its user cancelled while it ran. The usage records thus hold all the time that
    the tasks of a user had a slot. They do not change when the user or the maintenance deletes
    the row of a task.

    Two slots can finish tasks of one user at the same time. The update adds in the database, and
    thus no addition is lost.
    """
    day = (utcnow() if when is None else when).astimezone(dt.UTC).date()
    seconds = max(0.0, seconds)
    for _ in range(2):
        updated = UsageDay.objects.filter(user_id=userid, day=day).update(
            seconds=models.F("seconds") + seconds, tasks=models.F("tasks") + 1
        )
        if updated:
            return
        if not get_user_model().objects.filter(pk=userid).exists():
            # The account is deleted, and its usage has no reader. SQLite reports a row for a
            # user who does not exist only at the end of the transaction, and thus the insert
            # below cannot find this condition.
            return
        try:
            # the transaction keeps an error of this insert out of a transaction of the caller
            with transaction.atomic():
                UsageDay.objects.create(user_id=userid, day=day, seconds=seconds, tasks=1)
        except IntegrityError:
            # a different slot made the row between the update and the insert: the second pass
            # updates that row
            continue
        return


def usage_weight(agedays: int) -> float:
    """Return the weight that a usage record of this number of days ago has in the usage of a user.

    The record of today counts fully, and the record of USAGE_HALF_LIFE_DAYS ago counts half. The
    weight changes one time in a day, at the start of the day in UTC. The usage of a user is thus
    constant during a day if no attempt of the user ends, and the queue order does not move
    continuously.
    """
    return 0.5 ** (agedays / USAGE_HALF_LIFE_DAYS)


def decayed_usage(rows: Iterable[tuple[int, dt.date, float, int]], today: dt.date) -> dict[int, Usage]:
    """Return the usage of each user from usage records (user id, day, seconds, tasks); see usage_weight."""
    totals: dict[int, list[float]] = {}
    for userid, day, seconds, tasks in rows:
        weight = usage_weight((today - day).days)
        total = totals.setdefault(userid, [0.0, 0.0])
        total[0] += seconds * weight
        total[1] += tasks * weight
    return {userid: Usage(seconds, tasks) for userid, (seconds, tasks) in totals.items()}


def usage_records() -> models.QuerySet[UsageDay]:
    """Return the usage records that the usage of a user includes."""
    firstday = utcnow().date() - dt.timedelta(days=USAGE_RETENTION_DAYS)
    return UsageDay.objects.filter(day__gte=firstday)


def usage_by_user() -> dict[int, Usage]:
    """Return the usage of each user who has a usage record.

    The table has one row for each user and day, and thus the query reads some thousands of small
    rows. The task runner calls this at each renumbering.
    """
    return decayed_usage(usage_records().values_list("user_id", "day", "seconds", "tasks"), utcnow().date())


def user_usage_seconds(userid: int) -> float:
    """Return the usage of one user, in seconds of run time with the decay."""
    rows = usage_records().filter(user_id=userid).values_list("user_id", "day", "seconds", "tasks")
    usage = decayed_usage(rows, utcnow().date()).get(userid)
    return 0.0 if usage is None else usage.seconds


def remove_old_usage_records() -> int:
    """Delete the usage records that the usage of a user does not include, and return their number."""
    firstday = utcnow().date() - dt.timedelta(days=USAGE_RETENTION_DAYS)
    return UsageDay.objects.filter(day__lt=firstday).delete()[0]


def new_account_userids() -> set[int]:
    """Return the ids of the users whose account is a new account; see NEW_ACCOUNT_DAYS."""
    joinedafter = utcnow() - dt.timedelta(days=NEW_ACCOUNT_DAYS)
    return set(get_user_model().objects.filter(date_joined__gt=joinedafter).values_list("pk", flat=True))


def new_account_usage(usage: Mapping[int, Usage]) -> float:
    """Return the lowest usage that a new account has in the queue order, in seconds.

    This is the median usage of the users who have a usage record. A new account is thus before
    each user with more usage than the median, and it is not before the half of the users with
    the least usage. The scheduler of Linux has a rule of the same type for a new process. That
    process starts with the lowest usage of the current processes, and not with zero.
    """
    return statistics.median(own.seconds for own in usage.values()) if usage else 0.0


def expected_task_seconds(usage: Mapping[int, Usage]) -> Callable[[int], float]:
    """Return the function that gives the expected run time of one task of a user, in seconds.

    The queue order charges a user this time for each task of the user in a slot, and it puts
    this time between two tasks of the user that wait. The mean run time of the
    attempts of the user is the best value: the tasks of one user are frequently of one type, and
    a user with long tasks then gets fewer tasks for the same run time. A user with few attempts
    gets the mean of all users.
    """
    totalseconds = sum(own.seconds for own in usage.values())
    totaltasks = sum(own.tasks for own in usage.values())
    allusers = totalseconds / totaltasks if totaltasks >= EXPECTED_RUNTIME_MIN_TASKS else DEFAULT_TASK_SECONDS

    def expected(userid: int) -> float:
        own = usage.get(userid)
        seconds = own.seconds / own.tasks if own is not None and own.tasks >= EXPECTED_RUNTIME_MIN_TASKS else allusers
        # A time of zero puts all the tasks of a user at one place in the order, and the user
        # then has no turns with the other users.
        return max(1.0, seconds)

    return expected


def retry_wait_seconds(attempts: int) -> float:
    """Return the seconds that a task waits for a new attempt after the end of failed attempt number `attempts`."""
    # the limit of the exponent keeps the power in the range of a float for a task with many attempts
    return min(RETRY_WAIT_MAX_SECONDS, RETRY_WAIT_SECONDS * 2 ** min(attempts - 1, 10))


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

    Not only cosmetic. A task with no position cannot start (taskrunner.main.next_task), and the
    queue page shows no number for it. With this position, a new task can start before the next
    renumbering if the queue is empty, and the page shows a number for it at once.
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

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=TYPICAL_RUNTIME_WINDOW_HOURS)

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
            "Iterable[tuple[dt.datetime, dt.datetime]]",
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


def calculate_queue_positions(
    usage: Mapping[int, Usage] | None = None, running_taskids: Collection[int] | None = None
) -> None:
    """Assign each task that waits its position, which is the order in which the task runner starts the tasks.

    The order is a fair share of the run time, as in the multifactor priority of Slurm: the usage
    of the user decides, and the wait of the task counts also. The tasks of the members of the
    high priority group have the first positions. After that, the task with the lowest key is
    first. The key of a task that waits is in seconds:

        the usage of its user (the run time in the usage records, with the decay)
        + the expected run time of a task, for each task of the user in a slot
        + the expected run time of a task, for each task of the user that waits before it
        - WAIT_CREDIT, for each second that the task waits

    The first three terms are the usage that the user will have at the start of the task. Users
    with equal usage thus take turns, and a user with less usage gets tasks until the usage is
    equal. The usage of a new account is not less than the median usage; see new_account_usage.

    The last term makes sure that each task starts: a task that waits gets a lower key, in
    comparison with each task that a user submits later, until it is before those tasks. The
    submission time decides between equal keys.

    A start of a task does not change a key: the started task becomes a task in a slot, and each
    later task of its user has one task less before it. The order of the tasks that wait is thus the
    same before and after a start, and the task runner can start many tasks between two calls of
    this function. The end of an attempt changes the keys of one user by the difference between
    the real run time and the expected run time. Time does not change the order of two tasks:
    the two waits increase together.

    A task in a slot has no position. A task that has started and is not in a slot waits for a
    new attempt (see taskrunner.main.record_attempt_end). It has no position before the time in its field
    retry_after, which is the end of its failed attempt plus retry_wait_seconds. After that time
    it gets a position as the other tasks do. Thus a task that always fails does not keep a slot.
    The renumbering at an interval of RECALC_MAX_INTERVAL_SECONDS gives the position, and the
    wait is thus longer by that interval at most.

    A task of an inactive account has no position, and thus it cannot start; see
    inactive_userids. The renumbering at the same interval removes the positions after an
    administrator makes an account inactive. The maintenance of the task runner deletes those
    tasks (taskrunner.main.remove_tasks_of_inactive_users).

    The task runner starts the task with the lowest position whose user is below the maximum
    number of slots (taskrunner.main.next_task). The task runner has no second rule for the
    priority. The position of a task is thus the number of tasks that are before it in the
    order, and it is the largest number of tasks that can start before it. Fewer tasks start
    before it when a user with tasks before it has the maximum number of slots: the task then
    starts before the other tasks of that user.

    `usage` is the usage of each user, as usage_by_user returns it. Without it, this function
    reads the usage.

    `running_taskids` is the ids of the tasks in the slots of the task runner. Without it, each
    task that has started is a task in a slot.
    """
    # These queries run before the transaction locks the rows. Thus the lock does not include
    # the time of these queries.
    priorityuserids = priority_userids()
    inactiveuserids = inactive_userids()
    if usage is None:
        usage = usage_by_user()
    expected = expected_task_seconds(usage)
    newaccounts = new_account_userids()
    newaccountseconds = new_account_usage(usage)

    now = utcnow()

    def in_slot(task: Task) -> bool:
        return task.starttimestamp is not None if running_taskids is None else task.id in running_taskids

    def waits_for_retry(task: Task) -> bool:
        return task.retry_after is not None and now < task.retry_after

    # The ids of the queued tasks in ascending order, read without a lock. This read uses
    # task_queue_idx.
    queuedids = sorted(Task.queued().values_list("id", flat=True))

    with transaction.atomic():
        # Lock the queued rows and read them once. Without the lock, two concurrent
        # recalculations can each renumber from a snapshot that is missing the other's changes
        # and end up assigning duplicate queue positions.
        #
        # The lock is on the primary key only, in the order of the ids. Task.delete() locks the
        # row first, and its DELETE then changes the entry of the row in each index. A locking
        # read through Task.queued() takes the locks in the opposite order. It uses
        # task_queue_idx, and InnoDB locks the entry of each row in that index and then the row.
        # Such a read and a delete of a queued task can each hold the lock that the other needs.
        # InnoDB then stops one of them with MySQL error 1213, a deadlock.
        #
        # Each statement locks one batch of the ids; see QUEUE_LOCK_BATCH_SIZE. It selects whole
        # rows, so the database reads the primary key. A statement that selects only the id can
        # read a secondary index, because each entry of such an index contains the id.
        locked: list[Task] = []
        for batch in itertools.batched(queuedids, QUEUE_LOCK_BATCH_SIZE, strict=False):
            locked.extend(Task.objects.select_for_update().filter(id__in=batch).order_by("id"))

        # This read is after the lock, and it has no lock of its own. A task can finish or go
        # between the read of the ids and the lock, and Task.queued() does not return it here. A
        # task submitted in that interval is not in this pass. Its submission requests a
        # recalculation, and it has a provisional position until then.
        stillqueued = set(Task.queued().filter(id__in=queuedids).values_list("id", flat=True))
        queuedtasks = sorted(
            (task for task in locked if task.id in stillqueued),
            key=operator.attrgetter("user_id", "timestamp", "id"),
        )

        # (member of the group or not, key, submission time, id) for each task that waits
        keys: list[tuple[int, float, dt.datetime, int]] = []
        for userid, tasksofuser in itertools.groupby(queuedtasks, key=operator.attrgetter("user_id")):
            usertasks = list(tasksofuser)
            taskseconds = expected(userid)
            own = usage.get(userid)
            usageseconds = 0.0 if own is None else own.seconds
            if userid in newaccounts:
                usageseconds = max(usageseconds, newaccountseconds)
            tier = 0 if userid in priorityuserids else 1
            inslots = sum(1 for task in usertasks if in_slot(task))
            waiting: list[Task] = (
                []
                if userid in inactiveuserids
                else [task for task in usertasks if not in_slot(task) and not waits_for_retry(task)]
            )
            # One multiplication for the tasks in the slots and the tasks before. A start moves
            # one unit from the tasks before to the tasks in the slots, and the product is then
            # the same number. A sum of two products can differ in its last bit, and that
            # difference changes the order of two users with equal usage.
            keys.extend(
                (
                    tier,
                    usageseconds
                    + (inslots + index) * taskseconds
                    + WAIT_CREDIT * (task.timestamp - WAIT_EPOCH).total_seconds(),
                    task.timestamp,
                    task.id,
                )
                for index, task in enumerate(waiting)
            )

        order = [taskid for _, _, _, taskid in sorted(keys)]

        # The positions give an order, and they do not start at a fixed number: a reader
        # subtracts the lowest position (Task.min_queuepos_relative). The first position is thus
        # free, and this code selects the one that keeps the position of the largest number of
        # tasks. After a start, all the tasks that wait keep their positions. After a new task
        # at the front, the tasks behind it keep theirs, and the new task gets a lower number.
        # With positions from 0, each of these events is a write of each row of the queue, and
        # each write makes the ETag of a task list invalid.
        currentpositions = {task.id: task.queuepos_relative for task in queuedtasks}
        firstpositions = collections.Counter(
            position - index for index, taskid in enumerate(order) if (position := currentpositions[taskid]) is not None
        )
        firstposition = firstpositions.most_common(1)[0][0] if firstpositions else 0

        newpositions: dict[int, int | None] = dict.fromkeys(currentpositions, None)
        for index, taskid in enumerate(order):
            newpositions[taskid] = firstposition + index

        # Only the rows that change. The task runner calls this on a 30-second backstop, and a
        # write of an unchanged row changes its task_modified_datetime, which get_tasklist_etag()
        # aggregates. Such a write invalidates the ETag of every user with a queued task twice a
        # minute, and the next poll of each open queue page then pays a full serialisation.
        changed = {
            taskid: position for taskid, position in newpositions.items() if currentpositions[taskid] != position
        }

        if changed:
            # collected and written in one statement: issuing an UPDATE per task meant a round
            # trip per task while holding a lock on every queued row, so a deep queue made every
            # submission slow and serialised concurrent submitters behind it
            #
            # This writes task_modified_datetime by name: Model.save() applies auto_now, and a
            # bulk write does not. Without it get_tasklist_etag() cannot see a reordering, and the
            # server could serve a stale queue position.
            now = dt.datetime.now(dt.UTC)
            Task.objects.bulk_update(
                [
                    Task(id=taskid, queuepos_relative=newpos, task_modified_datetime=now)
                    for taskid, newpos in changed.items()
                ],
                ["queuepos_relative", "task_modified_datetime"],
            )
