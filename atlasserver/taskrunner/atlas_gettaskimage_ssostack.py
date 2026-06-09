#!/usr/bin/env python3
"""Convert the solar system stack FITS file to a JPEG. This script is to be run on sc01.

This code depends on a new monsta script, which is not in standard ATLAS pipeline
(tvjpeg_ssostack.pro). This script, and the new monsta script must be copied into the
atlas user's home directory.
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
    # The new script has jpeg levels settings. These can be adjusted, but the
    # default settings of -1 10 seem to suffice in most cases.
    os.system(f"timeout 5 /atlas/vendor/monsta/bin/monsta ~/tvjpeg_ssostack.pro {stackedfitsfile} -1 10\n")

    # Rename jobxxxxx.fits.jpg to jobxxxxx.jpg.
    Path(stackedfitsfile).with_suffix(".fits.jpg").rename(Path(stackedfitsfile).with_suffix(".jpg"))


if __name__ == "__main__":
    main()
