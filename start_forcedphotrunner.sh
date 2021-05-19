#!/bin/bash

while (true) do
   echo $(date) "Starting task runner process" | tee -a fprunnerlog.txt
   # your command goes here instead of sleep
   python3 -u taskrunner/main.py 2> >(tee -a fprunnerlogstderr.txt >&2) && break;
   # show result
   exitcode=$?
   echo $(date) " task runner crashed? Exit code was $exitcode. Restarting in two seconds..." | tee -a fprunnerlogstderr.txt
   sleep 2
done
