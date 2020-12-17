#!/bin/bash

python3 -u forcephotrunner.py > >(tee -a fprunnerlog.txt) 2> >(tee -a fprunnerlogstderr.txt >&2)