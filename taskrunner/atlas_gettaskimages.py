#!/usr/bin/env python3
"""
This script is to be run on sc01. A job datafile is converted to a zip of fits images.
"""

import os
import math
import pandas as pd
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def mjd_to_date(mjd):
    """
    modified from https://gist.github.com/jiffyclub/1294443
    Convert Modified Julian Day to date.

    Algorithm from 'Practical Astronomy with your Calculator or Spreadsheet',
        4th ed., Duffet-Smith and Zwart, 2011.

    Parameters
    ----------
    mjd : float
        Modified Julian Day

    Returns
    -------
    year : int
        Year as integer. Years preceding 1 A.D. should be 0 or negative.
        The year before 1 A.D. is 0, 10 B.C. is year -9.

    month : int
        Month as integer, Jan = 1, Feb. = 2, etc.

    day : float
        Day, may contain fractional part.

    """
    jd = mjd + 2400000.5
    jd = jd + 0.5

    F, I = math.modf(jd)
    I = int(I)

    A = math.trunc((I - 1867216.25)/36524.25)

    if I > 2299160:
        B = I + 1 + A - math.trunc(A / 4.)
    else:
        B = I

    C = B + 1524
    D = math.trunc((C - 122.1) / 365.25)
    E = math.trunc(365.25 * D)
    G = math.trunc((C - E) / 30.6001)

    day = C - E + F - math.trunc(30.6001 * G)

    if G < 13.5:
        month = G - 1
    else:
        month = G - 13

    if month > 2.5:
        year = D - 4716
    else:
        year = D - 4715

    return f'{year:04d}-{month:02d}-{int(day):02d}'


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
    wpdatelines = ['#obs MJD obsdate wallpapersource wallpaperdate wallpaperdescription\n'] if not reduced else None

    for index, row in df[:500].iterrows():
        mjd = row['#MJD']
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
            wallpaperdesc = subprocess.check_output(
                f'fitshdr {fitsinput} -noquote -v WPDATE WPDIR', shell=True).decode("utf-8").strip()
            if not wallpaperdesc or len(wallpaperdesc) == 0:
                if mjd < 58417:
                    wallpaperdesc = "0000-00-00 'wallpaper 1 because MJD < 58417'"
                elif obs[-1] == 'c' and mjd < 58882:
                    wallpaperdesc = "2018-10-26 'wallpaper 2 because cyan filter and 58417 <= MJD < 58882'"
                elif obs[-1] == 'o' and mjd < 58884:
                    wallpaperdesc = "2018-10-26 'wallpaper 2 because cyan filter and 58417 <= MJD < 58884'"
                elif obs[-1] == 'c':
                    wallpaperdesc = "2020-02-03 'wallpaper 3 because cyan filter and 58882 <= MJD'"
                elif obs[-1] == 'o':
                    wallpaperdesc = "2020-02-05 'wallpaper 3 because orange filter and 58884 <= MJD'"
                wallpaperstatus = 'inferred_from_mjd_filter'
            else:
                wallpaperstatus = 'fits_header'

            obsdate = mjd_to_date(mjd)
            wpdatelines.append(f'{obs} {mjd:.6f} {obsdate} {wallpaperstatus} {wallpaperdesc}\n')

    commandfile = tmpfolder / 'commandlist.sh'
    with commandfile.open('w') as f:
        f.writelines(commands)

    # if not reduced:
    #     wpdatefile = tmpfolder / 'wallpapers.txt'
    #     with wpdatefile.open('w') as f:
    #         f.writelines(wpdatelines)

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

    os.system(f'cp {datafile} {tmpfolder}')

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
