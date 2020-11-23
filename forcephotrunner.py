#!/usr/bin/env python3

import os
# import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from signal import SIGINT, signal

import mysql.connector
import pandas as pd
from django.conf import settings
from django.core.mail import EmailMessage
from dotenv import load_dotenv

import atlasserver.settings as djangosettings

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


def get_localresultfile(id):
    filename = f'job{id:05d}.txt'
    return Path(localresultdir, filename)


def runforced(id, ra, dec, mjd_min=50000, mjd_max=60000, email=None, **kwargs):
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
    if kwargs['use_reduced']:
        atlascommand += " red=1"
    atlascommand += " dodb=1 parallel=16"

    # for debugging because force.sh takes a long time to run
    # atlascommand = "echo '(DEBUG MODE: force.sh output will be here)'"

    atlascommand += " | sort -n"
    atlascommand += f" | tee {remoteresultfile}"

    log(f"Executing on {remoteServer}: {atlascommand}")

    p = subprocess.Popen(["ssh", f"{remoteServer}", atlascommand],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding='utf-8', bufsize=1, universal_newlines=True)

    starttime = time.perf_counter()
    while True:
        try:
            p.communicate(timeout=5)

        except subprocess.TimeoutExpired:
            log(f"ssh has been running for {time.perf_counter() - starttime:.1f} seconds", end='\r')

        else:
            break
    stdout, stderr = p.communicate()
    log(f'\nssh ran for {time.perf_counter() - starttime:.1f} seconds')

    if stdout:
        stdoutlines = stdout.split('\n')
        log(f"{remoteServer} STDOUT: ({len(stdoutlines)} lines of output)")
        # for line in stdoutlines:
        #     log(f"{remoteServer} STDOUT: {line}")

    if stderr:
        for line in stderr.split('\n'):
            log(f"{remoteServer} STDERR: {line}")

    # output realtime ssh output line by line
    # while True:
    #     stdoutline = p.stdout.readline()
    #     stderrline = p.stderr.readline()
    #     if not stdoutline and not stderrline:
    #         break
    #     if stdoutline:
    #         log(f"{remoteServer} STDOUT >>> {stdoutline.rstrip()}")
    #     if stderrline:
    #         log(f"{remoteServer} STDERR >>> {stderrline.rstrip()}")

    copycommand = f'scp {remoteServer}:{remoteresultfile} "{localresultfile}"'
    log(copycommand)

    p = subprocess.Popen(copycommand,
                         shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding='utf-8', bufsize=1, universal_newlines=True)
    stdout, stderr = p.communicate()

    if stdout:
        for line in stdout.split('\n'):
            log(f"STDOUT: {line}")

    if stderr:
        for line in stderr.split('\n'):
            log(f"STDERR: {line}")

        # task failed
        return False

    if not os.path.exists(localresultfile):
        # task failed
        return False

    return localresultfile


def send_possible_email(conn, task):
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
            if not batchtask['finished'] and batchtask['id'] != task['id']:
                batchtasks_unfinished += 1
            else:
                taskdesclist.append(
                    f"RA {batchtask['ra']} Dec {batchtask['dec']} "
                    f"use_reduced {'yes' if batchtask['use_reduced'] else 'no'} "
                    f"jobid {batchtask['id']}")
                localresultfilelist.append(get_localresultfile(batchtask['id']))
        conn.commit()
        cur3.close()

        if batchtasks_unfinished == 0:
            log(f'Sending email to {task["email"]} containing {batchtaskcount} tasks')

            message = EmailMessage(
                subject='ATLAS forced photometry results',
                body=(
                    'Your forced photometry results are attached for:\n\n'
                    + '\n'.join(taskdesclist) + '\n\n'),
                from_email=os.environ.get('EMAIL_HOST_USER'),
                to=[task["email"]],
            )
            for localresultfile in localresultfilelist:
                message.attach_file(localresultfile)
            message.send()
        else:
            log(f'Waiting to send email until remaining {batchtasks_unfinished} '
                f'of {batchtaskcount} batched tasks are finished.')
    elif task["send_email"]:
        log(f'User {task["username"]} has no email address.')
    else:
        log(f'User {task["username"]} did not request an email.')


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


def main():
    signal(SIGINT, handler)

    # with sqlite3.connect('db.sqlite3') as conn:
    #     conn.row_factory = sqlite3.Row

    # set a limit on the number of tasks run for each user per full pass of the task queue
    # this prevents a single user from monopolising a large block of the queue
    usertaskloadlimit = 1

    while True:
        # track some kind of workload measurement for each user
        # that is reset after each complete pass
        usertaskload = {}
        conn = mysql.connector.connect(**CONNKWARGS)

        cur = conn.cursor(dictionary=True, buffered=True)

        # SQLite version
        # taskcount = cur.execute("SELECT COUNT(*) FROM forcephot_task WHERE finished=false;").fetchone()[0]

        cur.execute("SELECT COUNT(*) as taskcount FROM forcephot_task WHERE finished=false;")
        taskcount = cur.fetchone()['taskcount']

        if taskcount == 0:
            log(f'Waiting for tasks', end='\r')
        else:
            log(f'Unfinished jobs in queue: {taskcount}')

        cur.execute(
            "SELECT t.*, a.email, a.username FROM forcephot_task AS t LEFT JOIN auth_user AS a"
            " ON user_id = a.id WHERE finished=false ORDER BY timestamp;")

        for taskrow in cur:
            task = dict(taskrow)

            taskload_thisuser = usertaskload.get(task['user_id'], 0)

            if taskload_thisuser < usertaskloadlimit:
                log(f"Starting job for {task['email']} (who has {taskload_thisuser} tasks run in this pass so far):")
                log(task)
                usertaskload[task['user_id']] = taskload_thisuser + 1

                runforced_starttime = time.perf_counter()

                localresultfile = runforced(**task)

                runforced_duration = time.perf_counter() - runforced_starttime

                localresultfile = get_localresultfile(task['id'])
                if localresultfile and os.path.exists(localresultfile):
                    # ingest_results(localresultfile, conn, use_reduced=task["use_reduced"])
                    send_possible_email(conn, task)

                    cur2 = conn.cursor()
                    cur2.execute(f"UPDATE forcephot_task SET finished=true, finishtimestamp=NOW() "
                                 f"WHERE id={task['id']};")
                    conn.commit()
                    cur2.close()
                else:
                    waittime = 10
                    log(f"ERROR: Task not completed successfully. Waiting {waittime} seconds before retrying...")
                    time.sleep(waittime)  # in case we're stuck in an error loop, wait a bit before trying again
            else:
                log(f"User {task['email']} has reached a task load of {taskload_thisuser} "
                    f"above limit {usertaskload[task['user_id']]} for this pass. Postponing jobid {task['id']} "
                    f"until next pass.")

        if taskcount == 0:
            time.sleep(5)

        conn.commit()
        cur.close()

        conn.close()


if __name__ == '__main__':
    main()
