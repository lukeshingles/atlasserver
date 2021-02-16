#!/bin/bash

while (true) do
   echo "Starting forcephotrunner.py" | tee -a fprunnerlog.txt
   # your command goes here instead of sleep
   python3 -u forcephotrunner.py > >(tee -a fprunnerlog.txt) 2> >(tee -a fprunnerlogstderr.txt >&2) && break;
   # show result
   exitcode=$?
   echo $(date) " Runner crashed? Exit code was $exitcode. Restarting in two seconds..." | tee -a fprunnerlogstderr.txt
   sleep 2
done
