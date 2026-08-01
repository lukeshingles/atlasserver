"""Where the task runner writes its status snapshot, and how often.

Split out of `main` so that the web server can find the file without importing the runner. Importing
`atlasserver.taskrunner.main` runs `django.setup()` and pulls in pandas and multiprocessing, so the
first request to /taskrunnerstatus.json used to load all of that into every mod_wsgi worker, and
keep it there, to read two constants.
"""

from pathlib import Path

# the runner writes into its own log directory, which the web server process can read but does not
# serve, so the status file is not reachable as a static asset
LOG_DIR: Path = Path(__file__).resolve().parent / "logs"

STATUS_PATH: Path = LOG_DIR / "taskrunner_status.json"

# how often the runner refreshes the status file read by the /taskrunnerstatus.json endpoint
STATUS_WRITE_SECONDS: float = 15.0
