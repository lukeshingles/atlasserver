#!/usr/bin/env python3
import os
import sqlite3
import subprocess
import sys
import time

from django.conf import settings
from django.core.mail import EmailMessage
from datetime import datetime
from pathlib import Path
from signal import signal, SIGINT

remoteServer = 'atlas'
localresultdir = Path('forcephot', 'static', 'results')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'atlasserver.settings')


def runforced(id, ra, dec, mjd_min=50000, mjd_max=60000, email=None, **kwargs):
    filename = f'job{id:05d}.txt'
    remoteresultdir = Path('~/atlasserver/results/')
    remoteresultfile = Path(remoteresultdir, filename)
    localresultfile = Path(localresultdir, filename)

    atlascommand = "nice -n 19 "
    atlascommand += f"/atlas/bin/force.sh {float(ra)} {float(dec)}"
    if mjd_min:
        atlascommand += f" m0={float(mjd_min)}"
    if mjd_max:
        atlascommand += f" m1={float(mjd_max)}"
    if kwargs['use_reduced']:
        atlascommand += " red=1"
    atlascommand += " dodb=1 parallel=2"

    # for debugging because force.sh takes a long time to run
    # atlascommand = "echo '(DEBUG MODE: force.sh output will be here)'"

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
            log(f"ssh has been running for {time.perf_counter() - starttime:.1f} seconds")

        else:
            break

    stdout, stderr = p.communicate()

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

    copycommand = f"scp {remoteServer}:{remoteresultfile} {localresultfile}"
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


def handler(signal_received, frame):
    # Handle any cleanup here
    print('SIGINT or CTRL-C detected. Exiting gracefully')
    exit(0)


def log(msg, *args, **kwargs):
    print(f'{datetime.utcnow()}  {msg}', *args, **kwargs)


def main():
    signal(SIGINT, handler)
    # runforced("job00000", 347.38792, 15.65928, mjd_min=57313, mjd_max=57314)

    with sqlite3.connect('db.sqlite3') as conn:

        conn.row_factory = sqlite3.Row

        cur = conn.cursor()

        # DEBUG: mark all jobs as unfinished
        # cur.execute(f"UPDATE forcephot_task SET finished=false;")
        # conn.commit()

        while True:
            taskcount = cur.execute("SELECT COUNT(*) FROM forcephot_task WHERE finished=false;").fetchone()[0]
            if taskcount == 0:
                log(f'Waiting for tasks', end='\r')
            else:
                log(f'Unfinished jobs in queue: {taskcount}')

            cur.execute(
                "SELECT t.*, a.email from forcephot_task as t LEFT JOIN auth_user as a"
                " on user_id = a.id WHERE finished=false ORDER BY timestamp ASC LIMIT 1;")

            for taskrow in cur:
                task = dict(taskrow)
                log("Starting job", task)
                taskid = task['id']

                if localresultfile := runforced(**task):
                    log(f'Sending email to {task["email"]} containing {localresultfile}')

                    message = EmailMessage(
                        subject='ATLAS forced photometry results',
                        body=f'Your forced photometry results for RA {task["ra"]} DEC {task["dec"]} are attached.\n\n',
                        from_email='luke.shingles+alas@gmail.com',
                        to=[task["email"]],
                    )
                    message.attach_file(localresultfile)
                    message.send()

                    cur2 = conn.cursor()
                    cur2.execute(f"UPDATE forcephot_task SET finished=true WHERE id={taskid};")
                    conn.commit()
                    cur2.close()
                else:
                    log("ERROR: Task not completed successfully.")

            if taskcount == 0:
                time.sleep(5)

        cur.close()

    return


if __name__ == '__main__':
    main()
