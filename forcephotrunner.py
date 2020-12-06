#!/usr/bin/env python3

import fundamentals
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from signal import SIGINT, SIGTERM, signal

import mysql.connector
import pandas as pd
from django.conf import settings
from django.core.mail import EmailMessage
from dotenv import load_dotenv

import atlasserver.settings as djangosettings
import plotatlasfp

load_dotenv(override=True)

remoteServer = 'atlas'
localresultdir = Path(djangosettings.STATIC_ROOT, 'results')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'atlasserver.settings')

CONNKWARGS = {
    'host': djangosettings.DATABASES['default']['HOST'],
    'port': djangosettings.DATABASES['default']['PORT'],
    'database': djangosettings.DATABASES['default']['NAME'],
    'user': djangosettings.DATABASES['default']['USER'],
    'password': djangosettings.DATABASES['default']['PASSWORD'],
    'autocommit': True,
}


# set a limit on the number of tasks run for each user per full pass of the task queue
# this prevents a single user from monopolising a large block of the queue
USERTASKLOADLIMIT = 1


def get_localresultfile(id):
    filename = f'job{id:05d}.txt'
    return Path(localresultdir, filename)


def task_exists(conn, taskid):
    cur = conn.cursor(dictionary=True)
    cur.execute(f"SELECT COUNT(*) as taskcount FROM forcephot_task WHERE id={taskid};")
    exists = not (cur.fetchone()['taskcount'] == 0)
    conn.commit()
    cur.close()
    return exists


def remove_task_resultfiles(taskid):
    taskfiles = []  # possible result files that don't necessarily exist

    localresultfile = get_localresultfile(taskid)
    taskfiles.append(localresultfile)
    pdfpath = Path(localresultfile).with_suffix('.pdf')
    taskfiles.append(pdfpath)

    for taskfile in taskfiles:
        if os.path.exists(taskfile):
            try:
                os.remove(taskfile)
            except OSError:
                log(f"Error deleting file: {Path(taskfile).relative_to(localresultdir)}")
            else:
                log(f"Deleted {Path(taskfile).relative_to(localresultdir)}")


def runforced(task, conn, logprefix='', **kwargs):
    id = task['id']
    ra = task['ra']
    dec = task['dec']
    mjd_min = task['mjd_min']
    mjd_max = task['mjd_max']

    filename = f'job{id:05d}.txt'

    remoteresultdir = Path('~/atlasserver/results/')
    remoteresultfile = Path(remoteresultdir, filename)

    localresultfile = get_localresultfile(id)
    localresultdir.mkdir(parents=True, exist_ok=True)

    atlascommand = "nice -n 19 "
    atlascommand += f"/atlas/bin/force.sh {float(ra)} {float(dec)}"
    if mjd_min:
        atlascommand += f" m0={float(mjd_min)}"
    if mjd_max:
        atlascommand += f" m1={float(mjd_max)}"
    if task['use_reduced']:
        atlascommand += " red=1"
    atlascommand += " dodb=1 parallel=16"

    # for debugging because force.sh takes a long time to run
    # atlascommand = "echo '(DEBUG MODE: force.sh output will be here)'"

    atlascommand += " | sort -n"
    atlascommand += f" | tee {remoteresultfile}"

    log(logprefix + f"Executing on {remoteServer}: {atlascommand}")

    p = subprocess.Popen(["ssh", f"{remoteServer}", atlascommand],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding='utf-8', bufsize=1, universal_newlines=True)

    starttime = time.perf_counter()
    cancelled = False
    while not cancelled:
        try:
            p.communicate(timeout=2)

        except subprocess.TimeoutExpired:
            cancelled = not task_exists(conn=conn, taskid=id)
            log(logprefix + f"ssh has been running for {time.perf_counter() - starttime:.1f} seconds        ", end='\r')
        else:
            break

    if cancelled:
        log(logprefix + "                                                                                 ")
        os.kill(p.pid, SIGTERM)
        return False

    stdout, stderr = p.communicate()
    log(logprefix + f'ssh ran for {time.perf_counter() - starttime:.1f} seconds                                       ')

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

    if not task_exists(conn=conn, taskid=id):  # check if job was cancelled
        return False

    copycommand = f'scp {remoteServer}:{remoteresultfile} "{localresultfile}"'
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

        # task failed
        return False

    if not os.path.exists(localresultfile):
        # task failed somehow
        return False

    make_pdf_plot(taskid=task['id'], taskcomment=task['comment'], localresultfile=localresultfile, logprefix=logprefix)

    return localresultfile


def send_email_if_needed(conn, task, logprefix=''):
    if task["send_email"] and task["email"]:
        # if we find an unfinished task in the same batch, hold off sending the email
        # same batch here is defined as being queue by the same user within a few seconds of each other
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
                taskurl = f"https://fallingstar-data.com/forcedphot/queue/{task['id']}/"
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


def make_pdf_plot(localresultfile, taskid, taskcomment='', logprefix=''):
    epochs = plotatlasfp.read_and_simga_clip_data(log=fundamentals.logs.emptyLogger(), fpFile=localresultfile)

    pdftitle = f"Task {taskid} {(':' + taskcomment) if taskcomment else ''}"
    temp_plot_path = plotatlasfp.plot_lc(log=fundamentals.logs.emptyLogger(), epochs=epochs,
                                         objectName=pdftitle, stacked=False)

    if not temp_plot_path:
        log(logprefix + f'Problem creating PDF plot from {localresultfile.relative_to(localresultdir)}')
        return None

    pdfpath = localresultfile.with_suffix('.pdf')
    Path(temp_plot_path).rename(pdfpath)

    log(logprefix + f'Created plot file {pdfpath.relative_to(localresultdir)}')

    return pdfpath


def ingest_results(localresultfile, conn, use_reduced=False):
    df = pd.read_csv(localresultfile, delim_whitespace=True, escapechar='#', skipinitialspace=True)
    # df.rename(columns={'#MJD': 'MJD'})
    cur = conn.cursor()
    for _, pdrow in df.iterrows():
        # print(pdrow.keys())
        rowdict = {}
        rowdict['timestamp'] = 'now()'
        rowdict['mjd'] = str(pdrow['#MJD'])
        rowdict['m'] = str(pdrow['m'])
        rowdict['dm'] = str(pdrow['dm'])
        rowdict['ujy'] = str(pdrow['uJy'])
        rowdict['dujy'] = str(pdrow['duJy'])
        rowdict['filter'] = f"'{pdrow['F']}'"
        rowdict['err'] = str(pdrow['err'])
        rowdict['chi_over_n'] = str(pdrow['chi/N'])
        rowdict['ra'] = str(pdrow['RA'])
        rowdict['declination'] = str(pdrow['Dec'])
        rowdict['x'] = str(pdrow['x'])
        rowdict['y'] = str(pdrow['y'])
        rowdict['maj'] = str(pdrow['maj'])
        rowdict['min'] = str(pdrow['min'])
        rowdict['phi'] = str(pdrow['phi'])
        rowdict['apfit'] = str(pdrow['apfit'])
        rowdict['sky'] = str(pdrow['Sky'])
        rowdict['zp'] = str(pdrow['ZP'])
        rowdict['obs'] = f"'{pdrow['Obs']}'"
        rowdict['use_reduced'] = "1" if use_reduced else "0"

        strsql = f'INSERT INTO forcephot_result ({",".join(rowdict.keys())}) VALUES ({",".join(rowdict.values())});'
        cur.execute(strsql)

    conn.commit()
    cur.close()


def handler(signal_received, frame):
    # Handle any cleanup here
    print('SIGINT or CTRL-C detected. Exiting gracefully')
    exit(0)


def log(msg, *args, **kwargs):
    print(f'{datetime.utcnow()}  {msg}', *args, **kwargs)


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
        log(f'Unfinished jobs in queue: {taskcount}')

    cur.execute(
        "SELECT t.*, a.email, a.username FROM forcephot_task AS t LEFT JOIN auth_user AS a"
        " ON user_id = a.id WHERE finishtimestamp IS NULL ORDER BY timestamp;")

    for taskrow in cur:
        task = dict(taskrow)

        if not task_exists(conn=conn, taskid=task['id']):
            log("job{task['id']:05d} (user {task['user_id']}): was cancelled.")
            continue

        taskload_thisuser = usertaskload.get(task['user_id'], 0)

        if taskload_thisuser < USERTASKLOADLIMIT:
            logprefix = f"job{task['id']:05d}: "
            cur2 = conn.cursor()
            cur2.execute(f"UPDATE forcephot_task SET starttimestamp=NOW() WHERE id={task['id']};")
            conn.commit()
            cur2.close()

            log(logprefix + f"Starting job for {task['email']} (who has run {taskload_thisuser} tasks "
                "in this pass so far):")
            log(logprefix + str(task))
            usertaskload[task['user_id']] = taskload_thisuser + 1

            runforced_starttime = time.perf_counter()

            localresultfile = runforced(conn=conn, logprefix=logprefix, task=task)

            if not task_exists(conn=conn, taskid=task['id']):  # job was cancelled
                log(logprefix + "Task was cancelled (no longer in database)")

                # in case a result file was created, delete it
                if localresultfile and os.path.exists(localresultfile):
                    remove_task_resultfiles(taskid=task['id'])
            else:
                runforced_duration = time.perf_counter() - runforced_starttime

                log(logprefix + f"Task took {runforced_duration:.1f} seconds to complete")

                localresultfile = get_localresultfile(task['id'])
                if localresultfile and os.path.exists(localresultfile):
                    # ingest_results(localresultfile, conn, use_reduced=task["use_reduced"])
                    send_email_if_needed(conn=conn, task=task, logprefix=logprefix)

                    cur2 = conn.cursor()
                    cur2.execute(f"UPDATE forcephot_task SET finishtimestamp=NOW() WHERE id={task['id']};")
                    conn.commit()
                    cur2.close()
                else:
                    waittime = 10
                    log(logprefix + f"ERROR: Task not completed successfully. Waiting {waittime} seconds "
                        f"before retrying...")
                    time.sleep(waittime)  # in case we're stuck in an error loop, wait a bit before trying again

                if (taskload_thisuser >= USERTASKLOADLIMIT):
                    log(f"User {task['email']} has reached a task load of {taskload_thisuser} "
                        f"above limit {usertaskload[task['user_id']]} for this pass.")

    conn.commit()
    cur.close()

    conn.close()

    return taskcount


def do_maintenance(maxtime=None):
    start_maintenancetime = time.perf_counter()

    logprefix = "Maintenance: "

    conn = mysql.connector.connect(**CONNKWARGS)

    cur = conn.cursor(dictionary=True, buffered=True)

    cur.execute("SELECT COUNT(*) as taskcount FROM forcephot_task WHERE finishtimestamp < NOW() - INTERVAL 30 DAY;")
    taskcount = cur.fetchone()['taskcount']
    log(logprefix + f"There are {taskcount} tasks that finished more than 30 days ago")

    conn.commit()
    cur.close()

    for resultfilepath in Path(localresultdir).glob('job*.*'):
        if resultfilepath.suffix in ['.txt', '.pdf']:
            try:
                taskidstr = resultfilepath.stem[4:]
                taskid = int(taskidstr)
                if not task_exists(conn=conn, taskid=taskid):
                    log(logprefix + f"Deleting unassociated result file {resultfilepath.relative_to(localresultdir)}")
                    remove_task_resultfiles(taskid)
                elif resultfilepath.suffix == '.txt':
                    if not os.path.exists(resultfilepath.with_suffix('.pdf')):  # result txt file without a PDF
                        log(logprefix + "Creating missing PDF from result file "
                            f"{resultfilepath.relative_to(localresultdir)}")
                        make_pdf_plot(taskid=taskid, localresultfile=resultfilepath, logprefix=logprefix)

            except ValueError:
                # log(f"Could not understand task id of file {resultfilepath.relative_to(localresultdir)}")
                pass

        maintenance_duration = time.perf_counter() - start_maintenancetime
        if maxtime and maintenance_duration > maxtime:
            log(logprefix + f"Maintenance has run for {maintenance_duration:.0f} s "
                f"(above limit of {maxtime:.0f}). Resuming normal tasks...")
            break

    conn.close()


def main():
    signal(SIGINT, handler)

    last_maintenancetime = float('-inf')
    printedwaiting = False
    while True:
        if (time.perf_counter() - last_maintenancetime) > 60 * 60:  # once per hour
            last_maintenancetime = time.perf_counter()
            do_maintenance(maxtime=60)
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
