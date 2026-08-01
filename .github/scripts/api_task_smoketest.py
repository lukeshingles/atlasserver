#!/usr/bin/env python3
"""Submit a forced photometry task through the API and wait for the result to arrive from sc01.

The unit tests mock out ssh, so nothing else checks that a task submitted by a user reaches ATLAS
sc01, runs there, and comes back as something the user can download. This script is that check, and
it goes through the running web server the way a user's script would (see static/apiexample.py): it
asks for a token, queues one task, polls it until the task runner has finished it, and downloads
the photometry.

The web server and the task runner must both be running, and ATLAS_SMOKETEST_USER must name an
account whose password is in ATLAS_SMOKETEST_PASSWORD.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# where atlaswebserver serves the site off macOS. It mounts the app at settings.PATHPREFIX, which
# follows DEBUG, so both possibilities are tried rather than pinning that rule here as well.
# ATLAS_SMOKETEST_BASEURL replaces the pair when the server is somewhere else entirely.
DEFAULT_BASEURLS: tuple[str, ...] = ("http://127.0.0.1:8086/forcedphot", "http://127.0.0.1:8086")

USERNAME: str = os.environ.get("ATLAS_SMOKETEST_USER", "citest")
PASSWORD: str = os.environ.get("ATLAS_SMOKETEST_PASSWORD", "")

# how long to wait for the result. force.sh takes a couple of minutes for the window requested
# below, but sc01 is shared with the production server and with whatever else is running on it
TIMEOUT_SECONDS: float = 15 * 60
POLL_SECONDS: float = 5.0
PROGRESS_SECONDS: float = 60.0

# apachectl returns as soon as httpd has forked, which is a little before it serves anything
SERVER_START_SECONDS: float = 60.0
REQUEST_TIMEOUT_SECONDS: float = 30.0

# how many days of survey data to request. Long enough that a run of bad weather at one site, or a
# few nights lost to moonlight, cannot leave the result empty
WINDOW_DAYS: float = 30.0


def log(msg: str) -> None:
    """Print a line and flush it.

    CI reads this over a pipe, where the output would otherwise be held until the script exits, and
    a run that has to be killed for taking too long is exactly the one whose progress matters.
    """
    print(msg, flush=True)


def http(url: str, token: str | None = None, data: dict[str, object] | None = None) -> tuple[int, bytes]:
    """Make one request to the server and return its status and body.

    A request with `data` is a form encoded POST, which is what the API guide's examples send.
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"

    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as ex:
        # an error response is an answer from the API, and its body says what was wrong with the
        # request, so hand it back to the caller instead of raising
        return ex.code, ex.read()


def mjd_now() -> float:
    """Return the current MJD. The Unix epoch is MJD 40587."""
    return time.time() / 86400.0 + 40587.0


def antisolar_target() -> tuple[float, float]:
    """Return an (RA, Dec) in degrees that the ATLAS telescopes are observing at this time of year.

    A hardcoded position spends a couple of months a year too close to the Sun to be observed, and
    a request there comes back empty, so this check would fail for weeks at a time for a reason
    that has nothing to do with the code. The point opposite the Sun on the celestial equator rises
    at sunset from every ATLAS site, whatever the date. The Sun's mean ecliptic longitude is used
    here as its right ascension, which is a couple of degrees out and far more precision than is
    needed to stay away from the Sun.
    """
    sun_ra_deg = (280.46 + 0.9856474 * (mjd_now() - 51544.5)) % 360.0

    return round((sun_ra_deg + 180.0) % 360.0, 4), 0.0


def find_server() -> str:
    """Return the base URL that the site answers on, waiting for it to start serving.

    apachectl returns as soon as httpd has forked, which is a little before the app answers, so a
    slow start must not be read as a broken API.
    """
    override = os.environ.get("ATLAS_SMOKETEST_BASEURL", "").rstrip("/")
    candidates = (override,) if override else DEFAULT_BASEURLS
    deadline = time.monotonic() + SERVER_START_SECONDS
    reasons: dict[str, str] = {}

    while True:
        for baseurl in candidates:
            try:
                status, _body = http(f"{baseurl}/robots.txt")
            except urllib.error.URLError as ex:
                reasons[baseurl] = str(ex)
            else:
                if status == 200:
                    log(f"the server is answering at {baseurl}")
                    return baseurl
                reasons[baseurl] = f"HTTP {status}"

        if time.monotonic() > deadline:
            tried = ", ".join(f"{baseurl} ({reason})" for baseurl, reason in reasons.items())
            sys.exit(f"ERROR: no answer from the web server after {SERVER_START_SECONDS:.0f}s. Tried {tried}")

        time.sleep(1.0)


def get_token(baseurl: str) -> str:
    """Exchange the account's password for an API token, as the API guide tells users to."""
    status, body = http(f"{baseurl}/api-token-auth/", data={"username": USERNAME, "password": PASSWORD})
    if status != 200:
        sys.exit(f"ERROR: /api-token-auth/ returned {status}: {body.decode(errors='replace')}")

    return str(json.loads(body)["token"])


def submit_task(baseurl: str, token: str) -> int:
    """Queue one forced photometry task through the API and return its id."""
    ra, dec = antisolar_target()
    mjd_min = round(mjd_now() - WINDOW_DAYS, 5)

    status, body = http(f"{baseurl}/queue/", token=token, data={"ra": ra, "dec": dec, "mjd_min": mjd_min})
    if status != 201:
        sys.exit(f"ERROR: POST to the queue returned {status}: {body.decode(errors='replace')}")

    task = json.loads(body)
    log(f"Queued task {task['id']}: RA {ra} Dec {dec}, {WINDOW_DAYS:.0f} days of data from MJD {mjd_min}")

    return int(task["id"])


def export_task_id(taskid: int) -> None:
    """Publish the task id to the later steps of the GitHub Actions job, which clean up after it.

    Whatever removes the files on sc01 has to know the id, and only the API can say what it is.
    """
    githubenv = os.environ.get("GITHUB_ENV")
    if githubenv:
        with Path(githubenv).open("a", encoding="utf-8") as envfile:
            envfile.write(f"ATLAS_SMOKETEST_TASK_ID={taskid}\n")


def wait_for_result(baseurl: str, token: str, taskid: int) -> dict:
    """Poll the task until the runner marks it finished, or the timeout expires."""
    taskurl = f"{baseurl}/queue/{taskid}/"
    starttime = time.monotonic()
    laststate = ""
    lastprogress = starttime

    while True:
        status, body = http(taskurl, token=token)
        if status != 200:
            sys.exit(f"ERROR: GET {taskurl} returned {status}: {body.decode(errors='replace')}")

        task = json.loads(body)
        elapsed = time.monotonic() - starttime

        if task["finishtimestamp"]:
            state = "finished"
        elif task["starttimestamp"]:
            state = "running"
        else:
            state = f"queued (position {task['queuepos']})"

        if state != laststate or (time.monotonic() - lastprogress) >= PROGRESS_SECONDS:
            log(f"{elapsed:6.0f}s: task {taskid} is {state}")
            laststate = state
            lastprogress = time.monotonic()

        if task["finishtimestamp"]:
            return task

        if elapsed >= TIMEOUT_SECONDS:
            sys.exit(f"ERROR: task {taskid} was still {state} after {TIMEOUT_SECONDS:.0f} seconds")

        time.sleep(POLL_SECONDS)


def check_result(baseurl: str, task: dict) -> None:
    """Fail unless the photometry that sc01 produced can be downloaded again through the server."""
    if task["error_msg"]:
        sys.exit(f"ERROR: task {task['id']} finished with an error: {task['error_msg']}")

    # not the result_url from the response: the deployment sets a fixed Host header, so the API
    # hands out fallingstar-data.com links that do not lead back to the server under test. This is
    # the same file, served by Django rather than by Apache's static alias.
    dataurl = f"{baseurl}/queue/{task['id']}/data.txt"
    status, body = http(dataurl)
    if status != 200:
        sys.exit(f"ERROR: GET {dataurl} returned {status}")

    lines = body.decode(errors="replace").splitlines()
    datalines = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]

    log(f"{dataurl} holds {len(datalines)} rows of photometry. The first lines of it are:")
    for line in lines[:3]:
        log(f"  {line}")

    if not datalines:
        sys.exit(f"ERROR: task {task['id']} came back with no photometry in it")

    # the preview image is made by a separate script on sc01 and its absence does not stop a task
    # from being served, so report it rather than failing the check on it
    log(f"preview image: {'made' if task['previewimage_url'] else 'MISSING'}")


def main() -> None:
    """Queue a task through the API, wait for sc01 to answer it, and check what came back."""
    if not PASSWORD:
        sys.exit("ERROR: ATLAS_SMOKETEST_PASSWORD is not set")

    baseurl = find_server()
    token = get_token(baseurl)

    taskid = submit_task(baseurl, token)
    export_task_id(taskid)

    task = wait_for_result(baseurl, token, taskid)
    check_result(baseurl, task)

    log(f"Task {taskid} completed the round trip through the API and sc01")


if __name__ == "__main__":
    main()
