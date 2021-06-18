#!/usr/bin/env python3
"""
This script is to be run on sc01. A job datafile is converted to a zip of fits images.
"""

import os
import pandas as pd
from pathlib import Path
import shutil
import subprocess
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

    firstfitsoutpath_c = None
    firstfitsoutpath_o = None
    tmpfolder = Path(tempfile.mkdtemp())
    rowcount = len(df)
    commands = []
    wpdatelines = ['#obs wallpaperdate wallpaperdir\n'] if not reduced else None

    for index, row in df[:500].iterrows():
        obs = row['Obs']  # looks like '01a59309o0235c'
        imgfolder = 'red' if reduced else 'diff'  # difference or reduced image
        fitsext = 'fits' if reduced else 'diff'
        fitsinput = f'/atlas/{imgfolder}/{obs[:3]}/{obs[3:8]}/{obs}.{fitsext}.fz'
        fitsoutpath = Path(tmpfolder / (f'{obs}.fits' if reduced else f'{obs}_diff.fits'))

        if firstfitsoutpath_c is None and obs.endswith('c'):
            firstfitsoutpath_c = fitsoutpath

        if firstfitsoutpath_o is None and obs.endswith('o'):
            firstfitsoutpath_o = fitsoutpath

        commands.append(
            f"echo Image {index + 1:04d} of {rowcount}: {obs}; "
            "/atlas/vendor/monsta/bin/monsta /atlas/lib/monsta/subarray.pro "
            f"{fitsinput} {fitsoutpath} "
            f"$(/atlas/bin/pix2sky -sky2pix {fitsinput} {row['RA']} {row['Dec']}) 200"
            "\n"
        )

        if not reduced:
            wpdatedir = subprocess.check_output(
                f'fitshdr {fitsinput} -noquote -v WPDATE WPDIR', shell=True).decode("utf-8").strip()

            wpdatelines.append(f'{obs} {wpdatedir}\n')

    commandfile = tmpfolder / 'commandlist.sh'
    with commandfile.open('w') as f:
        f.writelines(commands)

    if not reduced:
        wpdatefile = tmpfolder / 'wallpaperdates.txt'
        with wpdatefile.open('w') as f:
            f.writelines(wpdatelines)

    os.system(f'parallel --jobs 32 < {commandfile}')

    for firstfitsoutpath in [firstfitsoutpath_c, firstfitsoutpath_o]:
        if firstfitsoutpath is not None:
            origext = '.fits' if reduced else '_diff.fits'
            refoutputpath = str(firstfitsoutpath).replace(origext, '_ref.fits')
            command_getref = f'/atlas/bin/wpwarp2 -novar -nomask -nozerosat -wp {refoutputpath} {firstfitsoutpath}\n'
            with commandfile.open('a') as f:
                f.write(command_getref)
            print(command_getref)
            os.system(command_getref)

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
