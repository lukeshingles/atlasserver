#!/usr/bin/env python3
"""Command line tool to start/stop/restart the ATLAS Apache server."""

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv

APACHEPATH = Path("/tmp/atlasforced")

# where this file actually lives. Fine to read .env through, even with a space in it: only the
# apachectl script below cannot cope with one.
SOURCEPATH = Path(__file__).resolve().parent.parent

load_dotenv(dotenv_path=SOURCEPATH / ".env", override=True)


def _atlasserverpath() -> Path:
    """Return the project root to hand to mod_wsgi-express, or fail explaining why it cannot.

    The apachectl that mod_wsgi-express generates is a shell script that interpolates these paths
    unquoted, so a space anywhere in the project path breaks it later, with an error that says
    nothing about the cause. ATLASSERVER_PATH overrides the location for exactly that case: point
    it at a symlink whose path has no space.
    """
    if override := os.environ.get("ATLASSERVER_PATH"):
        # not resolve(): resolving a symlink here would put the space straight back
        path = Path(override).absolute()
        if " " in str(path):
            msg = f"ATLASSERVER_PATH must not contain a space, but is {path}"
            raise ValueError(msg)
        return path

    if " " in str(SOURCEPATH):
        msg = (
            f"The project path {SOURCEPATH} contains a space, which the apachectl script generated"
            f" by mod_wsgi-express cannot handle. Create a symlink whose path has no space"
            f" (ln -s '{SOURCEPATH}' ~/atlasserver) and set ATLASSERVER_PATH to it in .env."
        )
        raise ValueError(msg)

    return SOURCEPATH


ATLASSERVERPATH = _atlasserverpath()


def is_our_httpd(pid: int) -> bool:
    """Whether the process is this server, rather than whatever inherited its pid.

    A pid file outlives an unclean exit, and by the time it is read the number in it can belong to
    something else entirely -- so existence alone is not identity. The generated config path is
    unique to this instance and is on its command line.

    A process that cannot be inspected is treated as not ours: the caller either signals it or
    declines to start over it, and both are worse to get wrong than an unnecessary "not running".
    """
    try:
        return any(str(APACHEPATH / "httpd.conf") in arg for arg in psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def get_httpd_pid() -> int | None:
    """Return the pid of the httpd process if it is running, otherwise None."""
    pidfile = Path(APACHEPATH, "httpd.pid")
    if pidfile.is_file():
        pid = int(pidfile.open().read().strip())
        if psutil.pid_exists(pid) and is_our_httpd(pid):
            return pid

        # the process ended, or its pid has since been handed to something else
        pidfile.unlink()

    return None


def run_command(commands: list[str], print_output: bool = True) -> int:
    """Run a command and print the output."""
    proc = subprocess.Popen(
        commands,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merged, so a full stderr pipe can't deadlock the stdout read
        encoding="utf-8",
        bufsize=1,
        universal_newlines=True,
    )

    if print_output and proc.stdout is not None:
        for line in iter(proc.stdout.readline, ""):
            print(line, end="")

    stdout, _stderr = proc.communicate()
    if print_output and stdout:
        print(stdout, end="")
    assert proc.returncode is not None
    return proc.returncode


def start() -> None:
    """Start the ATLAS Apache server."""
    if pid := get_httpd_pid():
        print(f"ATLAS Apache server is already running (pid {pid})")
        return

    print("Starting ATLAS Apache server")

    envfile = ATLASSERVERPATH / ".env"
    if envfile.is_file():
        envfile.chmod(0o600)

    APACHEPATH.mkdir(parents=True, exist_ok=True)

    # Create a setup script and immediately start the apache instance.  Our URL prefix
    # is specified by the --mount-point setting.  We need to specify a PYTHONPATH before
    # starting the apache instance. Run this script from THIS directory.
    #
    # The mount point comes from Django's PATHPREFIX rather than being derived here a second
    # time: PATHPREFIX follows DEBUG (which ATLASSERVER_DEBUG can override on any platform), and
    # a mount point chosen by a different rule would serve the app at one prefix while every URL
    # Django generates used another.
    from atlasserver import settings as django_settings

    mountpoint = django_settings.PATHPREFIX or "/"

    if platform.system() == "Darwin":
        print("Detected macOS, so using testing configuration for http://localhost/")
        port = 80
        includefile: list[str] = []
    else:
        port = 8086
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
        time.sleep(0.2)

    print(f"ATLAS Apache server is running with pid {get_httpd_pid()}")


def signal_graceful_stop(pid: int) -> bool:
    """Ask the running server to finish its requests and exit, without going through apachectl.

    SIGWINCH is the signal `httpd -k graceful-stop` sends, so this is the same shutdown by a route
    that touches nothing on disk.
    """
    try:
        psutil.Process(pid).send_signal(signal.SIGWINCH)
    except psutil.NoSuchProcess:
        return True  # it exited between the pid check and the signal, which is the wanted end state
    except psutil.AccessDenied:
        print(f"Not allowed to signal pid {pid}. Stop it as the user that started it, or as root.")
        return False

    return True


def stop() -> None:
    """Stop the ATLAS Apache server."""
    pid = get_httpd_pid()
    if not pid:
        print("ATLAS Apache server was not running")
        return

    print(f"Stopping ATLAS Apache server (pid {pid})")
    if run_command([f"{APACHEPATH / 'apachectl'}", "graceful-stop"]) == 0:
        return

    # apachectl stops the server by running httpd against the generated config, which loads
    # mod_wsgi -- and that shared object names the interpreter it was built against by absolute
    # path. Moving that interpreter (a uv patch upgrade collecting the old one, or switching the
    # venv between Python installations) therefore breaks stopping a server that is running
    # perfectly well, since the config cannot be parsed to reach the shutdown.
    #
    # The running process needs nothing from disk to shut down, so signal it instead.
    print("apachectl could not stop the server, so signalling the process directly")
    signal_graceful_stop(pid)


def main() -> None:
    """Handle commands to start, stop, or restart the ATLAS Apache server."""
    if len(sys.argv) == 2 and sys.argv[1] == "start":
        start()

    elif len(sys.argv) == 2 and sys.argv[1] == "restart":
        stop()

        # wait for httpd process to be ended
        while get_httpd_pid():
            time.sleep(0.2)

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
