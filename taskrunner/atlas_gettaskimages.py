#!/usr/bin/env python3
"""
This script is to be run on sc01. A job datafile is converted to a zip of fits images.
"""

import os
import pandas as pd
from pathlib import Path
import shutil
import sys
import tempfile


def main():
    if len(sys.argv) != 3:
        print("ERROR: exactly two argument must be specified: [DATAFILE] ['red' or 'diff']")
        sys.exit(1)
        return

    datafile = sys.argv[1]
    reduced = (sys.argv[2] == 'red')
    df = pd.read_csv(datafile, delim_whitespace=True, escapechar='#')

    tmpfolder = Path(tempfile.mkdtemp())
    rowcount = len(df)
    commands = []
    for index, row in df[:500].iterrows():
        obs = row['Obs']  # looks like '01a59309o0235c'
        imgfolder = 'red' if reduced else 'diff'
        fitsext = 'fits' if reduced else 'diff'
        fitsinput = f'/atlas/{imgfolder}/{obs[:3]}/{obs[3:8]}/{obs}.{fitsext}.fz'  # assumes diff image not reduced
        fitsoutpath = Path(tmpfolder / f'{obs}.fits')
        commands.append(
            "/atlas/vendor/monsta/bin/monsta /atlas/lib/monsta/subarray.pro "
            f"{fitsinput} {fitsoutpath} "
            f"$(/atlas/bin/pix2sky -sky2pix {fitsinput} {row['RA']} {row['Dec']}) 101"
            f"; echo {index:04d}/{rowcount}: {obs}"
            "\n"
        )

    commandfile = tmpfolder / 'commands.txt'
    with commandfile.open('w') as f:
        f.writelines(commands)

    print(commandfile)

    os.system(f'parallel --jobs 32 < {commandfile}')

    zipoutpath = Path(datafile).with_suffix('.zip').resolve()

    # make sure the zip file doesn't somehow exist already
    if zipoutpath.exists():
        zipoutpath.unlink()

    cmd = f"zip --junk-paths -r {zipoutpath} {tmpfolder}"
    print(cmd)
    os.system(cmd)

    shutil.rmtree(tmpfolder)


if __name__ == '__main__':
    main()
