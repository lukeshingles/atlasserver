"""The task runner's configuration that the web app also needs.

Where the runner writes its status snapshot, how often, and how many tasks it runs at once.

Split out of `main` so that the web server can read these without importing the runner. Importing
`atlasserver.taskrunner.main` runs `django.setup()` and pulls in pandas and multiprocessing, so the
first request to /taskrunnerstatus.json used to load all of that into every mod_wsgi worker, and
keep it there, to read these few values.

Anything the two processes must agree on belongs here rather than in `main`, which the web app
cannot reach: a value defined in both places can drift, and NUMSLOTS below is one the queue page
and the runner's own pool size have to derive from the same number.
"""

from pathlib import Path

# the runner writes into its own log directory, which the web server process can read but does not
# serve, so the status file is not reachable as a static asset
LOG_DIR: Path = Path(__file__).resolve().parent / "logs"

STATUS_PATH: Path = LOG_DIR / "taskrunner_status.json"

# how often the runner refreshes the status file read by the /taskrunnerstatus.json endpoint
STATUS_WRITE_SECONDS: float = 15.0

# How many tasks the runner executes at once. The runner sizes its process pool from this and
# reports it in the status file; the web app needs the same number to say how fast the queue drains
# (the load factor on the stats page, and the wait estimate on the queue page).
NUMSLOTS: int = 16
