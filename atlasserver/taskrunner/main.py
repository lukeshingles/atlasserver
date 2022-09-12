#!/usr/bin/env python3
import datetime
import os
import subprocess
import time
from multiprocessing import Process
from pathlib import Path
from signal import SIGINT
from signal import signal
from signal import SIGTERM
from typing import Optional

import django
import pandas as pd
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.forms.models import model_to_dict

import atlasserver.settings as settings

remoteServer = "atlas"
localresultdir = Path(settings.RESULTS_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "atlasserver.settings")

# import atlasserver.wsgi
django.setup()

from atlasserver.forcephot.models import Task

TASKMAXTIME: int = 1200

logdir: Path = Path(__file__).resolve().parent / "logs"

# so that current log can be archived periodically, keep track
# of the filename with a date, so that it can be created when the date changes
LASTLOGFILEARCHIVED: dict[str, Path] = {}


def localresultfileprefix(id):
    return str(Path(localresultdir / f"job{int(id):05d}"))


def get_localresultfile(id):
    return localresultfileprefix(id) + ".txt"


def log_general(msg, suffix="", *args, **kwargs):
    global LASTLOGFILEARCHIVED
    dtnow = datetime.datetime.utcnow()
    strtime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{strtime}  {msg}"
    if suffix == "":
        print(line, *args, **kwargs)

    logfile_archive = Path(logdir, f"fprunnerlog_{dtnow.year:4d}-{dtnow.month:02d}-{dtnow.day:02d}{suffix}.txt")
    logfile_latest = Path(logdir, f"fprunnerlog_latest{suffix}.txt")

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
    flogfile.flush()
    flogfile.close()


def task_exists(taskid):
    try:
        Task.objects.get(id=taskid)

        return True

    except (ObjectDoesNotExist, IndexError):
        pass

    return False


def remove_task_resultfiles(taskid, parent_task_id=None, request_type=None, logfunc=log_general):
    # delete any associated result files from a deleted task
    if request_type == "FP":
        taskfiles = [Path(localresultdir, localresultfileprefix(taskid) + ".txt")]
    elif request_type == "IMGZIP" and parent_task_id is not None:
        taskfiles = [Path(localresultdir, localresultfileprefix(parent_task_id) + ".zip")]
    else:
        taskfiles = list(Path(localresultdir).glob(pattern=localresultfileprefix(taskid) + ".*"))

    for taskfile in taskfiles:
        if os.path.exists(taskfile):
            try:
                os.remove(taskfile)
            except OSError:
                logfunc(f"Error deleting file: {Path(taskfile).relative_to(localresultdir)}")
            else:
                logfunc(f"Deleted {Path(taskfile).relative_to(localresultdir)}")


def runtask(task, logfunc=None, **kwargs):
    # run the forced photometry on atlas sc01 and retrieve the result
    # returns (resultfilename, error_msg)
    # - resultfilename will be False if it could not be created due to an error
    # - error_msg is False unless there was an error that would make retries pointless (e.g. invalid object name)

    if task.request_type == "FP":
        filename = f"job{task.id:05d}.txt"
    elif task.request_type == "IMGZIP":
        filename = f"job{task.parent_task_id:05d}.zip"
    else:
        return False, False

    remoteresultdir = Path("~/atlasserver/results/")
    remoteresultfile = Path(remoteresultdir, filename)

    localresultfile = Path(localresultdir, filename)
    localresultdir.mkdir(parents=True, exist_ok=True)

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
        localdatafile = Path(localresultdir, f"job{task.parent_task_id:05d}.txt")
        remotedatafile = Path(remoteresultdir, f"job{task.parent_task_id:05d}.txt")

        copycommand = ["rsync", str(localdatafile), f"{remoteServer}:{remotedatafile}"]

        logfunc(" ".join(copycommand))

        p = subprocess.Popen(
            copycommand,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            bufsize=1,
            universal_newlines=True,
        )
        stdout, stderr = p.communicate()

        if stdout:
            for line in stdout.split("\n"):
                logfunc(f"STDOUT: {line}")

        if stderr:
            for line in stderr.split("\n"):
                logfunc(f"STDERR: {line}")

        atlascommand += f"~/atlas_gettaskimages.py {remotedatafile}"
        atlascommand += " red" if task.use_reduced else " diff"

    logfunc(f"Executing on {remoteServer}: {atlascommand}")

    p = subprocess.Popen(
        ["ssh", f"{remoteServer}", atlascommand],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        bufsize=1,
        universal_newlines=True,
    )

    starttime = time.perf_counter()
    lastlogtime = 0
    cancelled = False
    timed_out = False
    while not cancelled and not timed_out:
        try:
            p.communicate(timeout=1)

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
        os.kill(p.pid, SIGTERM)
        return False, False  # don't finish with an error message, because we'll retry it later

    stdout, stderr = p.communicate()
    logfunc(f"ssh finished after running for {time.perf_counter() - starttime:.1f} seconds")

    if stdout:
        stdoutlines = stdout.split("\n")
        logfunc(f"{remoteServer} STDOUT: ({len(stdoutlines)} lines of output)")
        # for line in stdoutlines:
        #     log(logprefix + f"{remoteServer} STDOUT: {line}")

    if stderr:
        for line in stderr.split("\n"):
            logfunc(f"{remoteServer} STDERR: {line}")

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
        return False, False

    # make sure the large zip files are not kept around on the remote system
    # but keep the data files there for possible image requests
    if task.request_type == "FP":
        copycommands = [
            ["scp", f"{remoteServer}:{remoteresultfile}", str(localresultfile)],
            [
                "rsync",
                "--remove-source-files",
                f'{remoteServer}:{Path(remoteresultdir / filename).with_suffix(".jpg")}',
                str(localresultdir),
            ],
        ]
    else:
        copycommands = [["rsync", "--remove-source-files", f"{remoteServer}:{remoteresultfile}", str(localresultdir)]]

    for copycommand in copycommands:
        logfunc(" ".join(copycommand))

        p = subprocess.Popen(
            copycommand,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            bufsize=1,
            universal_newlines=True,
        )
        stdout, stderr = p.communicate()

        if stdout:
            for line in stdout.split("\n"):
                logfunc(f"STDOUT: {line}")

        if stderr:
            for line in stderr.split("\n"):
                logfunc(f"STDERR: {line}")

    if not os.path.exists(localresultfile):
        # task failed somehow
        return False, False

    if task.request_type == "FP":
        df = pd.read_csv(localresultfile, delim_whitespace=True, escapechar="#", skipinitialspace=True)

        if df.empty:
            # file is just a header row without data
            return localresultfile, "No data returned"

        # if not task.from_api:
        #     make_pdf_plot(taskid=task.id, taskcomment=task.comment, localresultfile=localresultfile,
        #                   logprefix=logprefix, logfunc=log, separate_process=True)

    return localresultfile, False


def send_email_if_needed(task, logfunc):
    if task.send_email and task.user.email:
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
                localresultfile = get_localresultfile(batchtask.id)
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
                body=("Your forced photometry results are attached for:\n\n" + "\n".join(taskdesclist) + "\n\n"),
                from_email=settings.EMAIL_HOST_USER,
                to=[task.user.email],
            )

            for localresultfile in localresultfilelist:
                pdfpath = Path(localresultfile).with_suffix(".pdf")
                if os.path.exists(pdfpath):
                    message.attach_file(pdfpath)

            for localresultfile in localresultfilelist:
                message.attach_file(localresultfile)

            message.send()
        else:
            logfunc(
                f"Waiting to send email until remaining {batchtasks_unfinished} "
                f"of {batchtaskcount} batched tasks are finished."
            )
    elif task.send_email:
        logfunc(f"Tasked completed ok, but user {task.user.username} has no email address!")
    else:
        logfunc(f"Completed successfully ({task.user.username} did not request an email)")


def handler(signal_received, frame):
    # Handle any cleanup here
    log_general("SIGINT or CTRL-C detected. Exiting")
    exit(0)


def do_task(task, slotid):
    def logfunc_slotonly(x):
        log_general(f"slot{slotid:02d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

    def logfunc(x):
        log_general(f"slot{slotid:02d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

        # also log to the main process
        log_general(f"slot{slotid:02d} task {task.id:05d}: {x}")

    Task.objects.all().filter(pk=task.id).update(
        starttimestamp=datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc, microsecond=0).isoformat()
    )

    logfunc(f"Starting {task.request_type} task for {task.user.username} ({task.user.email}):")
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
                finishtimestamp=datetime.datetime.utcnow()
                .replace(tzinfo=datetime.timezone.utc, microsecond=0)
                .isoformat(),
                queuepos_relative=None,
                error_msg=error_msg,
            )
        else:
            if localresultfile and os.path.exists(localresultfile):
                # ingest_results(localresultfile, conn, use_reduced=task["use_reduced"])
                send_email_if_needed(task=task, logfunc=logfunc)

                Task.objects.all().filter(pk=task.id).update(
                    finishtimestamp=datetime.datetime.utcnow()
                    .replace(tzinfo=datetime.timezone.utc, microsecond=0)
                    .isoformat(),
                    queuepos_relative=None,
                )

            else:
                waittime = 5
                logfunc(
                    f"ERROR: Task was not completed successfully. Waiting {waittime} seconds to slow down retries..."
                )
                time.sleep(waittime)  # in case we're stuck in an error loop, wait a bit before trying again


# def rm_unassociated_files(logprefix, start_maintenancetime, maxtime):
#     # WARNING: DO NOT USE until it is updated to take into account the connections between photometry and image tasks
#     log(logprefix + "Checking for unassociated result files...")
#
#     taskid_list = list(Task.objects.all().values_list('id', flat=True))
#     print('taskidlist', taskid_list)
#     for resultfilepath in Path(localresultdir).glob('job*.*'):
#         if resultfilepath.suffix in ['.txt', '.pdf', '.js', '.zip', '.jpg']:
#             try:
#                 file_taskidstr = resultfilepath.stem[3:]
#                 file_taskid = int(file_taskidstr)
#                 # assert task_exists(conn=conn, taskid=file_taskid) == (file_taskid in taskid_list)
#                 if file_taskid not in taskid_list:
#                     log(logprefix + f"Deleting unassociated result file {resultfilepath.relative_to(localresultdir)} "
#                         f"because task {file_taskid} is not in the database")
#                     resultfilepath.unlink()
#                 # elif resultfilepath.suffix == '.txt':
#                 #     if not os.path.exists(resultfilepath.with_suffix('.pdf')):  # result txt file without a PDF
#                 #         # load the text file to check if it contains any data rows to be plotted
#                 #         df = pd.read_csv(
#                 #             resultfilepath, delim_whitespace=True, escapechar='#', skipinitialspace=True)
#                 #         if not df.empty:
#                 #             log(logprefix + "Creating missing PDF from result file "
#                 #                 f"{resultfilepath.relative_to(localresultdir)}")
#                 #
#                 #             make_pdf_plot(taskid=file_taskid, localresultfile=resultfilepath, logprefix=logprefix,
#                 #                           logfunc=log, separate_process=True)
#
#             except ValueError:
#                 # log(f"Could not understand task id of file {resultfilepath.relative_to(localresultdir)}")
#                 pass
#
#         maintenance_duration = time.perf_counter() - start_maintenancetime
#         if maxtime and maintenance_duration > maxtime:
#             log(logprefix + f"Maintenance has run for {maintenance_duration:.0f} seconds "
#                 f"(above limit of {maxtime:.0f}). Resuming normal tasks...")
#             break
#
#     log(logprefix + "Finished checking for unassociated result files")


def remove_old_tasks(
    days_ago, harddeleterecord=False, request_type=None, is_archived=None, from_api=None, logfunc=log_general
):
    assert days_ago > 29

    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    filteropts = dict(finishtimestamp__isnull=False, finishtimestamp__lt=now - datetime.timedelta(days=days_ago))

    if not harddeleterecord:
        # exclude tasks that are already soft deleted from soft deletion query
        is_archived = False

    if request_type is not None:
        filteropts["request_type"] = request_type

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
    # start_maintenancetime = time.perf_counter()

    def logfunc(x):
        log_general(f"Maintenance: {x}")

    remove_old_tasks(days_ago=35, harddeleterecord=False, request_type="IMGZIP", logfunc=logfunc)

    remove_old_tasks(days_ago=365, harddeleterecord=False, request_type="FP", logfunc=logfunc)

    remove_old_tasks(days_ago=70, harddeleterecord=True, is_archived=True, from_api=True, logfunc=logfunc)

    remove_old_tasks(days_ago=140, harddeleterecord=True, is_archived=False, from_api=True, logfunc=logfunc)

    remove_old_tasks(days_ago=365, harddeleterecord=True, logfunc=logfunc)

    # # this can get very slow
    # rm_unassociated_files(logprefix, start_maintenancetime, maxtime)


def main() -> None:
    signal(SIGINT, handler)

    logdir.mkdir(parents=True, exist_ok=True)

    def logfunc(x):
        log_general(x)

    logfunc("Starting forcedphot task runner...")

    numslots: int = 4
    procs: list[Optional[Process]] = list([None for _ in range(numslots)])
    procs_userids: dict[int, int] = {}  # user_id of currently running job, or None
    procs_taskids: dict[int, int] = {}  # tasks_id of currently running job, or None

    last_maintenancetime: float = float("-inf")
    printedwaiting = False
    while True:
        if (time.perf_counter() - last_maintenancetime) > 60 * 60:  # once per hour
            last_maintenancetime = time.perf_counter()
            do_maintenance(maxtime=300)
            printedwaiting = False

        queuedtasks = (
            Task.objects.all().filter(finishtimestamp__isnull=True, is_archived=False).order_by("queuepos_relative")
        )
        queuedtaskcount = queuedtasks.count()

        for slotid, proc in enumerate(procs):
            if proc is not None and proc.exitcode is not None:
                proc.join()
                proc.close()
                logfunc(f"Ended task {procs_taskids[slotid]} in slot {slotid}")
                procs[slotid] = None
                procs_userids.pop(slotid)
                procs_taskids.pop(slotid)

        if queuedtaskcount == 0:
            if not printedwaiting:
                logfunc("Waiting for tasks...")
                printedwaiting = True
            time.sleep(1)
        else:
            printedwaiting = False

            if not any([p is None for p in procs]):
                # no free slots
                time.sleep(1)
            else:
                slotid = -1
                try:
                    slotid = procs.index(None)  # lowest available slot

                except ValueError:
                    time.sleep(1)

                if slotid >= 0:
                    task = queuedtasks.exclude(user_id__in=list(procs_userids.values())).first()

                    if task is not None:
                        logfunc(f"Unfinished tasks in queue: {queuedtaskcount}")
                        logfunc(f"Running task {task.id} in slot {slotid}")
                        procs_userids[slotid] = task.user_id
                        procs_taskids[slotid] = task.id

                        proc = Process(target=do_task, kwargs={"task": task, "slotid": slotid})
                        proc.start()
                        procs[slotid] = proc


if __name__ == "__main__":
    main()
