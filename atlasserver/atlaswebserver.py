#!/usr/bin/env python3
"""Command line tool to start/stop/restart the ATLAS Apache server."""

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv

APACHEPATH = Path("/tmp/atlasforced")
ATLASSERVERPATH = Path(
    str(Path(__file__).resolve().parent.parent).replace(
        # the space in the path causes an issue with apachectl script,
        # so use a symlink with no space
        "/Users/luke/Library/Mobile Documents/com~apple~CloudDocs/GitHub",
        "/Users/luke/GitHub",
    )
)


load_dotenv(dotenv_path=ATLASSERVERPATH / ".env", override=True)


def get_httpd_pid() -> int | None:
    """Return the pid of the httpd process if it is running, otherwise None."""
    pidfile = Path(APACHEPATH, "httpd.pid")
    if pidfile.is_file():
        pid = int(pidfile.open().read().strip())
        if psutil.pid_exists(pid):
            return pid

        # process ended, so the pid file should be deleted
        pidfile.unlink()

    return None


def run_command(commands: list[str], print_output: bool = True) -> int:
    """Run a command and print the output."""
    proc = subprocess.Popen(
        commands,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        bufsize=1,
        universal_newlines=True,
    )

    if print_output and proc.stdout is not None:
        for line in iter(proc.stdout.readline, ""):
            print(line, end="")

    stdout, stderr = proc.communicate()
    if print_output:
        print(stdout, end="")
        print(stderr, end="")
    assert proc.returncode is not None
    return proc.returncode


def start() -> None:
    """Start the ATLAS Apache server."""
    if pid := get_httpd_pid():
        print(f"ATLAS Apache server is already running (pid {pid})")
        return

    print("Starting ATLAS Apache server")

    if Path(".env").is_file():
        Path(".env").chmod(0o600)

    APACHEPATH.mkdir(parents=True, exist_ok=True)

    # Create a setup script and immediately start the apache instance.  Our URL prefix
    # is specified by the --mount-point setting.  We need to specify a PYTHONPATH before
    # starting the apache instance. Run this script from THIS directory.
    if platform.system() == "Darwin":
        print("Detected macOS, so using testing configuration for http://localhost/")
        port = 80
        mountpoint = "/"
        includefile = []
    else:
        port = 8086
        mountpoint = "/forcedphot"
        includefile = ["--include-file", str(ATLASSERVERPATH / "httpconf.txt")]

    command = [
        "mod_wsgi-express",
        "setup-server",
        "--working-directory",
        str(ATLASSERVERPATH / "atlasserver"),
        "--url-alias",
        f"{mountpoint}/static",
        str(ATLASSERVERPATH / "static"),
        "--url-alias",
        "static",
        "static",
        "--application-type",
        "module",
        "atlasserver.wsgi",
        "--server-root",
        str(APACHEPATH),
        "--port",
        str(port),
        "--mount-point",
        mountpoint,
        *includefile,
        "--log-to-terminal",
    ]

    if "ATLASSERVER_NPROCESSES" in os.environ:
        command.extend(("--processes", os.environ["ATLASSERVER_NPROCESSES"]))
    if "ATLASSERVER_NTHREADPERPROC" in os.environ:
        command.extend(("--threads", os.environ["ATLASSERVER_NTHREADPERPROC"]))
    run_command(command)

    os.environ["PYTHONPATH"] = str(ATLASSERVERPATH)

    # socket might not be released, so try until it is
    while run_command([f"{APACHEPATH / 'apachectl'}", "start"]):
        print("Start command unsuccessful. Trying again in one second...")
        time.sleep(1)

    while not get_httpd_pid():
        pass

    print(f"ATLAS Apache server is running with pid {get_httpd_pid()}")


def stop() -> None:
    """Stop the ATLAS Apache server."""
    if pid := get_httpd_pid():
        print(f"Stopping ATLAS Apache server (pid {pid})")
        run_command([f"{APACHEPATH / 'apachectl'}", "graceful-stop"])
    else:
        print("ATLAS Apache server was not running")


def main() -> None:
    """Handle commands to start, stop, or restart the ATLAS Apache server."""
    if len(sys.argv) == 2 and sys.argv[1] == "start":
        start()

    elif len(sys.argv) == 2 and sys.argv[1] == "restart":
        stop()

        # wait for httpd process to be ended
        while get_httpd_pid():
            pass

        start()

    elif len(sys.argv) == 2 and sys.argv[1] == "stop":
        stop()

    else:
        print("Usage: atlaswebserver [start|restart|stop]")
        print()

        if pid := get_httpd_pid():
            print(f"ATLAS Apache server is running with pid {pid}")
        else:
            print("ATLAS Apache server is not running (pid file missing)")
        sys.exit(3)


if __name__ == "__main__":
    main()
