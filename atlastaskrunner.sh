#!/bin/bash

ATLASSERVERPATH=$(dirname $(realpath "$0"))

print_tips () {
  echo ""
  echo "to attach to the session (be careful not to stop the task runner!):"
  echo "  tmux attach -t atlastaskrunner"
  echo "check the log with:"
  echo "  atlastaskrunner log"
}

check_session_exists () {
  $(tmux has -t atlastaskrunner > /dev/null 2>&1)
  if [ $? -eq 0 ]; then
    session_exists=1
  else
    session_exists=0
  fi
}

start () {
  check_session_exists
  if [ $session_exists = 1 ]; then
    echo "atlastaskrunner tmux session already exists"
  else
    echo "Starting atlastaskrunner tmux session"
    tmux new-session -d -s atlastaskrunner $ATLASSERVERPATH/taskrunner/supervise_atlastaskrunner.sh
  fi

  print_tips
}

stop () {
  check_session_exists
  if [ $session_exists = 1 ]; then
    echo 'Stopping atlastaskrunner tmux session'
    tmux kill-session -t atlastaskrunner
  else
    echo 'task runner tmux session does not exist'
  fi
}


if [ $# -eq 0 ]; then
  echo 1>&2 "Usage: atlastaskrunner [start|restart|stop|log]"
  print_tips
  exit 3
fi


if [ $1 = "start" ]; then

  start

elif [ $1 = "restart" ]; then

  stop
  start

elif [ $1 = "stop" ]; then

  stop

elif [ $1 = "log" ]; then

  tail -f $ATLASSERVERPATH/taskrunner/logs/fprunnerlog_latest.txt

else

    echo 1>&2 "Usage: $0 [start|restart|stop|log]"
    print_tips
    exit 3

fi
