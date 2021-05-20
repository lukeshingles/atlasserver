#!/usr/bin/env python3
"""
This script is to be run on sc01. A job datafile is used to get the first images in JPEG
"""

import os
import pandas as pd
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 3:
        print("ERROR: exactly two argument must be specified: [DATAFILE] ['red' or 'diff']")
        sys.exit(1)
        return

    datafile = sys.argv[1]
    reduced = (sys.argv[2] == 'red')

    if not os.path.exists(datafile):
        return

    df = pd.read_csv(datafile, delim_whitespace=True, escapechar='#')

    if df.empty:
        return

    row = df.iloc[0]
    obs = row['Obs']  # looks like '01a59309o0235c'
    imgfolder = 'red' if reduced else 'diff'  # difference or reduced image
    fitsext = 'fits' if reduced else 'diff'
    fitsinput = f'/atlas/{imgfolder}/{obs[:3]}/{obs[3:8]}/{obs}.{fitsext}.fz'
    fitsoutpath = Path(datafile).with_suffix('.fits')
    os.system(
        "/atlas/vendor/monsta/bin/monsta /atlas/lib/monsta/subarray.pro "
        f"{fitsinput} {fitsoutpath} "
        f"$(/atlas/bin/pix2sky -sky2pix {fitsinput} {row['RA']} {row['Dec']}) 100"
        "\n"
    )

    # delete the .fits (but keep the .jpeg)
    if fitsoutpath.exists():
        fitsoutpath.unlink()

    # jobxxxxx.fits.jpg to jobxxxx.first.jpg
    fitsoutpath.with_suffix('.fits.jpg').rename(Path(datafile).with_suffix('.jpg'))


if __name__ == '__main__':
    main()
