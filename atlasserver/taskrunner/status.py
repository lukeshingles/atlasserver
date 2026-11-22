"""The task runner's configuration that the web app also needs.

Where the runner writes its status snapshot, how often, and how many tasks it runs at once.

Split out of `main` so that the web server can read these without importing the runner. Importing
`atlasserver.taskrunner.main` runs `django.setup()` and imports multiprocessing. Each mod_wsgi
worker would then load all of that and keep it, only to read these few values.

Anything the two processes must agree on belongs here rather than in `main`, which the web app
cannot reach: a value defined in both places can drift, and NUMSLOTS below is one the queue page
and the runner's own pool size have to derive from the same number.
"""

import datetime as dt
import json
import typing as t
from pathlib import Path

# the runner writes into its own log directory, which the web server process can read but does not
# serve, so the status file is not reachable as a static asset
LOG_DIR: Path = Path(__file__).resolve().parent / "logs"

STATUS_PATH: Path = LOG_DIR / "taskrunner_status.json"

# how often the runner refreshes the status file read by the /taskrunnerstatus.json endpoint
STATUS_WRITE_SECONDS: float = 15.0

# How old the snapshot has to be before the runner counts as gone. Some missed writes, and not
# one, so that a single slow write does not raise a false alarm.
STALE_AFTER_SECONDS: float = STATUS_WRITE_SECONDS * 4

# How many tasks the runner executes at once. The runner sizes its process pool from this and
# reports it in the status file; the web app needs the same number to say how fast the queue drains
# (the load factor on the stats page, and the wait estimate on the queue page).
NUMSLOTS: int = 24

# The maximum number of slots that the tasks of one user can have while a task of a different
# user can start; see taskrunner.main.next_task. When no other task can start, a user gets more.
#
# The queue order gives the next slot to the user with the least usage, and it counts each task
# in a slot as one expected run time. A user with much less usage than the other users thus gets
# slot after slot, until that count is equal to the difference. Without a maximum, such a user
# with a large batch gets all the slots. The maximum also applies to a member of the high
# priority group.
MAX_USER_SLOTS: int = 8


def read_status() -> tuple[dict[str, t.Any], float]:
    """Return the snapshot the runner last wrote, and how many seconds ago it wrote it.

    Raises when there is no usable snapshot: OSError when the file is missing or cannot be read,
    and KeyError, TypeError or ValueError when what it holds is not a status this version wrote.
    Every caller is reporting on the runner rather than serving the file, so each one treats all of
    those the same way -- as a runner that is not reporting.

    Both the endpoint and the page render read the runner's state, and they must not be able to
    disagree about it: one saying the runner is down while the other draws the box as though it
    were up is the visible fault. Thus the read and the threshold below live here, next to the
    interval they are derived from, rather than once in each caller.
    """
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    # this doubles as the check that the payload is an object at all: subscripting a JSON list,
    # string or number raises TypeError, which every caller catches with the rest
    written = dt.datetime.fromisoformat(status["written"])
    return status, (dt.datetime.now(dt.UTC) - written).total_seconds()


def runner_is_stale() -> bool:
    """Whether the task runner has stopped refreshing its snapshot.

    True when there is no usable snapshot at all, because a runner that never wrote one is not
    running either. This is the whole of what a page render needs to colour the site notice box
    before the browser paints it, and it costs one read of a small file: no query, no per-reader
    count, and none of the medians the endpoint answers with.
    """
    try:
        _status, age_seconds = read_status()
    except (OSError, KeyError, TypeError, ValueError):
        return True

    return age_seconds > STALE_AFTER_SECONDS
