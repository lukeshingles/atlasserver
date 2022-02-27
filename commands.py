#!/usr/bin/env python3

# import os
import subprocess
import sys
from pathlib import Path

ATLASSERVERPATH = Path(__file__).resolve().parent


def atlaswebserver():

    p = subprocess.Popen(
        [ATLASSERVERPATH / 'atlaswebserver.sh', *sys.argv[1:]],
        shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding='utf-8', bufsize=1, universal_newlines=True)
    stdout, stderr = p.communicate()
    print(stdout, end='')
    print(stderr, end='')


def atlastaskrunner():

    p = subprocess.Popen(
        [ATLASSERVERPATH / 'atlastaskrunner.sh', *sys.argv[1:]],
        shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding='utf-8', bufsize=1, universal_newlines=True)
    stdout, stderr = p.communicate()
    print(stdout, end='')
    print(stderr, end='')
