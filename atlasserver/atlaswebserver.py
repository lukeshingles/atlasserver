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


def our_httpd_process(pid: int) -> psutil.Process | None:
    """Return the process if it is this server, or None if it is anything else.

    A pid file outlives an unclean exit, and by the time it is read the number in it can belong to
    something else entirely -- so existence alone is not identity. The generated config path is
    unique to this instance and is on its command line.

    The process is returned rather than a yes or no so that a caller acting on it holds the object
    that was checked. psutil records a process's creation time when the object is built and refuses
    to signal a pid that has since been reused, which a second lookup from the bare number could
    not detect.

    A process that cannot be inspected -- including one that is not there at all -- is treated as
    not ours: the caller either signals it or declines to start over it, and both are worse to get
    wrong than an unnecessary "not running".
    """
    try:
        process = psutil.Process(pid)
        if str(APACHEPATH / "httpd.conf") in process.cmdline():
            return process
    except psutil.Error:
        pass

    return None


def get_httpd_pid() -> int | None:
    """Return the pid of the httpd process if it is running, otherwise None."""
    pidfile = Path(APACHEPATH, "httpd.pid")
    if not pidfile.is_file():
        return None

    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        # a file truncated by an unclean exit, which says nothing about what is running. Reported
        # rather than raised: every command here begins by asking whether the server is up, so an
        # exception makes the whole tool unusable until the file is removed by hand.
        print(f"Ignoring unreadable pid file {pidfile}")
        return None

    if our_httpd_process(pid) is not None:
        return pid

    # Removed only once nothing holds that number at all. A process that is there but could not be
    # identified is either a reused pid, where a leftover file is harmless because nothing believes
    # it, or a running server this user is not allowed to inspect -- and deleting the file in that
    # case throws away the only pointer to a live process.
    if not psutil.pid_exists(pid):
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


def signal_graceful_stop(process: psutil.Process) -> bool:
    """Ask the running server to finish its requests and exit, without going through apachectl.

    SIGWINCH is the signal `httpd -k graceful-stop` sends, so this is the same shutdown by a route
    that touches nothing on disk.

    Takes the process rather than its number so that psutil can refuse the signal if that number
    has been reused since the process was identified.
    """
    try:
        process.send_signal(signal.SIGWINCH)
    except psutil.NoSuchProcess:
        return True  # it exited between the check and the signal, which is the wanted end state
    except psutil.AccessDenied:
        print(f"Not allowed to signal pid {process.pid}. Stop it as the user that started it, or as root.")
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

    # Identified again rather than trusting the number from before the command above: that command
    # blocks, and the server can exit -- perhaps part-way through the shutdown it was asked for --
    # and have its pid handed to something else while it runs.
    process = our_httpd_process(pid)
    if process is None:
        # gone, or no longer identifiable as this server -- either way there is nothing here that
        # can be signalled safely
        print(f"Process {pid} could not be confirmed as this server, so it is left alone")
        return

    signal_graceful_stop(process)


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
