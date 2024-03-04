#!/usr/bin/env python3
"""Command line tool to start/stop/restart the task runner in a tmux session."""

import subprocess
import sys
from pathlib import Path

import psutil

ATLASSERVERPATH = Path(__file__).resolve().parent.parent


def run_command(commands: list[str], print_output: bool = True) -> int:
    """Run a command and print the output as it runs."""
    proc = subprocess.Popen(
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
            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ""):
                    print(line, end="")
        except KeyboardInterrupt:
            exit_now = True

    proc.wait()
    stdout, stderr = proc.communicate()
    if print_output:
        print(stdout, end="")
        print(stderr, end="")

    if exit_now:
        sys.exit(130)
    assert proc.returncode is not None
    return proc.returncode


def print_tips() -> None:
    """Print tips for using the task runner."""
    print("")
    print("to attach to the session (be careful not to stop the task runner!):")
    print("  tmux attach -t atlastaskrunner")
    print("check the log with:")
    print("  atlastaskrunner log [tail command-line arguments]")


def taskrunner_session_exists() -> bool:
    """Check if the tmux session exists."""
    returncode = run_command(["tmux", "has", "-t", "atlastaskrunner"], print_output=False)

    return returncode == 0


def start() -> None:
    """Start the task runner in a tmux session."""
    if taskrunner_session_exists():
        print("atlastaskrunner tmux session already exists")
    else:
        print("Starting atlastaskrunner tmux session")
        pidfile = Path("/tmp/atlasforced/taskrunner.pid")
        if pidfile.is_file():
            pid = int(pidfile.open().read().strip())
            if not psutil.pid_exists(pid):
                # process ended, so the pid file should be deleted
                pidfile.unlink(missing_ok=True)

        supervisorpath = ATLASSERVERPATH / "atlasserver" / "taskrunner" / "supervise_atlastaskrunner.sh"
        run_command(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "atlastaskrunner",
                f"'{supervisorpath}'",
            ]
        )

    print_tips()


def stop() -> None:
    """Stop the task runner."""
    if taskrunner_session_exists():
        print("Stopping atlastaskrunner tmux session")
        run_command(["tmux", "send-keys", "-t", "atlastaskrunner", "C-C"])
        run_command(["tmux", "kill-session", "-t", "atlastaskrunner"])
    else:
        print("task runner tmux session does not exist")

    if Path("/tmp/atlasforced/taskrunner.pid").is_file():
        Path("/tmp/atlasforced/taskrunner.pid").unlink()


def main() -> None:
    """Handle commands for starting/stopping the task runner or viewing the log."""
    if len(sys.argv) == 2 and sys.argv[1] == "start":
        start()

    elif len(sys.argv) == 2 and sys.argv[1] == "restart":
        stop()
        start()

    elif len(sys.argv) == 2 and sys.argv[1] == "stop":
        stop()

    elif len(sys.argv) >= 2 and sys.argv[1] == "log":
        run_command(
            [
                "tail",
                "-f",
                "-n30",
                *sys.argv[2:],  # pass any additional arguments to tail
                str(ATLASSERVERPATH / "atlasserver" / "taskrunner" / "logs" / "fprunnerlog_latest.txt"),
            ]
        )

    else:
        print("Usage: atlastaskrunner [start|restart|stop|log] [-f]")
        print_tips()
        sys.exit(3)


if __name__ == "__main__":
    main()
