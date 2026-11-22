#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["pandas"]
# ///
"""Input a job data file and produce the first (or brightest) image in JPEG. This script is to be run on sc01.

The task runner starts this script on sc01 with "uv run --script". uv reads the Python version
and the packages from the block above.
"""

import subprocess
import sys
from pathlib import Path

import pandas as pd


def main() -> None:
    if len(sys.argv) != 3:
        print("ERROR: exactly two argument must be specified: [DATAFILE] ['red' or 'diff']")
        sys.exit(1)

    datafile = sys.argv[1]
    reduced = sys.argv[2] == "red"

    if not Path(datafile).exists():
        return

    dfforcedphot = pd.read_csv(datafile, sep=r"\s+", escapechar="#").dropna()

    if dfforcedphot.empty:
        return

    row = dfforcedphot[dfforcedphot.uJy == dfforcedphot.uJy.max()].iloc[0]  # brightest data point
    obs = row["Obs"]  # looks like '01a59309o0235c'
    imgfolder = "red" if reduced else "diff"  # difference or reduced image
    fitsext = "fits" if reduced else "diff"
    fitsinput = f"/atlas/{imgfolder}/{obs[:3]}/{obs[3:8]}/{obs}.{fitsext}.fz"
    fitsoutpath = Path(datafile).with_suffix(".fits")
    pixcoords = subprocess.run(
        ["/atlas/bin/pix2sky", "-sky2pix", fitsinput, str(row["RA"]), str(row["Dec"])],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.split()
    subprocess.run(
        [
            "/atlas/vendor/monsta/bin/monsta",
            "/atlas/lib/monsta/subarray.pro",
            fitsinput,
            str(fitsoutpath),
            *pixcoords,
            "100",
        ],
        check=False,
    )

    # delete the .fits (but keep the .jpeg)
    fitsoutpath.unlink(missing_ok=True)

    # jobxxxxx.fits.jpg to jobxxxx.jpg
    jpgpath = fitsoutpath.with_suffix(".fits.jpg")
    if jpgpath.exists():
        jpgpath.rename(Path(datafile).with_suffix(".jpg"))
    else:
        print(f"ERROR: {jpgpath} was not created")


if __name__ == "__main__":
    main()
