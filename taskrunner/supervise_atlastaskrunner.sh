#!/bin/bash

taskrunnerpath=$(dirname "$0")

clean_up() {
  echo $(date "+%F %H:%M:%S") "Supervisor: Caught SIGTERM signal!" | tee -a $taskrunnerpath/logs/fprunnerlog_latest.txt
}

# trap 'clean_up; exit' SIGHUP SIGINT SIGTERM SIGABRT SIGKILL SIGQUIT SIGEXIT 0 1 2 3 13 15 # EXIT HUP INT QUIT PIPE TERM
trap clean_up EXIT HUP INT QUIT PIPE TERM

if [[ -n "$TMUX_PANE" ]]; then
    session_name=$(tmux list-panes -t "$TMUX_PANE" -F '#S' | head -n1)
fi

if [ "$session_name" = "atlastaskrunner" ]; then
  echo "running in correct tmux session. continuing..."
else
  echo "ERROR: this script should only be run under the designated tmux session. To start the task runner tmux session, run:"
  echo "  atlastaskrunner start"
  # exit 1
fi

while (true) do
   echo $(date "+%F %H:%M:%S") "Supervisor: Starting task runner Python script" | tee -a $taskrunnerpath/logs/fprunnerlog_latest.txt
   # your command goes here instead of sleep
   python3 -u $taskrunnerpath/main.py 2> >(tee -a $taskrunnerpath/logs/fprunnerlog_latest.txt >&2) && break;
   # show result
   exitcode=$?
   echo $(date "+%F %H:%M:%S") "Supervisor: task runner crashed? Exit code was $exitcode. Restarting in two seconds..." | tee -a $taskrunnerpath/logs/fprunnerlog_latest.txt
   sleep 2
done
