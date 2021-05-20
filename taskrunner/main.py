#!/usr/bin/env python3

import datetime
import os
import subprocess
import sys
import time
from pathlib import Path
from signal import SIGINT, SIGTERM, signal

import mysql.connector
import pandas as pd
from django.core.mail import EmailMessage
from dotenv import load_dotenv

sys.path.insert(1, str(Path(sys.path[0]).parent.resolve()))

import atlasserver.settings as settings
from forcephot.misc import make_pdf_plot

load_dotenv(override=True)

remoteServer = 'atlas'
localresultdir = Path(settings.STATIC_ROOT, 'results')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'atlasserver.settings')
logdir = Path(__file__).resolve().parent / 'logs'
TASKMAXTIME = 1200

CONNKWARGS = {
    'host': settings.DATABASES['default']['HOST'],
    'port': settings.DATABASES['default']['PORT'],
    'database': settings.DATABASES['default']['NAME'],
    'user': settings.DATABASES['default']['USER'],
    'password': settings.DATABASES['default']['PASSWORD'],
    'autocommit': True,
}


# set a limit on the number of tasks run for each user per full pass of the task queue
# this prevents a single user from monopolising a large block of the queue
USERTASKLOADLIMIT = 1


def localresultfileprefix(id):
    return str(Path(localresultdir / f'job{int(id):05d}'))


def get_localresultfile(id):
    return localresultfileprefix(id) + '.txt'


def task_exists(conn, taskid):
    cur = conn.cursor(dictionary=True)
    cur.execute(f"SELECT COUNT(*) as taskcount FROM forcephot_task WHERE id={taskid};")
    exists = not (cur.fetchone()['taskcount'] == 0)
    conn.commit()
    cur.close()
    return exists


def get_taskid_list(conn):
    cur = conn.cursor(dictionary=False)
    cur.execute("SELECT id FROM forcephot_task;")
    taskid_list = [row[0] for row in cur.fetchall()]
    conn.commit()
    cur.close()
    return taskid_list


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


def runtask(task, conn, logprefix='', **kwargs):
    # run the forced photometry on atlas sc01 and retrieve the result
    # returns (resultfilename, error_msg)
    # - resultfilename will be False if it could not be created due to an error
    # - error_msg is False unless there was an error that would make retries pointless (e.g. invalid object name)

    if task['request_type'] == 'FP':
        filename = f"job{task['id']:05d}.txt"
    elif task['request_type'] == 'IMGZIP':
        filename = f"job{task['parent_task_id']:05d}.zip"
    else:
        return False, False

    remoteresultdir = Path('~/atlasserver/results/')
    remoteresultfile = Path(remoteresultdir, filename)

    localresultfile = Path(localresultdir, filename)
    localresultdir.mkdir(parents=True, exist_ok=True)

    atlascommand = "nice -n 19 "
    if task['request_type'] == 'FP':
        if task['mpc_name']:
            atlascommand += f"/atlas/bin/ssforce.sh {task['mpc_name']}"
        else:
            atlascommand += f"/atlas/bin/force.sh {float(task['ra'])} {float(task['dec'])}"

        if task['use_reduced']:
            atlascommand += " red=1"

        if task['radec_epoch_year']:
            atlascommand += f" epoch={task['radec_epoch_year']}"

        if task['propermotion_ra']:
            atlascommand += f" pmra={task['propermotion_ra']}"

        if task['propermotion_dec']:
            atlascommand += f" pmdec={task['propermotion_dec']}"

        if task['mjd_min']:
            atlascommand += f" m0={float(task['mjd_min'])}"
        if task['mjd_max']:
            atlascommand += f" m1={float(task['mjd_max'])}"

        atlascommand += " dodb=1 parallel=32"

        # for debugging because force.sh takes a long time to run
        # atlascommand = "echo '(DEBUG MODE: force.sh output will be here)'"

        atlascommand += " | sort -n"
        atlascommand += f" | tee {remoteresultfile}; "
        atlascommand += f"~/atlas_gettaskfirstimage.py {remoteresultfile}"
        atlascommand += (" red" if task['use_reduced'] else " diff")

    elif task['request_type'] == 'IMGZIP':
        remotedatafile = Path(remoteresultdir, f"job{task['parent_task_id']:05d}.txt")
        atlascommand += f"~/atlas_gettaskimages.py {remotedatafile}"
        atlascommand += (" red" if task['use_reduced'] else " diff")

    log(logprefix + f"Executing on {remoteServer}: {atlascommand}")

    p = subprocess.Popen(["ssh", f"{remoteServer}", atlascommand],
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
            cancelled = not task_exists(conn=conn, taskid=task['id'])
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

    if not task_exists(conn=conn, taskid=task['id']):  # check if job was cancelled
        return False, False

    # make sure the large zip files are not kept around on the remote system
    # but keep the data files there for possible image requests
    if task['request_type'] == 'FP':
        copycommand = f'scp {remoteServer}:{remoteresultfile} "{localresultfile}"; rsync --remove-source-files {remoteServer}:{Path(remoteresultdir / filename).with_suffix(".jpg")} {localresultdir}'
    else:
        copycommand = f'rsync --remove-source-files {remoteServer}:{remoteresultfile} {localresultdir}'

    log(logprefix + copycommand)

    p = subprocess.Popen(copycommand,
                         shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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

    if task['request_type'] == 'FP':
        df = pd.read_csv(localresultfile, delim_whitespace=True, escapechar='#', skipinitialspace=True)

        if df.empty:
            # file is just a header row without data
            return localresultfile, 'No data returned'

        # if not task['from_api']:
        #     make_pdf_plot(taskid=task['id'], taskcomment=task['comment'], localresultfile=localresultfile,
        #                   logprefix=logprefix, logfunc=log, separate_process=True)

    return localresultfile, False


def send_email_if_needed(conn, task, logprefix=''):
    if task["send_email"] and task["email"]:
        # if we find an unfinished task in the same batch, hold off sending the email
        # same batch here is defined as being queued by the same user with identical timestamps
        batchtasks_unfinished = 0
        batchtaskcount = 0

        taskdesclist = []
        localresultfilelist = []
        cur3 = conn.cursor(dictionary=True)
        cur3.execute(
            "SELECT forcephot_task.* FROM forcephot_task, forcephot_task T2 "
            f"WHERE T2.id={task['id']} AND forcephot_task.user_id={task['user_id']} AND "
            f"T2.user_id={task['user_id']} AND "
            "forcephot_task.send_email=true AND "
            "forcephot_task.timestamp=T2.timestamp;")

        for batchtaskrow in cur3:
            batchtask = dict(batchtaskrow)
            batchtaskcount += 1
            if not batchtask['finishtimestamp'] and batchtask['id'] != task['id']:
                batchtasks_unfinished += 1
            else:
                localresultfile = get_localresultfile(batchtask['id'])
                taskurl = f"https://fallingstar-data.com/forcedphot/queue/{batchtask['id']}/"
                strtask = (
                    f"Task {batchtask['id']}: RA {batchtask['ra']} Dec {batchtask['dec']} "
                    f"{'img_reduced' if batchtask['use_reduced'] else 'img_difference'} "
                    f"\n{taskurl}\n"
                )

                if task['comment']:
                    strtask += " comment: '" + task['comment'] + "'"

                taskdesclist.append(strtask)
                localresultfilelist.append(localresultfile)
        conn.commit()
        cur3.close()

        if batchtasks_unfinished == 0:
            log(logprefix + f'Sending email to {task["email"]} containing {batchtaskcount} tasks')

            message = EmailMessage(
                subject='ATLAS forced photometry results',
                body=(
                    'Your forced photometry results are attached for:\n\n'
                    + '\n'.join(taskdesclist) + '\n\n'),
                from_email=os.environ.get('EMAIL_HOST_USER'),
                to=[task["email"]],
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
    elif task["send_email"]:
        log(logprefix + f'User {task["username"]} has no email address.')
    else:
        log(logprefix + f'User {task["username"]} did not request an email.')


def handler(signal_received, frame):
    # Handle any cleanup here
    print('SIGINT or CTRL-C detected. Exiting')
    exit(0)


def log(msg, *args, **kwargs):
    dtnow = datetime.datetime.utcnow()
    line = f'{dtnow}  {msg}'
    print(line, *args, **kwargs)
    logfile = Path(logdir, f'fprunnerlog_{dtnow.year:4d}-{dtnow.month:02d}-{dtnow.day:02d}.txt')
    with logfile.open("a+") as logfile:
        logfile.write(line + '\n')


def do_taskloop():
    # track some kind of workload measurement for each user
    # that is reset after each complete pass
    usertaskload = {}

    conn = mysql.connector.connect(**CONNKWARGS)

    cur = conn.cursor(dictionary=True, buffered=True)

    cur.execute("SELECT COUNT(*) as taskcount FROM forcephot_task WHERE finishtimestamp IS NULL;")
    taskcount = cur.fetchone()['taskcount']

    if taskcount == 0:
        return 0
    else:
        log(f'Unfinished tasks in queue: {taskcount}')

    cur.execute(
        "SELECT t.*, a.email, a.username FROM forcephot_task AS t LEFT JOIN auth_user AS a"
        " ON user_id = a.id WHERE finishtimestamp IS NULL ORDER BY timestamp;")

    for taskrow in cur:
        task = dict(taskrow)

        if not task_exists(conn=conn, taskid=task['id']):
            log(f"task {task['id']} (user {task['username']}): was cancelled.")
            continue

        taskload_thisuser = usertaskload.get(task['user_id'], 0)

        if taskload_thisuser < USERTASKLOADLIMIT:
            logprefix = f"task{task['id']:05d}: "
            cur2 = conn.cursor()
            cur2.execute(f"UPDATE forcephot_task SET starttimestamp=UTC_TIMESTAMP() WHERE id={task['id']};")
            conn.commit()
            cur2.close()

            log(logprefix + f"Starting task for {task['username']} (who has run {taskload_thisuser} tasks "
                "in this pass so far):")
            log(logprefix + str(task))
            usertaskload[task['user_id']] = taskload_thisuser + 1

            runtask_starttime = time.perf_counter()

            localresultfile, error_msg = runtask(conn=conn, logprefix=logprefix, task=task)

            if not task_exists(conn=conn, taskid=task['id']):  # task was cancelled
                log(logprefix + "Task was cancelled during execution (no longer in database)")

                # in case a result file was created, delete it
                remove_task_resultfiles(taskid=task['id'], parent_task_id=task['parent_task_id'],
                                        request_type=task['request_type'])
            else:
                runtask_duration = time.perf_counter() - runtask_starttime

                log(logprefix + f"Task ran for {runtask_duration:.1f} seconds")

                if error_msg:
                    # an error occured and the task should not be retried (e.g. invalid
                    # minor planet center object name or no data returned)
                    log(logprefix + f"Error_msg: {error_msg}")

                    send_email_if_needed(conn=conn, task=task, logprefix=logprefix)

                    cur2 = conn.cursor()
                    cur2.execute(f"UPDATE forcephot_task SET finishtimestamp=UTC_TIMESTAMP(), error_msg='{error_msg}' WHERE id={task['id']};")
                    conn.commit()
                    cur2.close()
                else:
                    if localresultfile and os.path.exists(localresultfile):
                        # ingest_results(localresultfile, conn, use_reduced=task["use_reduced"])
                        send_email_if_needed(conn=conn, task=task, logprefix=logprefix)

                        cur2 = conn.cursor()
                        cur2.execute(f"UPDATE forcephot_task SET finishtimestamp=UTC_TIMESTAMP() WHERE id={task['id']};")
                        conn.commit()
                        cur2.close()
                    else:
                        waittime = 5
                        log(logprefix + f"ERROR: Task was not completed successfully. Waiting {waittime} seconds "
                            f"before continuing with next task...")
                        time.sleep(waittime)  # in case we're stuck in an error loop, wait a bit before trying again

            if (taskload_thisuser >= USERTASKLOADLIMIT):
                log(f"User {task['username']} has reached a task load of {taskload_thisuser} "
                    f"above limit {USERTASKLOADLIMIT} for this pass.")

    conn.commit()
    cur.close()

    conn.close()

    return taskcount


def rm_unassociated_files(conn, logprefix, start_maintenancetime, maxtime):
    log(logprefix + "Checking for unassociated result files...")

    taskid_list = get_taskid_list(conn)
    for resultfilepath in Path(localresultdir).glob('job*.*'):
        if resultfilepath.suffix in ['.txt', '.pdf', '.js', '.zip']:
            try:
                file_taskidstr = resultfilepath.stem[3:]
                file_taskid = int(file_taskidstr)
                # assert task_exists(conn=conn, taskid=file_taskid) == (file_taskid in taskid_list)
                if file_taskid not in taskid_list:
                    log(logprefix + f"Deleting unassociated result file {resultfilepath.relative_to(localresultdir)} "
                        f"because task {file_taskid} is not in the database")
                    resultfilepath.unlink()
                # elif resultfilepath.suffix == '.txt':
                #     if not os.path.exists(resultfilepath.with_suffix('.pdf')):  # result txt file without a PDF
                #         # load the text file to check if it contains any data rows to be plotted
                #         df = pd.read_csv(resultfilepath, delim_whitespace=True, escapechar='#', skipinitialspace=True)
                #         if not df.empty:
                #             log(logprefix + "Creating missing PDF from result file "
                #                 f"{resultfilepath.relative_to(localresultdir)}")
                #
                #             make_pdf_plot(taskid=file_taskid, localresultfile=resultfilepath, logprefix=logprefix,
                #                           logfunc=log, separate_process=True)

            except ValueError:
                # log(f"Could not understand task id of file {resultfilepath.relative_to(localresultdir)}")
                pass

        maintenance_duration = time.perf_counter() - start_maintenancetime
        if maxtime and maintenance_duration > maxtime:
            log(logprefix + f"Maintenance has run for {maintenance_duration:.0f} seconds "
                f"(above limit of {maxtime:.0f}). Resuming normal tasks...")
            break

    log(logprefix + "Finished checking for unassociated result files")


def do_maintenance(maxtime=None):
    start_maintenancetime = time.perf_counter()

    logprefix = "Maintenance: "

    conn = mysql.connector.connect(**CONNKWARGS)

    cur = conn.cursor(dictionary=True, buffered=True)

    cur.execute("SELECT COUNT(*) as taskcount FROM forcephot_task "
                "WHERE finishtimestamp < UTC_TIMESTAMP() - INTERVAL 30 DAY;")
    taskcount = cur.fetchone()['taskcount']
    log(logprefix + f"There are {taskcount} tasks that finished more than 30 days ago")

    conn.commit()
    cur.close()

    # this can get very slow
    # rm_unassociated_files(conn, logprefix, start_maintenancetime, maxtime)

    conn.close()


def main():
    signal(SIGINT, handler)

    logdir.mkdir(parents=True, exist_ok=True)

    log('Starting forcephotrunner...')

    last_maintenancetime = float('-inf')
    printedwaiting = False
    while True:
        # if (time.perf_counter() - last_maintenancetime) > 60 * 60:  # once per hour
        #     last_maintenancetime = time.perf_counter()
        #     do_maintenance(maxtime=300)
        #     printedwaiting = False

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
