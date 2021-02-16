#!/bin/bash

while (true) do
   echo $(date) "Starting forcephotrunner.py" | tee -a fprunnerlog.txt
   # your command goes here instead of sleep
   python3 -u forcephotrunner.py 2> >(tee -a fprunnerlogstderr.txt >&2) && break;
   # show result
   exitcode=$?
   echo $(date) " forcephotrunner.py crashed? Exit code was $exitcode. Restarting in two seconds..." | tee -a fprunnerlogstderr.txt
   sleep 2
done
