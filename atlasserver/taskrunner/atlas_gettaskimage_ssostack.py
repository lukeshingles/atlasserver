#!/usr/bin/env python3
"""Convert the solar system stack FITS file to a JPEG. This script is to be run on sc01.
   Also depends on a new monsta script, which is not in standard ATLAS pipeline bin directory
   (tvjpeg_ssostack.pro)
"""

import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        print("ERROR: exactly one argument must be specified: [FITSFILE]")
        sys.exit(1)
        return

    stackedfitsfile = sys.argv[1]

    if not Path(stackedfitsfile).exists():
        return

    # Force a timeout if monsta takes longer than 5 seconds. Should be instant.
    os.system(
        "timeout 5 /atlas/vendor/monsta/bin/monsta ~/tvjpeg_ssostack.pro "
        f"{stackedfitsfile} -1 10"
        "\n"
    )

    # jobxxxxx.fits.jpg to jobxxxx.jpg
    Path(stackedfitsfile).with_suffix(".fits.jpg").rename(Path(stackedfitsfile).with_suffix(".jpg"))


if __name__ == "__main__":
    main()
