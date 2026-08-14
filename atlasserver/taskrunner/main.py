#!/usr/bin/env python3
"""Task runner for forced photometry jobs that are dispatched to ATLAS sc01 over ssh."""

import contextlib
import datetime
import json
import multiprocessing as mp
import os
import smtplib
import subprocess
import time
import typing as t
from pathlib import Path
from signal import SIGINT
from signal import signal
from signal import SIGTERM

import django
import pandas as pd
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.db import models
from django.forms.models import model_to_dict

from atlasserver import settings
from atlasserver.forcephot.misc import datetime_to_mjd
from atlasserver.taskrunner import status as runnerstatus

REMOTE_SERVER = "atlas"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "atlasserver.settings")

django.setup()

import sys

from atlasserver.forcephot import queue as taskqueue
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.webhooks import send_task_callback

TASK_MAXTIME_SECONDS: int = 4 * 3600

# how often a running task asks the database whether it has been cancelled
CANCEL_CHECK_SECONDS: float = 15.0

# how long the main loop waits between polls of the queue. The longer interval applies once the
# queue has been seen to be empty, so that an idle server is not querying twice a second all night.
POLL_INTERVAL_SECONDS: float = 0.5
IDLE_POLL_INTERVAL_SECONDS: float = 5.0

# how many tasks the maintenance sweep loads, cleans up and writes back at a time
MAINTENANCE_BATCH_SIZE: int = 500

# LOG_DIR, STATUS_PATH and STATUS_WRITE_SECONDS live in atlasserver.taskrunner.status so that the
# web server can read the status file without importing this module. All three are referenced
# through that module at each use rather than bound by name here, so that one
# mock.patch.object(runnerstatus, ...) is seen by the runner and the web view alike.


def mjdnow() -> float:
    """Return the current MJD."""
    return datetime_to_mjd(datetime.datetime.now(datetime.UTC))


def localresultfileprefix(taskid: int) -> str:
    """Return the absolute path to the job file (with no extension) for a given task id."""
    return str(Path(settings.RESULTS_DIR / f"job{taskid:05d}"))


def log_general(msg: str, suffix: str = "", *args, **kwargs) -> None:
    """Log a message to the console and to a log file (with given filename suffix)."""
    dtnow = datetime.datetime.now(datetime.UTC)
    strtime = dtnow.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{strtime}  {msg}"
    if not suffix:
        print(line, *args, **kwargs)

    logfile_latest = Path(runnerstatus.LOG_DIR, f"fprunnerlog_latest{suffix}.txt")

    # archive the latest log when its last write was on an earlier UTC day. The check is
    # mtime-based so that the short-lived worker processes also rotate their slot logs
    openmode = "a"
    if logfile_latest.exists():
        lastwrite = datetime.datetime.fromtimestamp(logfile_latest.stat().st_mtime, tz=datetime.UTC)
        if lastwrite.date() < dtnow.date():
            import shutil

            logfile_archive = Path(
                runnerstatus.LOG_DIR,
                f"fprunnerlog_{lastwrite.year:4d}-{lastwrite.month:02d}-{lastwrite.day:02d}{suffix}.txt",
            )
            shutil.copyfile(logfile_latest, logfile_archive)
            openmode = "w"

    with logfile_latest.open(openmode) as flogfile:
        flogfile.write(line + "\n")


def run_rsync(copycommand: list[str], logfunc: t.Callable[[t.Any], None]) -> int | None:
    """Run an rsync command, logging its output. Return the exit code, or None if it timed out."""
    logfunc(" ".join(copycommand))

    proc = subprocess.Popen(
        copycommand,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        bufsize=1,
        universal_newlines=True,
    )

    try:
        # without a timeout an rsync against a wedged host blocks the worker slot indefinitely
        stdout, stderr = proc.communicate(timeout=TASK_MAXTIME_SECONDS)
    except subprocess.TimeoutExpired:
        logfunc(f"ERROR: rsync timed out after {TASK_MAXTIME_SECONDS:.0f} seconds")
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)  # reap the process to avoid leaving a zombie
        return None

    if stdout:
        for line in stdout.split("\n"):
            logfunc(f"STDOUT: {line}")

    if stderr:
        for line in stderr.split("\n"):
            logfunc(f"STDERR: {line}")

    if proc.returncode != 0:
        logfunc(f"ERROR: rsync exited with code {proc.returncode}")

    return proc.returncode


def task_exists(taskid: int) -> bool:
    """Return true if the task exists in the database."""
    try:
        Task.objects.all().get(id=taskid)
    except (ObjectDoesNotExist, IndexError):
        return False

    return True


def remove_task_resultfiles(
    taskid: int,
    parent_task_id: int | None = None,
    request_type: str | None = None,
    logfunc: t.Callable[[t.Any], None] = log_general,
) -> None:
    """Delete any associated result files from a deleted task."""
    if request_type == "FP":
        # an FP task also produces a .jpg preview (and possibly a .pdf plot). The database row is
        # already gone by the time this runs, so anything left behind is orphaned forever.
        # glob patterns must be relative to the globbed directory
        taskfiles = list(Path(settings.RESULTS_DIR).glob(pattern=f"job{taskid:05d}.*"))
    elif request_type == "IMGZIP" and parent_task_id is not None:
        taskfiles = [Path(settings.RESULTS_DIR, localresultfileprefix(parent_task_id) + ".zip")]
    else:  # SSOSTACK
        # glob patterns must be relative to the globbed directory
        taskfiles = list(Path(settings.RESULTS_DIR).glob(pattern=f"job{taskid:05d}.*"))

    for taskfile in taskfiles:
        if Path(taskfile).exists():
            try:
                Path(taskfile).unlink(missing_ok=True)
            except OSError:
                logfunc(f"Error deleting file: {Path(taskfile).relative_to(settings.RESULTS_DIR)}")
            else:
                logfunc(f"Deleted {Path(taskfile).relative_to(settings.RESULTS_DIR)}")


def remote_result_filename(task) -> str | None:
    """Return the name of the one file whose absence means the task failed, or None for unknown types.

    A task can produce several files, but this is the one that has to exist.
    """
    if task.request_type == "FP":
        return f"job{task.id:05d}.txt"
    if task.request_type == "IMGZIP":
        return f"job{task.parent_task_id:05d}.zip"
    if task.request_type == "SSOSTACK":
        return f"job{task.id:05d}.fits"

    return None


def build_fp_command(task, remoteresultfile: Path) -> str:
    """Return the remote shell command for a forced photometry task."""
    # falsy exactly when there is no MPC target: task_mpc_name_not_blank keeps whitespace out of
    # the column, so this needs no normalising of its own
    if task.mpc_name:
        atlascommand = f"/atlas/bin/ssforce.sh '{task.mpc_name}'"
    else:
        atlascommand = f"/atlas/bin/force.sh {float(task.ra)} {float(task.dec)}"

    if task.use_reduced:
        atlascommand += " red=1"

    if task.radec_epoch_year:
        atlascommand += f" epoch={task.radec_epoch_year}"

    if task.propermotion_ra:
        atlascommand += f" pmra={task.propermotion_ra}"

    if task.propermotion_dec:
        atlascommand += f" pmdec={task.propermotion_dec}"

    # "is not None" rather than truthiness, so that an explicit bound of 0 is passed through
    # instead of being silently dropped
    if task.mjd_min is not None:
        atlascommand += f" m0={float(task.mjd_min)}"
    if task.mjd_max is not None:
        atlascommand += f" m1={float(task.mjd_max)}"

    atlascommand += " dodb=1 parallel=4"

    # 2026-06-03 KWS Temporary hack. Only specified users can request TDO data.
    if task.user_id in settings.TEST_USERS:
        atlascommand += " tdo=1"

    atlascommand += " | sort -n"
    atlascommand += f" | tee {remoteresultfile}; "
    atlascommand += f"~/atlas_gettaskimage.py {remoteresultfile}"
    atlascommand += " red" if task.use_reduced else " diff"

    return atlascommand


def build_ssostack_command(task, remoteresultfile: Path, remotedatafile: Path, remotetaskdir: Path) -> str:
    """Return the remote shell command for a solar system object image stack task."""
    atlascommand = f"/atlas/bin/stack_rock.sh '{task.mpc_name}'"
    atlascommand += (  # stack_rock.sh doesn't support float mjds
        f" {float(task.mjd_min) if task.mjd_min else 0:.0f}"
    )
    atlascommand += f" {float(task.mjd_max) if task.mjd_max else mjdnow():.0f}"
    atlascommand += f" outdir={remotetaskdir}"
    # 2026-06-03 KWS Need to add tdo to sites. The whole command is passed to ssh as a single
    # argument and evaluated by one remote shell, so plain quotes are what's needed here:
    # escaped quotes end up as literal characters and the site list is split into five arguments.
    if task.user_id in settings.TEST_USERS:
        atlascommand += " sites='hko mlo chl sth tdo'"
    atlascommand += f" | tee {remotedatafile}; "
    atlascommand += f" mv {remotetaskdir}/*.fits {remoteresultfile}; "
    atlascommand += f"~/atlas_gettaskimage_ssostack.py {remoteresultfile}; "
    atlascommand += f" rm -rf {remotetaskdir}"

    return atlascommand


def runtask(task, logfunc, **kwargs) -> tuple[Path | None, str | None]:
    """Run the forced photometry on atlas sc01 and retrieve the result.

    returns (resultfilename, error_msg)
     - resultfilename will be None if it could not be created due to an error
     - error_msg is None unless there was an error that would make retries pointless (e.g. invalid object name)
    """
    filename = remote_result_filename(task)
    if filename is None:
        return None, None

    remoteresultdir = Path("~/atlasserver/results/")
    remotetaskdir = remoteresultdir / f"task{task.id:05d}"
    remoteresultfile = Path(remoteresultdir, filename)

    localresultfile = Path(settings.RESULTS_DIR, filename)
    settings.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    atlascommand = f"nice -n 19 timeout {TASK_MAXTIME_SECONDS:.1f}s "
    if task.request_type == "FP":
        atlascommand += build_fp_command(task, remoteresultfile=remoteresultfile)

    elif task.request_type == "IMGZIP":
        localdatafile = Path(settings.RESULTS_DIR, f"job{task.parent_task_id:05d}.txt")
        remotedatafile = Path(remoteresultdir, f"job{task.parent_task_id:05d}.txt")

        if not localdatafile.exists():
            # the parent forced photometry data file lists the images to fetch. Without it the task
            # can never succeed, so finish with an error instead of retrying it forever.
            return None, "The forced photometry data file for the parent task is no longer available."

        # copy out the FP data file first, so that it's available on sc01 for the image gathering script
        if run_rsync(["rsync", str(localdatafile), f"{REMOTE_SERVER}:{remotedatafile}"], logfunc) != 0:
            # without the data file the remote script cannot select any images, so retry later
            # instead of burning a remote run that is certain to fail
            return None, None

        atlascommand += f"~/atlas_gettaskimages.py {remotedatafile}"
        atlascommand += " red" if task.use_reduced else " diff"

    elif task.request_type == "SSOSTACK":
        remotedatafile = Path(remoteresultdir, f"job{task.id:05d}.txt")
        atlascommand += build_ssostack_command(
            task,
            remoteresultfile=remoteresultfile,
            remotedatafile=remotedatafile,
            remotetaskdir=remotetaskdir,
        )

    logfunc(f"Executing on {REMOTE_SERVER}: {atlascommand}")

    proc = subprocess.Popen(
        ["ssh", f"{REMOTE_SERVER}", atlascommand],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        bufsize=1,
        universal_newlines=True,
    )

    starttime = time.perf_counter()
    lastlogtime = 0.0
    lastcancelcheck = 0.0
    cancelled = False
    timed_out = False
    while not cancelled and not timed_out:
        try:
            proc.communicate(timeout=1)

        except subprocess.TimeoutExpired:
            now = time.perf_counter()
            # the cancellation check is a database query, and the 1s communicate() timeout used to
            # run one per second per slot. Across 16 slots that was ~16 queries/sec forever, and a
            # single job at the 4 hour limit issued over ten thousand of them. Cancelling a job
            # that has already been running for minutes is not more urgent than this.
            if (now - lastcancelcheck) >= CANCEL_CHECK_SECONDS:
                lastcancelcheck = now
                cancelled = not task_exists(taskid=task.id)
            timed_out = (now - starttime) >= (
                TASK_MAXTIME_SECONDS + 30
            )  # give a little extra time for cleanup after timeout
            if (now - lastlogtime) >= 10:
                logfunc(f"ssh has been running for {now - starttime:.0f} seconds        ")
                lastlogtime = now
        else:
            break

    if cancelled or timed_out:
        if timed_out:
            logfunc(f"ERROR: ssh was killed after exceeding TASKMAXTIME limit of {TASK_MAXTIME_SECONDS:.0f} seconds")
        os.kill(proc.pid, SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)  # reap the process to avoid leaving a zombie
        return None, None  # don't finish with an error message, because we'll retry it later

    stdout, stderr = proc.communicate()
    logfunc(f"ssh finished after running for {time.perf_counter() - starttime:.1f} seconds")

    if stdout:
        stdoutlines = stdout.split("\n")
        logfunc(f"{REMOTE_SERVER} STDOUT: ({len(stdoutlines)} lines of output)")

    if stderr:
        for line in stderr.split("\n"):
            logfunc(f"{REMOTE_SERVER} STDERR: {line}")

    if not task_exists(taskid=task.id):  # check if job was cancelled
        return None, None

    # copy files from sc01 to local machine, deleting the remote files after successful copy
    if task.request_type == "FP":
        copycommands = [
            # move the jpg task image from sc01 (deleting the remote file)
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(localresultfile)],
            [
                "rsync",
                "--remove-source-files",
                f"{REMOTE_SERVER}:{Path(remoteresultdir / filename).with_suffix('.jpg')}",
                str(settings.RESULTS_DIR),
            ],
        ]
    elif task.request_type == "SSOSTACK":
        # move the stack *.fits, *.jpg and *.txt data from sc01 (deleting the remote files)
        copycommands = [
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(settings.RESULTS_DIR)],
            [
                "rsync",
                "--remove-source-files",
                f"{REMOTE_SERVER}:{Path(remoteresultfile).with_suffix('.jpg')}",
                str(settings.RESULTS_DIR),
            ],
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remotedatafile}", str(settings.RESULTS_DIR)],
        ]
    else:  # IMGZIP
        # move the image zip from sc01 (deleting the remote file)
        copycommands = [
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(settings.RESULTS_DIR)]
        ]
        # the data file should already be copied, but just in case, copy it again (deleting the remote file)
        copycommands.append(
            [
                "rsync",
                "--remove-source-files",
                f"{REMOTE_SERVER}:{remotedatafile}",
                str(settings.RESULTS_DIR),
            ]
        )

    for copycommand in copycommands:
        run_rsync(copycommand, logfunc)

    # got an error message (probably no observations in time range) and no fits file, but task is completed
    if task.request_type == "SSOSTACK" and (
        not localresultfile.exists() and localresultfile.with_suffix(".txt").exists()
    ):
        # the fallback matters: an empty error message is falsy, and the task would be retried
        # forever instead of being marked as finished
        errortext = localresultfile.with_suffix(".txt").read_text()[:512].strip()
        return localresultfile, errortext or "No image stack was produced."

    if not localresultfile.exists():
        # task failed somehow
        return None, None

    if task.request_type == "FP":
        try:
            dfforcedphot = pd.read_csv(localresultfile, sep=r"\s+", escapechar="#", skipinitialspace=True)

            if dfforcedphot.empty:
                # file is just a header row without data
                return localresultfile, "No data returned"
        except pd.errors.EmptyDataError:
            return localresultfile, "No data returned"
        except pd.errors.ParserError:
            # a ragged file (e.g. a diagnostic line mixed into the output) must not raise out of
            # here, because the task would then never be marked finished and would be retried forever
            logfunc("ERROR: could not parse the result file")
            return localresultfile, "Could not parse the result file"

        # if not task.from_api:
        #     make_pdf_plot(taskid=task.id, taskcomment=task.comment, localresultfile=localresultfile,
        #                   logprefix=logprefix, logfunc=log, separate_process=True)

    return localresultfile, None


def mark_finished(task, error_msg: str | None) -> None:
    """Record that a task has finished, in the database and on the in-memory instance.

    The database write has to be a queryset update rather than a save(): `task` is a copy read when
    the queue was scanned, and saving it would write back every other column as it was then.

    The same values are then applied to the instance, because notify_finished() reports on it. Until
    this was done, every callback said `finishtimestamp: null` and `success: true` no matter what
    had actually happened, because the instance still held the values it was loaded with.
    """
    finishtimestamp = datetime.datetime.now(datetime.UTC).replace(microsecond=0)

    Task.objects.filter(pk=task.id).update(finishtimestamp=finishtimestamp, queuepos_relative=None, error_msg=error_msg)

    task.finishtimestamp = finishtimestamp
    task.queuepos_relative = None
    task.error_msg = error_msg


def notify_finished(task, logfunc) -> None:
    """Tell the submitter that a task finished, by callback and/or email.

    Both are best-effort and neither may raise: the task is already marked finished by the time
    this runs, and an exception here would kill the worker before it could move on.
    """
    if task.callback_url:
        try:
            send_task_callback(task=task, logfunc=logfunc)
        except Exception as ex:  # noqa: BLE001 (a callback is a courtesy; no failure of it
            # may stop the task being marked finished)
            logfunc(f"ERROR: unexpected failure sending callback for task {task.id}: {ex}")

    send_email_if_needed(task=task, logfunc=logfunc)


def send_email_if_needed(task, logfunc) -> None:
    """Send an email to the user if requested and all tasks in the batch are finished."""
    if not task.send_email:
        logfunc(f"Completed successfully ({task.user.username} did not request an email)")
        return

    if task.from_api:
        logfunc("Requested email for an API-originated task (not sending email)")
        return

    if not task.user.email:
        logfunc(f"Tasked completed ok, but user {task.user.username} has no email address!")
        return

    # if we find an unfinished task in the same batch, hold off sending the email.
    # same batch here is defined as being queued by the same user with identical timestamps
    batchtasks_unfinished = 0
    batchtaskcount = 0

    taskdesclist = []
    localresultfilelist = []
    batchtasks = Task.objects.all().filter(user_id=task.user_id, send_email=True, timestamp=task.timestamp)

    for batchtask in batchtasks:
        batchtaskcount += 1
        if not batchtask.finishtimestamp and batchtask.id != task.id:
            batchtasks_unfinished += 1
        else:
            localresultfile = localresultfileprefix(batchtask.id) + ".txt"
            taskurl = f"https://fallingstar-data.com/forcedphot/queue/{batchtask.id}/"
            strtask = (
                f"Task {batchtask.id}: RA {batchtask.ra} Dec {batchtask.dec} "
                f"{'img_reduced' if batchtask.use_reduced else 'img_difference'} "
                f"\n{taskurl}\n"
            )

            if batchtask.comment:
                strtask += " comment: '" + batchtask.comment + "'"

            taskdesclist.append(strtask)
            localresultfilelist.append(localresultfile)

    if batchtasks_unfinished == 0:
        logfunc(f"Sending email to {task.user.email} containing {batchtaskcount} tasks")

        message = EmailMessage(
            subject="ATLAS forced photometry results",
            body=("Your forced photometry results are available for:\n\n" + "\n".join(taskdesclist) + "\n\n"),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[task.user.email],
        )

        attach_size_mb = 0.0

        for localresultfile in localresultfilelist:
            if Path(localresultfile).exists():
                filesize_mb = Path(localresultfile).stat().st_size / 1024.0 / 1024.0
                if (attach_size_mb + filesize_mb) < 22:
                    attach_size_mb += filesize_mb
                    message.attach_file(localresultfile)

        for localresultfile in localresultfilelist:
            pdfpath = Path(localresultfile).with_suffix(".pdf")
            if pdfpath.exists():
                filesize_mb = Path(pdfpath).stat().st_size / 1024.0 / 1024.0
                if (attach_size_mb + filesize_mb) < 22:
                    attach_size_mb += filesize_mb
                    message.attach_file(str(pdfpath))

        try:
            message.send()
        except (OSError, smtplib.SMTPException) as ex:
            # a refused recipient or an unreachable mail server must not propagate: it would kill
            # the worker before the task is marked finished, and the task would be re-run forever
            logfunc(f"ERROR: could not send email to {task.user.email}: {ex}")
    else:
        logfunc(
            f"Waiting to send email until remaining {batchtasks_unfinished} "
            f"of {batchtaskcount} batched tasks are finished."
        )


def write_status(procs_taskids: dict[int, int], numslots: int, maintenance: bool = False) -> None:
    """Write a snapshot of the runner's state for the status endpoint to read.

    Nothing outside the runner could previously tell whether it was alive, how many slots were
    busy, or whether the queue had stalled — the only signal was the log files. The file is written
    atomically (write to a temporary name, then rename) so that a reader never sees a partial one.

    `maintenance` marks a snapshot written while the hourly sweep is blocking the main loop. The
    slot fields are frozen for that whole window (nothing is reaped or dispatched), so without the
    flag the file would confidently describe workers that may have already exited.
    """
    # One pass over the queued set for all three figures, rather than a first(), a count() and a
    # distinct count() each rebuilding the queryset and scanning again -- this runs every
    # STATUS_WRITE_SECONDS for as long as the runner is up, and the scan grows with the queue.
    #
    # distinct users is here because it is what bounds the rate the queue drains at: dispatch below
    # takes at most one task per user at a time, so the effective concurrency is min(numslots,
    # this). A fact about the queue, counted on the runner's own timescale instead of once per
    # request from every open queue page.
    queuestats = Task.queued().aggregate(
        queued_task_count=models.Count("id"),
        distinct_queued_users=models.Count("user_id", distinct=True),
        oldest_queued=models.Min("timestamp"),
    )
    oldest_queued = queuestats["oldest_queued"]

    status = {
        "written": datetime.datetime.now(datetime.UTC).isoformat(),
        "pid": os.getpid(),
        "numslots": numslots,
        "slots_busy": len(procs_taskids),
        "running_taskids": sorted(procs_taskids.values()),
        "maintenance": maintenance,
        "queued_task_count": queuestats["queued_task_count"],
        "distinct_queued_users": queuestats["distinct_queued_users"],
        "oldest_queued_task_time": oldest_queued.isoformat() if oldest_queued is not None else None,
    }

    try:
        statuspath = runnerstatus.STATUS_PATH
        statuspath.parent.mkdir(parents=True, exist_ok=True)
        tmppath = statuspath.with_suffix(".tmp")
        tmppath.write_text(json.dumps(status))
        tmppath.replace(statuspath)
    except OSError as ex:
        # the status file is diagnostic only; failing to write it must never stop the runner
        log_general(f"WARNING: could not write status file: {ex}")


def handler(signal_received, frame):
    """Handle any cleanup here."""
    log_general("SIGINT or CTRL-C detected. Exiting")
    sys.exit(0)


def do_task(task, slotid: int) -> None:
    """Run a task in a particular slot and send a result email if requested."""

    def logfunc_slotonly(x) -> None:
        log_general(f"slot {slotid:2d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

    def logfunc(x) -> None:
        log_general(f"slot {slotid:2d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

        # also log to the main process
        log_general(f"slot {slotid:2d} task {task.id:05d}: {x}")

    Task.objects.all().filter(pk=task.id).update(
        starttimestamp=datetime.datetime.now(datetime.UTC).replace(tzinfo=datetime.UTC, microsecond=0).isoformat()
    )

    logfunc(f"Starting {'API' if task.from_api else 'web'} task for {task.user.username} ({task.user.email}):")
    for key, value in model_to_dict(task).items():
        logfunc_slotonly(f"{key:>17}: {value}")

    runtask_starttime = time.perf_counter()

    localresultfile, error_msg = runtask(task=task, logfunc=logfunc_slotonly)

    if not task_exists(taskid=task.id):  # task was cancelled
        logfunc("Task was cancelled during execution (no longer in database)")

        # in case a result file was created, delete it
        remove_task_resultfiles(
            taskid=task.id, parent_task_id=task.parent_task_id, request_type=task.request_type, logfunc=logfunc
        )
    else:
        runtask_duration = time.perf_counter() - runtask_starttime

        logfunc(f"Task ran for {runtask_duration:.1f} seconds")

        # the task is marked finished before the email is sent, so that a mail failure can never
        # leave the task looking unfinished and get it re-run
        if error_msg:
            # an error occurred and the task should not be retried (e.g. invalid
            # minor planet center object name or no data returned)
            logfunc(f"Error_msg: {error_msg}")

            mark_finished(task=task, error_msg=error_msg)

            notify_finished(task=task, logfunc=logfunc)

        elif localresultfile and localresultfile.exists():
            # the result file is served from disk; nothing parses it into the database
            mark_finished(task=task, error_msg=None)

            notify_finished(task=task, logfunc=logfunc)

        else:
            waittime = 5
            logfunc(f"ERROR: Task was not completed successfully. Waiting {waittime} seconds to slow down retries...")
            time.sleep(waittime)  # in case we're stuck in an error loop, wait a bit before trying again


def remove_old_tasks(
    days_ago: int,
    harddeleterecord: bool = False,
    request_type: str | None = None,
    is_archived: bool | None = None,
    from_api: bool | None = None,
    logfunc=log_general,
    heartbeat: t.Callable[[], None] | None = None,
) -> None:
    """Remove old tasks matching given criteria, optionally deleting their result files.

    `heartbeat` is called after every batch so that a long sweep does not look like a dead runner
    to whatever is watching the status file.
    """
    now = datetime.datetime.now(datetime.UTC)
    filteropts: dict[str, t.Any] = {
        "finishtimestamp__isnull": False,
        "finishtimestamp__lt": now - datetime.timedelta(days=days_ago),
    }

    if request_type is not None:
        filteropts["request_type"] = request_type

    if not harddeleterecord:
        if is_archived:
            msg = "is_archived=True requires harddeleterecord=True (archiving already-archived tasks is a no-op)"
            raise ValueError(msg)
        # exclude tasks that are already soft deleted from soft deletion query
        is_archived = False

    strarchived = ""
    if is_archived is not None:
        filteropts["is_archived"] = is_archived
        strarchived = "archived " if is_archived else "non-archived "

    strfromapi = ""
    if from_api is not None:
        filteropts["from_api"] = from_api
        strfromapi = "API " if from_api else "web"

    matchingtasks = Task.objects.all().filter(**filteropts)

    taskcount = matchingtasks.count()

    strrequesttype = f"{request_type} " if request_type else ""
    strdeletetype = "hard deleted" if harddeleterecord else "archived"
    logfunc(
        f"There are {taskcount} {strarchived}{strrequesttype}{strfromapi}tasks that finished more than {days_ago} days"
        f" ago to be {strdeletetype}"
    )

    if taskcount > 0:
        strdeleteaction = (
            "Deleting files and database rows" if harddeleterecord else "Deleting files and marking archived"
        )
        taskid_examples = list(matchingtasks.values_list("id", flat=True)[:10])
        logfunc(f"  {strdeleteaction}... (first few task ids: {taskid_examples})")

        # The sweeps reach back up to 183 days, so the widest of them can match a lot of rows.
        # Take one bounded batch at a time rather than reading every matching id into memory, and
        # do the database write once per batch rather than once per task: the old loop issued a
        # full-row save() for every archived task and an UPDATE-then-DELETE for every hard-deleted
        # one. select_related and the prefetch cover the lookups that delete_result_files() makes,
        # so those cost one query per batch instead of one per task.
        #
        # No offset is needed: processing a task removes it from this queryset, because it is
        # either deleted or marked archived (and the non-archived case filters on is_archived
        # above). The pass limit is a safety net so that a write which somehow left a row matching
        # could not spin here forever.
        maxpasses = taskcount // MAINTENANCE_BATCH_SIZE + 2
        for _ in range(maxpasses):
            tasks = list(
                matchingtasks.select_related("parent_task").prefetch_related(Task.prefetch_imagerequests())[
                    :MAINTENANCE_BATCH_SIZE
                ]
            )
            if not tasks:
                break

            taskids = [task.id for task in tasks]

            for taskindex, task in enumerate(tasks):
                task.delete_result_files()
                # inside the batch as well as after it: 500 tasks times several filesystem operations
                # each can alone outlast the staleness window on a slow results mount. The
                # heartbeat rate-limits itself, so calling it often costs almost nothing.
                if heartbeat is not None and taskindex % 50 == 49:
                    heartbeat()

            if harddeleterecord:
                # the rows are about to go, so there is no point marking them archived first
                Task.objects.filter(id__in=taskids).delete()
            else:
                Task.objects.filter(id__in=taskids).update(is_archived=True, task_modified_datetime=now)

            for task in tasks:
                task.forget_derived_cache()

            if heartbeat is not None:
                heartbeat()
        else:
            logfunc(f"  WARNING: stopped after {maxpasses} batches with tasks still matching")

        logfunc("  Done.")


def do_maintenance(heartbeat: t.Callable[[], None] | None = None):
    """Remove old tasks and associated files according to their type and age.

    `heartbeat` is called between sweeps and periodically within them. This runs in the main loop
    and blocks it, and a long sweep would otherwise leave the status file untouched for minutes,
    which from outside is indistinguishable from a dead runner: the queue page would tell every
    user that their tasks are not being processed while all sixteen slots were in fact busy.
    """
    # no maxtime parameter: one used to be accepted and silently ignored (its only reference was a
    # commented-out call), which read as a five-minute bound on the sweep that did not exist

    def logfunc(x):
        log_general(f"Maintenance: {x}")

    sweeps: list[dict[str, t.Any]] = [
        {"days_ago": 14, "harddeleterecord": False, "request_type": "IMGZIP"},
        {"days_ago": 14, "harddeleterecord": False, "request_type": "SSOSTACK"},
        {"days_ago": 183, "harddeleterecord": False, "request_type": "FP"},
        # archived (user-deleted) API tasks: remove the database records and files
        {"days_ago": 7, "harddeleterecord": True, "is_archived": True, "from_api": True},
        # other API tasks
        {"days_ago": 31, "harddeleterecord": True, "from_api": True},
        # Any old tasks
        {"days_ago": 183, "harddeleterecord": True},
    ]

    for sweep in sweeps:
        if heartbeat is not None:
            heartbeat()
        remove_old_tasks(**sweep, logfunc=logfunc, heartbeat=heartbeat)


def main() -> None:
    """Run queued tasks and clean up on old tasks."""
    signal(SIGINT, handler)

    runnerstatus.LOG_DIR.mkdir(parents=True, exist_ok=True)

    def logfunc(x):
        log_general(x)

    logfunc("Starting forcedphot task runner...")
    mp.set_start_method("spawn")
    numslots: int = runnerstatus.NUMSLOTS
    procs: list[mp.Process | None] = [None for _ in range(numslots)]
    procs_userids: dict[int, int] = {}  # user_id of currently running job, or None
    procs_taskids: dict[int, int] = {}  # tasks_id of currently running job, or None

    last_maintenancetime: float = float("-inf")
    last_statustime: float = float("-inf")
    last_queuerecalctime: float = float("-inf")
    last_queueflagchecktime: float = float("-inf")
    # what the queue-recalc counter read the last time positions were renumbered; see
    # forcephot.queue.recalc_generation. Starts below any real value so the first pass renumbers.
    last_recalc_generation: int = -1
    seen_generation: int = -1
    printedwaiting = False

    def refresh_status(maintenance: bool = False) -> None:
        """Write the status snapshot now, and reset the interval that would have written it."""
        nonlocal last_statustime
        write_status(procs_taskids=procs_taskids, numslots=numslots, maintenance=maintenance)
        last_statustime = time.perf_counter()

    def maintenance_heartbeat() -> None:
        """Keep the status file fresh during the sweep, at the normal cadence.

        Rate-limited so that callers can invoke it as often as they like; marked as maintenance
        because the slot fields are frozen while the sweep blocks the loop, and an unqualified
        snapshot would describe workers that may have already exited.
        """
        if (time.perf_counter() - last_statustime) >= runnerstatus.STATUS_WRITE_SECONDS:
            refresh_status(maintenance=True)

    while True:
        # an idle server polled twice a second all night for nothing. Once the queue has been seen
        # to be empty, wait longer between polls; the interval drops back as soon as work appears,
        # so the latency to pick up a new task is unchanged whenever the server is actually busy.
        time.sleep(IDLE_POLL_INTERVAL_SECONDS if printedwaiting else POLL_INTERVAL_SECONDS)

        if (time.perf_counter() - last_maintenancetime) > 60 * 60:  # once per hour
            last_maintenancetime = time.perf_counter()
            # the sweep blocks this loop, so it refreshes the status file itself; without that a
            # sweep lasting longer than STATUS_WRITE_SECONDS * 4 makes the queue page tell every
            # user that their tasks are not being processed while all the slots are still busy
            do_maintenance(heartbeat=maintenance_heartbeat)
            refresh_status()
            printedwaiting = False

        # Renumbering belongs here rather than in the submitting request: this loop is the only
        # thing that changes which task is running, it already reads the table twice a second, and
        # doing it here keeps positions fresh as tasks finish. The counter is a file read in
        # production, so it is consulted on its own interval rather than on every pass.
        recalc_requested = False
        if (time.perf_counter() - last_queueflagchecktime) > taskqueue.RECALC_CHECK_INTERVAL_SECONDS:
            last_queueflagchecktime = time.perf_counter()
            seen_generation = taskqueue.recalc_generation()
            recalc_requested = seen_generation != last_recalc_generation

        if recalc_requested or ((time.perf_counter() - last_queuerecalctime) > taskqueue.RECALC_MAX_INTERVAL_SECONDS):
            # caught, because this loop is what dispatches every job: unguarded, a lock timeout or
            # a bad queue state would take the exception out of main() and stop the runner, and the
            # supervisor would restart it straight back into the same state. Queue positions going
            # stale is a display problem; not dispatching anything is not.
            try:
                taskqueue.calculate_queue_positions()
            except Exception as ex:  # noqa: BLE001 (this loop dispatches every job; stale
                # queue positions are a display problem, not dispatching is not)
                logfunc(f"ERROR: could not update queue positions: {ex}")
            else:
                # only on success, and the value read *before* renumbering: a request that arrived
                # during the pass is still new next time, and a pass that failed leaves its request
                # outstanding so the next check retries it. Recording it either way would drop the
                # request and leave dispatch ordering by stale positions until the backstop.
                last_recalc_generation = seen_generation

            # stamped even on failure, so a persistent one is retried on an interval rather than on
            # every pass of the loop
            last_queuerecalctime = time.perf_counter()

        for slotid, proc in enumerate(procs):
            if proc is not None and proc.exitcode is not None:
                proc.join()
                proc.close()
                procs[slotid] = None
                procs_userids.pop(slotid)
                procs_taskids.pop(slotid)

                numslotsfree = sum(1 if p is None else 0 for p in procs)
                logfunc(f"slot {slotid} is now free. {numslotsfree} of {numslots} slots are available")

        queuedtasks = Task.queued().order_by("queuepos_relative")

        if (time.perf_counter() - last_statustime) >= runnerstatus.STATUS_WRITE_SECONDS:
            refresh_status()

        slotid = procs.index(None) if None in procs else -1

        if slotid < 0:
            # every slot is busy, so there is nothing to dispatch and no reason to look at the queue
            continue

        # one query rather than a count() followed by a first(): the count was only used for a log
        # line, and is fetched below only when a task is actually dispatched
        task = queuedtasks.exclude(user_id__in=list(procs_userids.values())).first()

        if task is None:
            # nothing runnable. That is either an empty queue or a queue holding only tasks from
            # users who already have a job running, and only the former should slow the polling
            if not printedwaiting and not queuedtasks.exists():
                logfunc("Waiting for tasks...")
                printedwaiting = True
            continue

        printedwaiting = False
        logfunc(f"Unfinished tasks in queue: {queuedtasks.count()}")
        logfunc(f"Running task {task.id} in slot {slotid}")
        procs_userids[slotid] = task.user_id
        procs_taskids[slotid] = task.id

        proc = mp.Process(target=do_task, kwargs={"task": task, "slotid": slotid})
        proc.start()
        procs[slotid] = proc


if __name__ == "__main__":
    main()
