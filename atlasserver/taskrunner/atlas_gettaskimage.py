#!/usr/bin/env python3
"""Input a job data file and produce the first (or brightest) image in JPEG. This script is to be run on sc01."""
import os
import sys
from pathlib import Path

import pandas as pd


def main() -> None:
    if len(sys.argv) != 3:
        print("ERROR: exactly two argument must be specified: [DATAFILE] ['red' or 'diff']")
        sys.exit(1)
        return

    datafile = sys.argv[1]
    reduced = sys.argv[2] == "red"

    if not Path(datafile).exists():
        return

    dfforcedphot = pd.read_csv(datafile, delim_whitespace=True, escapechar="#")

    if dfforcedphot.empty:
        return

    row = dfforcedphot[dfforcedphot.uJy == dfforcedphot.uJy.max()].iloc[0]  # brightest data point
    obs = row["Obs"]  # looks like '01a59309o0235c'
    imgfolder = "red" if reduced else "diff"  # difference or reduced image
    fitsext = "fits" if reduced else "diff"
    fitsinput = f"/atlas/{imgfolder}/{obs[:3]}/{obs[3:8]}/{obs}.{fitsext}.fz"
    fitsoutpath = Path(datafile).with_suffix(".fits")
    os.system(
        "/atlas/vendor/monsta/bin/monsta /atlas/lib/monsta/subarray.pro "
        f"{fitsinput} {fitsoutpath} "
        f"$(/atlas/bin/pix2sky -sky2pix {fitsinput} {row['RA']} {row['Dec']}) 100"
        "\n"
    )

    # delete the .fits (but keep the .jpeg)
    if fitsoutpath.exists():
        fitsoutpath.unlink()

    # jobxxxxx.fits.jpg to jobxxxx.jpg
    fitsoutpath.with_suffix(".fits.jpg").rename(Path(datafile).with_suffix(".jpg"))


if __name__ == "__main__":
    main()
