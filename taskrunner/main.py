#!/usr/bin/env python3

import datetime
import os
import subprocess
import time
from pathlib import Path
from signal import SIGINT, SIGTERM, signal

import pandas as pd

import django
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.forms.models import model_to_dict

# sys.path.insert(1, str(Path(sys.path[0]).parent.resolve()))

import atlasserver.settings as settings
# from forcephot.misc import make_pdf_plot

remoteServer = 'atlas'
localresultdir = Path(settings.RESULTS_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'atlasserver.settings')

# import atlasserver.wsgi
django.setup()

from forcephot.models import Task

TASKMAXTIME = 1200

logdir = Path(__file__).resolve().parent / 'logs'

# so that current log can be archived periodically, keep track
# of the filename with date, so that it can be created when the date changes
LASTLOGFILEARCHIVED = None


def localresultfileprefix(id):
    return str(Path(localresultdir / f'job{int(id):05d}'))


def get_localresultfile(id):
    return localresultfileprefix(id) + '.txt'


def task_exists(taskid):
    try:
        Task.objects.all().get(id=taskid)

        return True

    except ObjectDoesNotExist:
        pass

    return False


def remove_task_resultfiles(taskid, parent_task_id=None, request_type=None):
    # delete any associated result files from a deleted task
    if request_type == 'FP':
        taskfiles = [Path(localresultdir, localresultfileprefix(taskid) + '.txt')]
    elif request_type == 'IMGZIP' and parent_task_id is not None:
        taskfiles = [Path(localresultdir, localresultfileprefix(parent_task_id) + '.zip')]
    else:
        taskfiles = list(Path(localresultdir).glob(pattern=localresultfileprefix(taskid) + '.*'))

    for taskfile in taskfiles:
        if os.path.exists(taskfile):
            try:
                os.remove(taskfile)
            except OSError:
                log(f"Error deleting file: {Path(taskfile).relative_to(localresultdir)}")
            else:
                log(f"Deleted {Path(taskfile).relative_to(localresultdir)}")


def runtask(task, logprefix='', **kwargs):
    # run the forced photometry on atlas sc01 and retrieve the result
    # returns (resultfilename, error_msg)
    # - resultfilename will be False if it could not be created due to an error
    # - error_msg is False unless there was an error that would make retries pointless (e.g. invalid object name)

    if task.request_type == 'FP':
        filename = f"job{task.id:05d}.txt"
    elif task.request_type == 'IMGZIP':
        filename = f"job{task.parent_task_id:05d}.zip"
    else:
        return False, False

    remoteresultdir = Path('~/atlasserver/results/')
    remoteresultfile = Path(remoteresultdir, filename)

    localresultfile = Path(localresultdir, filename)
    localresultdir.mkdir(parents=True, exist_ok=True)

    atlascommand = "nice -n 19 "
    if task.request_type == 'FP':
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
        atlascommand += f"~/atlas_gettaskfirstimage.py {remoteresultfile}"
        atlascommand += (" red" if task.use_reduced else " diff")

    elif task.request_type == 'IMGZIP':
        localdatafile = Path(localresultdir, f"job{task.parent_task_id:05d}.txt")
        remotedatafile = Path(remoteresultdir, f"job{task.parent_task_id:05d}.txt")

        copycommand = ['rsync', str(localdatafile), f'{remoteServer}:{remotedatafile}']

        log(logprefix + ' '.join(copycommand))

        p = subprocess.Popen(copycommand, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding='utf-8', bufsize=1, universal_newlines=True)
        stdout, stderr = p.communicate()

        if stdout:
            for line in stdout.split('\n'):
                log(logprefix + f"STDOUT: {line}")

        if stderr:
            for line in stderr.split('\n'):
                log(logprefix + f"STDERR: {line}")

        atlascommand += f"~/atlas_gettaskimages.py {remotedatafile}"
        atlascommand += (" red" if task.use_reduced else " diff")

    log(logprefix + f"Executing on {remoteServer}: {atlascommand}")

    p = subprocess.Popen(["ssh", f"{remoteServer}", atlascommand], shell=False,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding='utf-8', bufsize=1, universal_newlines=True)

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
                log(logprefix + f"ssh has been running for {time.perf_counter() - starttime:.0f} seconds        ")
                lastlogtime = time.perf_counter()
        else:
            break

    if cancelled or timed_out:
        if timed_out:
            log(logprefix + f"ERROR: ssh was killed after reaching TASKMAXTIME limit of {TASKMAXTIME:.0f} seconds")
        os.kill(p.pid, SIGTERM)
        return False, False  # don't finish with an error message, because we'll retry it later

    stdout, stderr = p.communicate()
    log(logprefix + f'ssh finished after running for {time.perf_counter() - starttime:.1f} seconds')

    if stdout:
        stdoutlines = stdout.split('\n')
        log(logprefix + f"{remoteServer} STDOUT: ({len(stdoutlines)} lines of output)")
        # for line in stdoutlines:
        #     log(logprefix + f"{remoteServer} STDOUT: {line}")

    if stderr:
        for line in stderr.split('\n'):
            log(logprefix + f"{remoteServer} STDERR: {line}")

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
    if task.request_type == 'FP':
        copycommands = [
            ['scp', f'{remoteServer}:{remoteresultfile}', str(localresultfile)],
            ['rsync', '--remove-source-files',
             f'{remoteServer}:{Path(remoteresultdir / filename).with_suffix(".jpg")}', str(localresultdir)]
        ]
    else:
        copycommands = [['rsync', '--remove-source-files', f'{remoteServer}:{remoteresultfile}', str(localresultdir)]]

    for copycommand in copycommands:
        log(logprefix + ' '.join(copycommand))

        p = subprocess.Popen(copycommand, shell=False,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding='utf-8', bufsize=1, universal_newlines=True)
        stdout, stderr = p.communicate()

        if stdout:
            for line in stdout.split('\n'):
                log(logprefix + f"STDOUT: {line}")

        if stderr:
            for line in stderr.split('\n'):
                log(logprefix + f"STDERR: {line}")

    if not os.path.exists(localresultfile):
        # task failed somehow
        return False, False

    if task.request_type == 'FP':
        df = pd.read_csv(localresultfile, delim_whitespace=True, escapechar='#', skipinitialspace=True)

        if df.empty:
            # file is just a header row without data
            return localresultfile, 'No data returned'

        # if not task.from_api:
        #     make_pdf_plot(taskid=task.id, taskcomment=task.comment, localresultfile=localresultfile,
        #                   logprefix=logprefix, logfunc=log, separate_process=True)

    return localresultfile, False


def send_email_if_needed(task, logprefix=''):
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
            log(logprefix + f'Sending email to {task.user.email} containing {batchtaskcount} tasks')

            message = EmailMessage(
                subject='ATLAS forced photometry results',
                body=(
                    'Your forced photometry results are attached for:\n\n'
                    + '\n'.join(taskdesclist) + '\n\n'),
                from_email=settings.EMAIL_HOST_USER,
                to=[task.user.email],
            )

            for localresultfile in localresultfilelist:
                pdfpath = Path(localresultfile).with_suffix('.pdf')
                if os.path.exists(pdfpath):
                    message.attach_file(pdfpath)

            for localresultfile in localresultfilelist:
                message.attach_file(localresultfile)

            message.send()
        else:
            log(logprefix + f'Waiting to send email until remaining {batchtasks_unfinished} '
                f'of {batchtaskcount} batched tasks are finished.')
    elif task.send_email:
        log(logprefix + f'User {task.user.username} has no email address.')
    else:
        log(logprefix + f'User {task.user.username} did not request an email.')


def handler(signal_received, frame):
    # Handle any cleanup here
    log('SIGINT or CTRL-C detected. Exiting')
    exit(0)


def log(msg, *args, **kwargs):
    global LASTLOGFILEARCHIVED
    dtnow = datetime.datetime.utcnow()
    strtime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f'{strtime}  {msg}'
    print(line, *args, **kwargs)

    logfile_archive = Path(logdir, f'fprunnerlog_{dtnow.year:4d}-{dtnow.month:02d}-{dtnow.day:02d}.txt')
    logfile_latest = Path(logdir, 'fprunnerlog_latest.txt')

    if LASTLOGFILEARCHIVED and logfile_archive != LASTLOGFILEARCHIVED:
        import shutil
        # os.rename(logfile_latest, logfile_archive)
        shutil.copyfile(logfile_latest, LASTLOGFILEARCHIVED)
        flogfile = logfile_latest.open("w")
    else:
        flogfile = logfile_latest.open("a")

    LASTLOGFILEARCHIVED = logfile_archive

    # with logfile_latest.open("a") as flogfile:
    flogfile.write(line + '\n')
    flogfile.close()


def do_taskloop():
    queuedtasks = Task.objects.all().filter(finishtimestamp__isnull=True, is_archived=False)
    taskcount = queuedtasks.count()

    if taskcount == 0:
        return 0
    else:
        log(f'Unfinished tasks in queue: {taskcount}')

    task = queuedtasks.order_by('queuepos_relative').first()
    taskdict = model_to_dict(task)

    logprefix = f"task {task.id:05d}: "
    Task.objects.all().filter(pk=task.id).update(
        starttimestamp=datetime.datetime.utcnow().replace(
            tzinfo=datetime.timezone.utc, microsecond=0).isoformat())

    log(logprefix + f"Starting task for {task.user.username} ({task.user.email}):")
    for key, value in taskdict.items():
        log(f'{logprefix}   {key:>17}: {value}')

    runtask_starttime = time.perf_counter()

    localresultfile, error_msg = runtask(logprefix=logprefix, task=task)

    if not task_exists(taskid=task.id):  # task was cancelled
        log(logprefix + "Task was cancelled during execution (no longer in database)")

        # in case a result file was created, delete it
        remove_task_resultfiles(taskid=task.id, parent_task_id=task.parent_task_id,
                                request_type=task.request_type)
    else:
        runtask_duration = time.perf_counter() - runtask_starttime

        log(logprefix + f"Task ran for {runtask_duration:.1f} seconds")

        if error_msg:
            # an error occured and the task should not be retried (e.g. invalid
            # minor planet center object name or no data returned)
            log(logprefix + f"Error_msg: {error_msg}")

            send_email_if_needed(task=task, logprefix=logprefix)

            Task.objects.all().filter(pk=task.id).update(
                finishtimestamp=datetime.datetime.utcnow().replace(
                    tzinfo=datetime.timezone.utc, microsecond=0).isoformat(),
                queuepos_relative=None,
                error_msg=error_msg)
        else:
            if localresultfile and os.path.exists(localresultfile):
                # ingest_results(localresultfile, conn, use_reduced=task["use_reduced"])
                send_email_if_needed(task=task, logprefix=logprefix)

                Task.objects.all().filter(pk=task.id).update(
                    finishtimestamp=datetime.datetime.utcnow().replace(
                        tzinfo=datetime.timezone.utc, microsecond=0).isoformat(),
                    queuepos_relative=None)

            else:
                waittime = 5
                log(logprefix + f"ERROR: Task was not completed successfully. Waiting {waittime} seconds "
                    f"before continuing with next task...")
                time.sleep(waittime)  # in case we're stuck in an error loop, wait a bit before trying again

    return taskcount


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


def remove_old_tasks(logprefix, request_type, days_ago):
    assert days_ago > 29

    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    oldtasks = Task.objects.all().filter(
        finishtimestamp__lt=now - datetime.timedelta(days=days_ago),
        request_type='IMGZIP',
        is_archived=False
    )

    taskcount = oldtasks.count()

    taskid_examples = list(oldtasks.values_list('id', flat=True)[:10])

    log(logprefix + f"There are {taskcount} {request_type} tasks that finished more than {days_ago} days ago")

    if taskcount > 0:
        log(logprefix + f"  first few task ids: {taskid_examples}")
        log(logprefix + "  deleting...")

        for task in oldtasks:
            task.delete()

        log(logprefix + "  done.")


def do_maintenance(maxtime=None):
    # start_maintenancetime = time.perf_counter()

    logprefix = "Maintenance: "

    remove_old_tasks(logprefix=logprefix, request_type='IMGZIP', days_ago=60)
    remove_old_tasks(logprefix=logprefix, request_type='FP', days_ago=365)

    # # this can get very slow
    # rm_unassociated_files(logprefix, start_maintenancetime, maxtime)


def main():
    signal(SIGINT, handler)

    logdir.mkdir(parents=True, exist_ok=True)

    log('Starting forcedphot task runner...')

    last_maintenancetime = float('-inf')
    printedwaiting = False
    while True:
        if (time.perf_counter() - last_maintenancetime) > 60 * 60:  # once per hour
            last_maintenancetime = time.perf_counter()
            do_maintenance(maxtime=300)
            printedwaiting = False

        queuedtaskcount = do_taskloop()
        if queuedtaskcount == 0:
            if not printedwaiting:
                log('Waiting for tasks...')
                printedwaiting = True
            time.sleep(1)
        else:
            printedwaiting = False


if __name__ == '__main__':
    main()
