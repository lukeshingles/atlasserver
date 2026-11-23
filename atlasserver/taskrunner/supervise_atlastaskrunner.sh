#!/bin/bash

taskrunnerpath=$(dirname "$0")
logfile="$taskrunnerpath/logs/fprunnerlog_latest.txt"

pidfile=/tmp/atlasforced/taskrunner.pid

# without this, the pid file write below fails and the single-instance guard stops working
mkdir -p "$(dirname "$pidfile")"

if [ -f "$pidfile" ]; then
  echo "ERROR: pid file $pidfile already exists. Exiting to prevent multiple instances of the task runner process." | tee -a "$logfile"
  exit 1
fi

echo $$ > "$pidfile"

clean_up() {
  sig=$?
  echo "$(date "+%F %H:%M:%S")" "Supervisor: Caught signal $sig $(kill -l $sig)" | tee -a "$logfile"
  rm -f "$pidfile"
  exit
}

trap clean_up HUP INT QUIT ABRT PIPE TERM EXIT


if [[ -n "$TMUX_PANE" ]]; then
    session_name=$(tmux list-panes -t "$TMUX_PANE" -F '#S' | head -n1)
fi

if [ "$session_name" = "atlastaskrunner" ]; then
  echo "running in correct tmux session. continuing..."
else
  echo "ERROR: this script should only be run under the designated tmux session. To start the task runner tmux session, run:"
  echo "  atlastaskrunner start"
  exit 1
fi

while true; do
   echo "$(date "+%F %H:%M:%S")" "Supervisor: Starting task runner Python script" | tee -a "$logfile"
   python3 -u "$taskrunnerpath/main.py" 2> >(tee -a "$logfile" >&2) && break
   exitcode=$?
   echo "$(date "+%F %H:%M:%S")" "Supervisor: task runner crashed? Exit code was $exitcode. Restarting in two seconds..." | tee -a "$logfile"
   sleep 2
done
