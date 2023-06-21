#!/usr/bin/env python3
"""Source code for the ATLAS Forced Photometry Server, a Python Django Rest Framework server with a React frontend."""
from pathlib import Path

from setuptools import find_packages
from setuptools import setup
from setuptools_scm import get_version

setup(
    name="atlasserver",
    version=get_version(),
    author="Luke Shingles",
    author_email="luke.shingles@gmail.com",
    packages=find_packages(),
    url="https://www.github.com/lukeshingles/atlasserver/",
    long_description=(Path(__file__).absolute().parent / "README.md").open("rt").read(),
    long_description_content_type="text/markdown",
    install_requires=(Path(__file__).absolute().parent / "requirements.txt").open("rt").read().splitlines(),
    entry_points={
        "console_scripts": [
            "atlaswebserver = atlasserver.atlaswebserver:main",
            "atlastaskrunner = atlasserver.atlastaskrunner:main",
        ]
    },
    python_requires=">=3.8",
    # test_suite='tests',
    # setup_requires=['pytest', 'pytest-runner', 'pytest-cov'],
    # tests_require=['pytest', 'pytest-runner', 'pytest-cov'],
    include_package_data=True,
)
