#!/usr/bin/env python3
import os
import sqlite3
import subprocess
import sys
import time

from datetime import datetime
from pathlib import Path
from signal import signal, SIGINT

remoteServer = 'atlas'
localresultdir = Path('results')


def runforced(id, ra, dec, mjd_min=50000, mjd_max=60000, **kwargs):
    filename = f'job{id:05d}.txt'
    remoteresultdir = Path('/home/shingles/atlasserver/jobresults/')
    remoteresultfile = Path(remoteresultdir, filename)
    localresultfile = Path(localresultdir, filename)

    atlascommand = "nice -n 19 "
    atlascommand += f"/atlas/bin/force.sh {float(ra)} {float(dec)}"
    if mjd_min:
        atlascommand += f" m0={float(mjd_min)}"
    if mjd_max:
        atlascommand += f" m1={float(mjd_max)}"
    atlascommand += " parallel=10"

    # for debugging because force.sh takes a long time to run
    # atlascommand = "echo '(DEBUG MODE: force.sh output will be here)'"

    atlascommand += f" | tee {remoteresultfile}"

    log(f"Executing on {remoteServer}: {atlascommand}")

    p = subprocess.Popen(["ssh", f"{remoteServer}", atlascommand],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding='utf-8')

    while True:
        line = p.stdout.readline()
        if not line:
            break
        log(f"{remoteServer} >>> {line.rstrip()}")

    stdout, stderr = p.communicate()

    if stdout:
        log(f"{remoteServer} STDOUT: {stdout}")
    if stderr:
        log(f"{remoteServer} STDERR: {stderr}")

    copycommand = f"scp {remoteServer}:{remoteresultfile} {localresultfile}"
    log(copycommand)
    p = subprocess.Popen(copycommand,
                         shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    stdout, stderr = p.communicate()

    if stdout:
        log(f"STDOUT: {stdout}")
    if stderr:
        log(f"STDERR: {stderr}")
        return False

    if not os.path.exists(localresultfile):
        return False

    return localresultfile


def handler(signal_received, frame):
    # Handle any cleanup here
    print('SIGINT or CTRL-C detected. Exiting gracefully')
    exit(0)


def log(msg, *args):
    print(f'{datetime.utcnow()}: {msg}', *args)


def main():
    signal(SIGINT, handler)
    # runforced("job00000", 347.38792, 15.65928, mjd_min=57313, mjd_max=57314)

    with sqlite3.connect('../../db.sqlite3') as conn:

        conn.row_factory = sqlite3.Row

        cur = conn.cursor()

        # DEBUG: mark all jobs as unfinished
        cur.execute(f"UPDATE forcephot_forcephottask SET finished=false")

        while True:
            taskcount = cur.execute("SELECT COUNT(*) FROM forcephot_forcephottask WHERE finished=false;").fetchone()[0]
            log(f'Unfinished jobs in queue: {taskcount}')

            cur.execute("SELECT * FROM forcephot_forcephottask WHERE finished=false ORDER BY timestamp ASC;")

            for row in cur:
                task = dict(row)
                log("Starting job", task)
                taskid = task['id']

                if runforced(**task):
                    cur2 = conn.cursor()
                    cur2.execute(f"UPDATE forcephot_forcephottask SET finished=true WHERE id={taskid}")
                    log(f"Completed task id {taskid}")
                else:
                    log("ERROR: Task not completed.")

            time.sleep(5)

    return


if __name__ == '__main__':
    main()
