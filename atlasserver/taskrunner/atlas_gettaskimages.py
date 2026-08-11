#!/usr/bin/env python3
"""Input a job data file and produce a zip of FITS images. This script is to be run on sc01."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd


def main() -> None:
    if len(sys.argv) != 3:
        print("ERROR: exactly two argument must be specified: [DATAFILE] ['red' or 'diff']")
        sys.exit(1)
        return

    tmpfolder = Path(tempfile.mkdtemp())
    try:
        gather_images(datafile=sys.argv[1], reduced=sys.argv[2] == "red", tmpfolder=tmpfolder)
    finally:
        # this can hold up to 1000 image cutouts, so it must not be left behind when the remote
        # timeout kills the script or an image conversion raises
        shutil.rmtree(tmpfolder, ignore_errors=True)


def gather_images(datafile: str, reduced: bool, tmpfolder: Path) -> None:
    dfforcedphot = pd.read_csv(datafile, sep=r"\s+", escapechar="#").dropna()

    firstfitsoutpath_c = None
    firstfitsoutpath_o = None
    rowcount = len(dfforcedphot)
    # An abandoned feature wrote a wallpapers.txt alongside the images, recording which wallpaper
    # (the sky reference a difference is made against) each observation used: read from the FITS
    # header with `fitshdr WPDATE WPDIR`, and otherwise inferred from the generation changeovers at
    # MJD 58417, then 58882 for c and 58884 for o. Removed 2026-08; it is in git if wanted.
    commands = []

    for index, row in dfforcedphot[:1000].iterrows():
        obs = row["Obs"]  # looks like '01a59309o0235c'
        imgfolder = "red" if reduced else "diff"  # difference or reduced image
        fitsext = "fits" if reduced else "diff"
        fitsinput = f"/atlas/{imgfolder}/{obs[:3]}/{obs[3:8]}/{obs}.{fitsext}.fz"
        fitsoutpath = Path(tmpfolder / (f"{obs}.fits" if reduced else f"{obs}_diff.fits"))

        if firstfitsoutpath_c is None and obs.endswith("c"):
            firstfitsoutpath_c = fitsoutpath

        if firstfitsoutpath_o is None and obs.endswith("o"):
            firstfitsoutpath_o = fitsoutpath

        commands.append(
            f"echo Image {index:04d} of {rowcount}: {obs}; "
            "/atlas/vendor/monsta/bin/monsta /atlas/lib/monsta/subarray.pro "
            f"{fitsinput} {fitsoutpath} "
            f"$(/atlas/bin/pix2sky -sky2pix {fitsinput} {row['RA']} {row['Dec']}) 400"
            "\n"
        )

    commandfile = tmpfolder / "commandlist.sh"
    with commandfile.open("w") as f:
        f.writelines(commands)

    with commandfile.open() as f:
        subprocess.run(["parallel", "--jobs", "16"], check=False, stdin=f)

    for firstfitsoutpath in [firstfitsoutpath_c, firstfitsoutpath_o]:
        if firstfitsoutpath is not None:
            origext = ".fits" if reduced else "_diff.fits"
            refoutputpath = str(firstfitsoutpath).replace(origext, "_ref.fits")
            command_getref = [
                "/atlas/bin/wpwarp2",
                "-novar",
                "-nomask",
                "-nozerosat",
                "-wp",
                refoutputpath,
                str(firstfitsoutpath),
            ]
            with commandfile.open("a") as f:
                f.write(" ".join(command_getref) + "\n")
            print(" ".join(command_getref))
            subprocess.run(command_getref, check=False)

    shutil.copy(datafile, tmpfolder)

    zipoutpath = Path(datafile).with_suffix(".zip").resolve()

    # make sure the zip file doesn't somehow exist already
    if zipoutpath.exists():
        zipoutpath.unlink()

    cmd = ["zip", "--junk-paths", "-r", str(zipoutpath), str(tmpfolder)]
    print(" ".join(cmd))
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
