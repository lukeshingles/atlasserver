#!/usr/bin/env python3
# coding: utf-8
"""This is the source code for the ATLAS Forced Photometry Server, a Python Django Rest Framework server with a React frontend."""

import datetime
from pathlib import Path
from setuptools import find_packages, setup


def get_version():
    utcnow = datetime.datetime.utcnow()
    strversion = f"{utcnow.year}.{utcnow.month}.{utcnow.day}."
    strversion += f"{utcnow.hour:02d}{utcnow.minute:02d}{utcnow.second:02d}."
    strversion += "dev"
    return strversion


setup(
    name="atlasserver",
    version=get_version(),
    author="Luke Shingles",
    author_email="luke.shingles@gmail.com",
    packages=find_packages(),
    url="https://www.github.com/lukeshingles/atlasserver/",
    license="MIT",
    description="ATLAS Forced Photometry server",
    long_description=(Path(__file__).absolute().parent / "README.md").open("rt").read(),
    long_description_content_type="text/markdown",
    install_requires=(Path(__file__).absolute().parent / "requirements.txt").open("rt").read().splitlines(),
    entry_points={
        "console_scripts": [
            "atlaswebserver = atlaswebserver:main",
            "atlastaskrunner = atlastaskrunner:main",
        ]
    },
    python_requires=">=3.8",
    # test_suite='tests',
    # setup_requires=['pytest', 'pytest-runner', 'pytest-cov'],
    # tests_require=['pytest', 'pytest-runner', 'pytest-cov'],
    include_package_data=True,
)
