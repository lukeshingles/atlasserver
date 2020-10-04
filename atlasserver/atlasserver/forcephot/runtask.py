import os
import sqlite3
import subprocess
import sys

from pathlib import Path

remoteServer = 'atlas'
localresultdir = Path('results')


def runforced(id, ra, dec, mjd_min=50000, mjd_max=60000, **kwargs):
    filename = f'job{id:05d}.txt'
    print(filename)
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
    # atlascommand = "echo '(force.sh output will be here)'"

    atlascommand += f"> {remoteresultfile}"

    print(atlascommand)

    p = subprocess.Popen(["ssh", f"{remoteServer}", atlascommand],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding='utf-8')
    output, errors = p.communicate()

    print(output, errors)

    copycommand = f"scp {remoteServer}:{remoteresultfile} {localresultfile}"
    print(copycommand)
    p = subprocess.Popen(copycommand,
                         shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    output, errors = p.communicate()

    print(output, errors)

    return localresultfile


def main():
    # runforced("job00000", 347.38792, 15.65928, mjd_min=57313, mjd_max=57314)
    conn = None
    try:
        conn = sqlite3.connect('../../db.sqlite3')
    except Error as e:
        print(e)

    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM forcephot_forcephottask WHERE finished=false ORDER BY timestamp DESC;")

    jobrow = dict(cur.fetchone())

    print(jobrow)

    runforced(**jobrow)

    return


if __name__ == '__main__':
    main()
