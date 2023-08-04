#!/usr/bin/env python3
"""Task runner for forced photometry jobs that are dispatched to ATLAS sc01 over ssh."""
import contextlib
import datetime
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
from django.forms.models import model_to_dict

from atlasserver import settings
from atlasserver.forcephot.misc import datetime_to_mjd

REMOTE_SERVER = "atlas"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "atlasserver.settings")

# import atlasserver.wsgi
django.setup()

import sys

from atlasserver.forcephot.models import Task

TASKMAXTIME: int = 1200

LOG_DIR: Path = Path(__file__).resolve().parent / "logs"

# so that current log can be archived periodically, keep track
# of the filename with a date, so that it can be created when the date changes
LASTLOGFILEARCHIVED: dict[str, Path] = {}


def mjdnow() -> float:
    """Return the current MJD."""
    return datetime_to_mjd(datetime.datetime.now(datetime.UTC))


def localresultfileprefix(id: int) -> str:
    """Return the absolute path to the job file (with no extension) for a given task id."""
    return str(Path(settings.RESULTS_DIR / f"job{int(id):05d}"))


def log_general(msg: str, suffix: str = "", *args, **kwargs) -> None:
    """Log a message to the console and to a log file (with given filename suffix)."""
    dtnow = datetime.datetime.now(datetime.UTC)
    strtime = dtnow.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{strtime}  {msg}"
    if not suffix:
        print(line, *args, **kwargs)

    logfile_archive = Path(LOG_DIR, f"fprunnerlog_{dtnow.year:4d}-{dtnow.month:02d}-{dtnow.day:02d}{suffix}.txt")
    logfile_latest = Path(LOG_DIR, f"fprunnerlog_latest{suffix}.txt")

    if suffix in LASTLOGFILEARCHIVED and LASTLOGFILEARCHIVED[suffix] and logfile_archive != LASTLOGFILEARCHIVED[suffix]:
        import shutil

        # os.rename(logfile_latest, logfile_archive)
        shutil.copyfile(logfile_latest, LASTLOGFILEARCHIVED[suffix])
        flogfile = logfile_latest.open("w")
    else:
        flogfile = logfile_latest.open("a")

    LASTLOGFILEARCHIVED[suffix] = logfile_archive

    # with logfile_latest.open("a") as flogfile:
    flogfile.write(line + "\n")
    flogfile.close()


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
        taskfiles = [Path(settings.RESULTS_DIR, localresultfileprefix(taskid) + ".txt")]
    elif request_type == "IMGZIP" and parent_task_id is not None:
        taskfiles = [Path(settings.RESULTS_DIR, localresultfileprefix(parent_task_id) + ".zip")]
    else:  # SSOSTACK
        taskfiles = list(Path(settings.RESULTS_DIR).glob(pattern=localresultfileprefix(taskid) + ".*"))

    for taskfile in taskfiles:
        if Path(taskfile).exists():
            try:
                Path(taskfile).unlink(missing_ok=True)
            except OSError:
                logfunc(f"Error deleting file: {Path(taskfile).relative_to(settings.RESULTS_DIR)}")
            else:
                logfunc(f"Deleted {Path(taskfile).relative_to(settings.RESULTS_DIR)}")


def runtask(task, logfunc=None, **kwargs) -> tuple[Path | None, str | None]:
    """Run the forced photometry on atlas sc01 and retrieve the result.

    returns (resultfilename, error_msg)
     - resultfilename will be None if it could not be created due to an error
     - error_msg is None unless there was an error that would make retries pointless (e.g. invalid object name)
    """
    # the task can create multiple files, but if this main one wasn't created, then the task failed
    if task.request_type == "FP":
        filename = f"job{task.id:05d}.txt"
    elif task.request_type == "IMGZIP":
        filename = f"job{task.parent_task_id:05d}.zip"
    elif task.request_type == "SSOSTACK":
        filename = f"job{task.id:05d}.fits"
    else:
        return None, None

    remoteresultdir = Path("~/atlasserver/results/")
    remotetaskdir = remoteresultdir / f"task{task.id:05d}"
    remoteresultfile = Path(remoteresultdir, filename)

    localresultfile = Path(settings.RESULTS_DIR, filename)
    settings.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    atlascommand = "nice -n 19 "
    if task.request_type == "FP":
        if task.mpc_name:
            atlascommand += f"/atlas/bin/ssforce.sh '{task.mpc_name}'"
        else:
            atlascommand += f"/atlas/bin/force.sh {float(task.ra)} {float(task.dec)}"

        if task.use_reduced:
            atlascommand += " red=1"

        if task.radec_epoch_year:
            atlascommand += f" epoch={task.radec_epoch_year}"

        if task.propermotion_ra:
            atlascommand += f" pmra={task.propermotion_ra}"

        if task.propermotion_dec:
            atlascommand += f" pmdec={task.propermotion_dec}"

        if task.mjd_min:
            atlascommand += f" m0={float(task.mjd_min)}"
        if task.mjd_max:
            atlascommand += f" m1={float(task.mjd_max)}"

        atlascommand += " dodb=1 parallel=8"

        # for debugging because force.sh takes a long time to run
        # atlascommand = "echo '(DEBUG MODE: force.sh output will be here)'"

        atlascommand += " | sort -n"
        atlascommand += f" | tee {remoteresultfile}; "
        atlascommand += f"~/atlas_gettaskimage.py {remoteresultfile}"
        atlascommand += " red" if task.use_reduced else " diff"

    elif task.request_type == "IMGZIP":
        localdatafile = Path(settings.RESULTS_DIR, f"job{task.parent_task_id:05d}.txt")
        remotedatafile = Path(remoteresultdir, f"job{task.parent_task_id:05d}.txt")

        # copy out the FP data file first, so that it's available on sc01 for the image gathering script
        copycommand = ["rsync", str(localdatafile), f"{REMOTE_SERVER}:{remotedatafile}"]

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
        stdout, stderr = proc.communicate()

        if stdout:
            for line in stdout.split("\n"):
                logfunc(f"STDOUT: {line}")

        if stderr:
            for line in stderr.split("\n"):
                logfunc(f"STDERR: {line}")

        atlascommand += f"~/atlas_gettaskimages.py {remotedatafile}"
        atlascommand += " red" if task.use_reduced else " diff"

    elif task.request_type == "SSOSTACK":
        remotedatafile = Path(remoteresultdir, f"job{task.id:05d}.txt")
        atlascommand += f"/atlas/bin/stack_rock.sh '{task.mpc_name}'"
        atlascommand += (  # stack_rock.sh doesn't suppport float mjds
            f" {float(task.mjd_min) if task.mjd_min else 0:.0f}"
        )
        atlascommand += f" {float(task.mjd_max) if task.mjd_max else mjdnow():.0f}"
        atlascommand += f" outdir={remotetaskdir}"
        atlascommand += f" | tee {remotedatafile}; "
        atlascommand += f" mv {remotetaskdir}/*.fits {remoteresultfile}; "
        atlascommand += f" rm -rf {remotetaskdir}"

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
    cancelled = False
    timed_out = False
    while not cancelled and not timed_out:
        try:
            proc.communicate(timeout=1)

        except subprocess.TimeoutExpired:
            cancelled = not task_exists(taskid=task.id)
            timed_out = (time.perf_counter() - starttime) >= TASKMAXTIME
            if (time.perf_counter() - lastlogtime) >= 10:
                logfunc(f"ssh has been running for {time.perf_counter() - starttime:.0f} seconds        ")
                lastlogtime = time.perf_counter()
        else:
            break

    if cancelled or timed_out:
        if timed_out:
            logfunc(f"ERROR: ssh was killed after reaching TASKMAXTIME limit of {TASKMAXTIME:.0f} seconds")
        os.kill(proc.pid, SIGTERM)
        return None, None  # don't finish with an error message, because we'll retry it later

    stdout, stderr = proc.communicate()
    logfunc(f"ssh finished after running for {time.perf_counter() - starttime:.1f} seconds")

    if stdout:
        stdoutlines = stdout.split("\n")
        logfunc(f"{REMOTE_SERVER} STDOUT: ({len(stdoutlines)} lines of output)")
        # for line in stdoutlines:
        #     log(logprefix + f"{remoteServer} STDOUT: {line}")

    if stderr:
        for line in stderr.split("\n"):
            logfunc(f"{REMOTE_SERVER} STDERR: {line}")

    # output realtime ssh output line by line
    # while True:
    #     stdoutline = p.stdout.readline()
    #     stderrline = p.stderr.readline()
    #     if not stdoutline and not stderrline:
    #         break
    #     if stdoutline:
    #         log(logprefix + f"{remoteServer} STDOUT >>> {stdoutline.rstrip()}")
    #     if stderrline:
    #         log(logprefix + f"{remoteServer} STDERR >>> {stderrline.rstrip()}")

    if not task_exists(taskid=task.id):  # check if job was cancelled
        return None, None

    # make sure the large zip files are not kept around on the remote system
    # but keep the data files there for possible image requests
    if task.request_type == "FP":
        copycommands = [
            # leave the FP data file on SC01 to be used for follow-up image requests
            ["scp", f"{REMOTE_SERVER}:{remoteresultfile}", str(localresultfile)],
            # move the jpg task image from sc01 (deleting the remote file)
            [
                "rsync",
                "--remove-source-files",
                f'{REMOTE_SERVER}:{Path(remoteresultdir / filename).with_suffix(".jpg")}',
                str(settings.RESULTS_DIR),
            ],
        ]
    elif task.request_type == "SSOSTACK":
        # move the stack *.fits and *.txt data from sc01 (deleting the remote files)
        copycommands = [
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(settings.RESULTS_DIR)],
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remotedatafile}", str(settings.RESULTS_DIR)],
        ]
    else:  # IMGZIP
        # move the image zip from sc01 (deleting the remote file)
        copycommands = [
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(settings.RESULTS_DIR)]
        ]

    for copycommand in copycommands:
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
        stdout, stderr = proc.communicate()

        if stdout:
            for line in stdout.split("\n"):
                logfunc(f"STDOUT: {line}")

        if stderr:
            for line in stderr.split("\n"):
                logfunc(f"STDERR: {line}")

    # got an error message (probably no observations in time range) and no fits file, but task is completed
    if task.request_type == "SSOSTACK" and (
        not localresultfile.exists() and localresultfile.with_suffix(".txt").exists()
    ):
        return localresultfile, localresultfile.with_suffix(".txt").read_text()[:512]

    if not localresultfile.exists():
        # task failed somehow
        return None, None

    if task.request_type == "FP":
        dfforcedphot = pd.read_csv(localresultfile, delim_whitespace=True, escapechar="#", skipinitialspace=True)

        if dfforcedphot.empty:
            # file is just a header row without data
            return localresultfile, "No data returned"

        # if not task.from_api:
        #     make_pdf_plot(taskid=task.id, taskcomment=task.comment, localresultfile=localresultfile,
        #                   logprefix=logprefix, logfunc=log, separate_process=True)

    return localresultfile, None


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

            if task.comment:
                strtask += " comment: '" + task.comment + "'"

            taskdesclist.append(strtask)
            localresultfilelist.append(localresultfile)

    if batchtasks_unfinished == 0:
        logfunc(f"Sending email to {task.user.email} containing {batchtaskcount} tasks")

        message = EmailMessage(
            subject="ATLAS forced photometry results",
            body=("Your forced photometry results are available for:\n\n" + "\n".join(taskdesclist) + "\n\n"),
            from_email=settings.EMAIL_HOST_USER,
            to=[task.user.email],
        )

        attach_size_mb = 0.0

        for localresultfile in localresultfilelist:
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

        with contextlib.suppress(smtplib.SMTPDataError):
            message.send()
    else:
        logfunc(
            f"Waiting to send email until remaining {batchtasks_unfinished} "
            f"of {batchtaskcount} batched tasks are finished."
        )


def handler(signal_received, frame):
    """Handle any cleanup here."""
    log_general("SIGINT or CTRL-C detected. Exiting")
    sys.exit(0)


def do_task(task, slotid: int) -> None:
    """Run a task in a particular slot and send a result email if requested."""

    def logfunc_slotonly(x) -> None:
        log_general(f"slot{slotid:2d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

    def logfunc(x) -> None:
        log_general(f"slot{slotid:2d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

        # also log to the main process
        log_general(f"slot{slotid:2d} task {task.id:05d}: {x}")

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

        if error_msg:
            # an error occured and the task should not be retried (e.g. invalid
            # minor planet center object name or no data returned)
            logfunc(f"Error_msg: {error_msg}")

            send_email_if_needed(task=task, logfunc=logfunc)

            Task.objects.all().filter(pk=task.id).update(
                finishtimestamp=datetime.datetime.now(datetime.UTC)
                .replace(tzinfo=datetime.UTC, microsecond=0)
                .isoformat(),
                queuepos_relative=None,
                error_msg=error_msg,
            )

        elif localresultfile and localresultfile.exists():
            # ingest_results(localresultfile, conn, use_reduced=task["use_reduced"])
            send_email_if_needed(task=task, logfunc=logfunc)

            Task.objects.all().filter(pk=task.id).update(
                finishtimestamp=datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
                queuepos_relative=None,
            )

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
) -> None:
    """Remove old tasks matching given critera from the database and optionally delete their result files (if harddeleterecord)."""
    now = datetime.datetime.now(datetime.UTC)
    filteropts = {"finishtimestamp__isnull": False, "finishtimestamp__lt": now - datetime.timedelta(days=days_ago)}

    if request_type is not None:
        filteropts["request_type"] = request_type

    if not harddeleterecord:
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

    taskid_examples = list(matchingtasks.values_list("id", flat=True)[:10])

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
        logfunc(f"  {strdeleteaction}... (first few task ids: {taskid_examples})")

        for task in matchingtasks:
            task.delete()

        # the previous call only deleted data files and kept the database record
        # marked as archived. The following also deletes the database records.
        if harddeleterecord:
            matchingtasks.delete()

        logfunc("  Done.")


def do_maintenance(maxtime=None):
    """Remove old tasks and associated files according to their type and age."""
    # start_maintenancetime = time.perf_counter()

    def logfunc(x):
        log_general(f"Maintenance: {x}")

    remove_old_tasks(days_ago=14, harddeleterecord=False, request_type="IMGZIP", logfunc=logfunc)

    remove_old_tasks(days_ago=14, harddeleterecord=False, request_type="SSOSTACK", logfunc=logfunc)

    remove_old_tasks(days_ago=183, harddeleterecord=False, request_type="FP", logfunc=logfunc)

    # archived API tasks
    remove_old_tasks(days_ago=7, harddeleterecord=False, is_archived=True, from_api=True, logfunc=logfunc)

    # other API tasks
    remove_old_tasks(days_ago=31, harddeleterecord=True, from_api=True, logfunc=logfunc)

    # Any old tasks
    remove_old_tasks(days_ago=183, harddeleterecord=True, logfunc=logfunc)

    # # this can get very slow
    # rm_unassociated_files(logprefix, start_maintenancetime, maxtime)


def main() -> None:
    """Run queued tasks and clean up on old tasks."""
    signal(SIGINT, handler)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    def logfunc(x):
        log_general(x)

    logfunc("Starting forcedphot task runner...")
    mp.set_start_method("spawn")
    numslots: int = 8
    procs: list[mp.Process | None] = [None for _ in range(numslots)]
    procs_userids: dict[int, int] = {}  # user_id of currently running job, or None
    procs_taskids: dict[int, int] = {}  # tasks_id of currently running job, or None

    last_maintenancetime: float = float("-inf")
    printedwaiting = False
    while True:
        time.sleep(0.5)

        if (time.perf_counter() - last_maintenancetime) > 60 * 60:  # once per hour
            last_maintenancetime = time.perf_counter()
            do_maintenance(maxtime=300)
            printedwaiting = False

        for slotid, proc in enumerate(procs):
            if proc is not None and proc.exitcode is not None:
                proc.join()
                proc.close()
                procs[slotid] = None
                procs_userids.pop(slotid)
                procs_taskids.pop(slotid)

                numslotsfree = sum(1 if p is None else 0 for p in procs)
                logfunc(f"slot {slotid} is now free. {numslotsfree} of {numslots} slots are available")

        queuedtasks = (
            Task.objects.all().filter(finishtimestamp__isnull=True, is_archived=False).order_by("queuepos_relative")
        )
        queuedtaskcount = queuedtasks.count()

        if queuedtaskcount == 0:
            if not printedwaiting:
                logfunc("Waiting for tasks...")
                printedwaiting = True

        else:
            slotid = -1
            try:
                slotid = procs.index(None)  # lowest available slot

            except ValueError:
                slotid = -1

            if slotid >= 0:
                task = queuedtasks.exclude(user_id__in=list(procs_userids.values())).first()

                if task is not None:
                    printedwaiting = False
                    logfunc(f"Unfinished tasks in queue: {queuedtaskcount}")
                    logfunc(f"Running task {task.id} in slot {slotid}")
                    procs_userids[slotid] = task.user_id
                    procs_taskids[slotid] = task.id

                    proc = mp.Process(target=do_task, kwargs={"task": task, "slotid": slotid})
                    proc.start()
                    procs[slotid] = proc


if __name__ == "__main__":
    main()
