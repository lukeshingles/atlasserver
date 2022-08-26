#!/usr/bin/env python3

import psutil
import subprocess
import sys
from pathlib import Path


ATLASSERVERPATH = Path(__file__).resolve().parent


def run_command(commands, print_output=True):
    p = subprocess.Popen(
        commands,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        bufsize=1,
        universal_newlines=True,
    )

    exit_now = False
    if print_output:
        try:
            for line in iter(p.stdout.readline, ""):
                print(line, end="")
        except KeyboardInterrupt:
            exit_now = True

    stdout, stderr = p.communicate()
    if print_output:
        print(stdout, end="")
        print(stderr, end="")

    if exit_now:
        sys.exit(130)

    return p.returncode


def print_tips():
    print("")
    print("to attach to the session (be careful not to stop the task runner!):")
    print("  tmux attach -t atlastaskrunner")
    print("check the log with:")
    print("  atlastaskrunner log [-f]")


def check_session_exists():
    returncode = run_command(["tmux", "has", "-t", "atlastaskrunner"], print_output=False)

    return returncode == 0


def start():
    if check_session_exists():
        print("atlastaskrunner tmux session already exists")
    else:
        print("Starting atlastaskrunner tmux session")
        pidfile = Path("/tmp/atlasforced/taskrunner.pid")
        if pidfile.is_file():
            pid = int(pidfile.open().read().strip())
            if not psutil.pid_exists(pid):
                # process ended, so the pid file should be deleted
                pidfile.unlink()
        run_command(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "atlastaskrunner",
                str(ATLASSERVERPATH / "taskrunner" / "supervise_atlastaskrunner.sh"),
            ]
        )

    print_tips()


def stop():
    if check_session_exists():
        print("Stopping atlastaskrunner tmux session")
        run_command(["tmux", "send-keys", "-t", "atlastaskrunner", "C-C"])
        run_command(["tmux", "kill-session", "-t", "atlastaskrunner"])
    else:
        print("task runner tmux session does not exist")

    if Path("/tmp/atlasforced/taskrunner.pid").is_file():
        Path("/tmp/atlasforced/taskrunner.pid").unlink()


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "start":
        start()

    elif len(sys.argv) == 2 and sys.argv[1] == "restart":
        stop()
        start()

    elif len(sys.argv) == 2 and sys.argv[1] == "stop":
        stop()

    elif len(sys.argv) >= 2 and sys.argv[1] == "log":
        # pass through a -f for follow logs
        run_command(["tail", *sys.argv[2:], str(ATLASSERVERPATH / "taskrunner" / "logs" / "fprunnerlog_latest.txt")])

    else:
        print("Usage: atlastaskrunner [start|restart|stop|log] [-f]")
        print_tips()
        sys.exit(3)


if __name__ == "__main__":
    main()
