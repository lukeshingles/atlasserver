#!/usr/bin/env python3
"""Task runner: it runs the forced photometry tasks on ATLAS sc01 over ssh."""

import collections
import contextlib
import datetime as dt
import fcntl
import json
import multiprocessing as mp
import os
import pwd
import stat
import subprocess
import time
import typing as t
import uuid
from pathlib import Path
from signal import SIGHUP
from signal import SIGINT
from signal import signal
from signal import SIGTERM

import django
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.db import close_old_connections
from django.db import models
from django.db import transaction
from django.db.models import prefetch_related_objects
from django.forms.models import model_to_dict

from atlasserver.forcephot.misc import datetime_to_mjd
from atlasserver.taskrunner import status as runnerstatus

REMOTE_SERVER = "atlas"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "atlasserver.settings")

django.setup()

import sys

from django.contrib.auth import get_user_model

from atlasserver.forcephot import queue as taskqueue
from atlasserver.forcephot.models import jobfilename
from atlasserver.forcephot.models import PendingEmailVerification
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.webhooks import send_task_callback

TASK_MAXTIME_SECONDS: int = 4 * 3600

# the exit status ssh itself gives when the connection fails, as opposed to the status of the
# remote command
SSH_CONNECTION_FAILED: int = 255

# The number of slots that use one ssh connection to the remote server. A slot has one session
# at a time. The sshd of the remote server refuses a session above its MaxSessions limit, which
# is 10 unless the administrator set a different value. ssh then opens a new connection for
# that command.
SSH_SLOTS_PER_CONNECTION: int = 8

# the time that an ssh connection with no session stays open for the next task
SSH_CONTROL_PERSIST: str = "1h"

# the directory of the control sockets, relative to the home directory. ssh fails if the path of
# a socket is longer than about 100 characters, so the names are short.
SSH_CONTROL_DIR: str = ".ssh/atlasserver"

# the maximum time for the first connection of a group of slots. The other slots of the group
# wait for this connection, so the time has a limit.
SSH_MASTER_CONNECT_SECONDS: int = 30

# the columns that a forced photometry result file must have, as the header names them
RESULT_REQUIRED_COLUMNS: tuple[str, ...] = ("MJD", "uJy")

# how often a running task asks the database whether it has been cancelled
CANCEL_CHECK_SECONDS: float = 15.0

# How long the ssh command that stops a remote command can take; see stop_remote_job.
REMOTE_STOP_TIMEOUT_SECONDS: float = 60.0

# The directory on sc01 that holds the files of the tasks.
REMOTE_RESULT_DIR: t.Final = "~/atlasserver/results"

# The state of a slot process for its stop signals; see request_stop. _stop_requested is True
# after a stop signal. _defer_stop is True while the poll loop of runtask runs, where a stop
# signal waits for the loop: the loop then stops the remote command before the slot ends.
_stop_requested = False
_defer_stop = False

# how long the main loop waits between polls of the queue. The longer interval applies once the
# queue has been seen to be empty, so that an idle server is not querying twice a second all night.
POLL_INTERVAL_SECONDS: float = 0.5
IDLE_POLL_INTERVAL_SECONDS: float = 5.0

# how many tasks the maintenance sweep loads, cleans up and writes back at a time
MAINTENANCE_BATCH_SIZE: int = 500

# LOG_DIR, STATUS_PATH and STATUS_WRITE_SECONDS live in atlasserver.taskrunner.status so that the
# web server can read the status file without importing this module. All three are referenced
# through that module at each use rather than bound by name here, so that one
# mock.patch.object(runnerstatus, ...) is seen by the runner and the web view alike.


def mjdnow() -> float:
    """Return the current MJD."""
    return datetime_to_mjd(dt.datetime.now(dt.UTC))


def localresultfileprefix(taskid: int) -> str:
    """Return the absolute path to the job file (with no extension) for a given task id."""
    return str(Path(settings.RESULTS_DIR / jobfilename(taskid)))


def log_general(msg: str, suffix: str = "", *args, **kwargs) -> None:
    """Log a message to the console and to a log file (with given filename suffix)."""
    dtnow = dt.datetime.now(dt.UTC)
    strtime = dtnow.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{strtime}  {msg}"
    if not suffix:
        print(line, *args, **kwargs)

    logfile_latest = Path(runnerstatus.LOG_DIR, f"fprunnerlog_latest{suffix}.txt")

    # archive the latest log when its last write was on an earlier UTC day. The check is
    # mtime-based so that the short-lived worker processes also rotate their slot logs
    openmode = "a"
    if logfile_latest.exists():
        lastwrite = dt.datetime.fromtimestamp(logfile_latest.stat().st_mtime, tz=dt.UTC)
        if lastwrite.date() < dtnow.date():
            import shutil

            logfile_archive = Path(
                runnerstatus.LOG_DIR,
                f"fprunnerlog_{lastwrite.year:4d}-{lastwrite.month:02d}-{lastwrite.day:02d}{suffix}.txt",
            )
            shutil.copyfile(logfile_latest, logfile_archive)
            openmode = "w"

    with logfile_latest.open(openmode) as flogfile:
        flogfile.write(line + "\n")


def ssh_control_dir() -> Path:
    """Return the directory of the control sockets.

    ssh expands ~ from the password database and not from $HOME, so the directory comes from the
    same source.
    """
    return Path(pwd.getpwuid(os.getuid()).pw_dir, SSH_CONTROL_DIR)


def ssh_options(slotid: int | None, logfunc: t.Callable[[t.Any], None] | None = None) -> list[str]:
    """Return the ssh options that give a slot the connection of its group of slots.

    Each option is one argument with no space, because rsync divides its -e value at the spaces.
    Without a slot there are no options, and ssh uses only the configuration of the account.
    """
    if slotid is None:
        return []

    controldir = ssh_control_dir()
    try:
        controldir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # lstat and not stat, so that a link to a directory is not accepted as the directory
        status = controldir.lstat()
    except OSError:
        # ssh exits with status 255 when it cannot make the socket, and the task then never
        # completes. A connection for each command is slower, but it works.
        return []

    # A different user who can write to the directory can put a socket or a link there, and ssh
    # then sends the commands of a task to that socket. The mode alone is not sufficient to
    # repair such a directory, because the entries stay. The task runner thus uses the
    # directory only if no other user was able to write to it.
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o022:
        if logfunc is not None:
            logfunc(f"ERROR: {controldir} is not a private directory of this account, so ssh uses no shared connection")
        return []

    group = slotid // SSH_SLOTS_PER_CONNECTION
    return [
        "-oControlMaster=auto",
        f"-oControlPath=~/{SSH_CONTROL_DIR}/{REMOTE_SERVER}-{group}",
        # ControlPersist puts the master into a background process. The ssh process of a slot is
        # then a client, and the SIGTERM of a cancelled task does not close the sessions of the
        # other slots.
        f"-oControlPersist={SSH_CONTROL_PERSIST}",
        # the master exits when the remote server does not reply. Without these options, each
        # new session waits on a dead connection.
        "-oServerAliveInterval=30",
        "-oServerAliveCountMax=3",
    ]


def open_ssh_master(slotid: int, sshoptions: t.Sequence[str], logfunc: t.Callable[[t.Any], None]) -> None:
    """Open the ssh connection of the group of a slot, if it is not open.

    ControlMaster=auto does not wait for a master that a different process starts. Each ssh that
    finds no socket opens a connection of its own, and many new connections at the same time make
    the remote server reset some of them. With a lock for each socket, one slot opens the
    connection, and the other slots of the group wait and then find the socket.

    A failure here does not stop the task. The commands of the task then connect without a master.
    """
    lockfile = ssh_control_dir() / f"{REMOTE_SERVER}-{slotid // SSH_SLOTS_PER_CONNECTION}.lock"
    try:
        # O_NOFOLLOW gives an error for a link, and the open does not change the content of the
        # file. The lock ends when the file closes, and also when this process ends.
        lockfd = os.open(lockfile, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(lockfd) as flock:
            fcntl.flock(flock, fcntl.LOCK_EX)

            # the check asks only the local master, so it makes no connection
            check = subprocess.run(
                ["ssh", "-O", "check", *sshoptions, REMOTE_SERVER],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=SSH_MASTER_CONNECT_SECONDS,
                check=False,
            )
            if check.returncode == 0:
                return

            # this ssh finds no socket, so it starts the master and then runs the command as a
            # client of that master
            start = subprocess.run(
                ["ssh", *sshoptions, f"-oConnectTimeout={SSH_MASTER_CONNECT_SECONDS}", REMOTE_SERVER, "true"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=SSH_MASTER_CONNECT_SECONDS * 2,
                check=False,
            )
            if start.returncode != 0:
                logfunc(f"ERROR: ssh did not open the connection of the slot group: {start.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as ex:
        logfunc(f"ERROR: ssh did not open the connection of the slot group: {ex}")


def run_rsync(
    copycommand: list[str], logfunc: t.Callable[[t.Any], None], sshoptions: t.Sequence[str] = ()
) -> int | None:
    """Run an rsync command, logging its output. Return the exit code, or None if it timed out.

    rsync starts ssh with `sshoptions`; see ssh_options.
    """
    if sshoptions:
        copycommand = [copycommand[0], "-e", " ".join(["ssh", *sshoptions]), *copycommand[1:]]

    logfunc(" ".join(copycommand))

    proc = subprocess.Popen(
        copycommand,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        # replace, not the default strict: one byte of non-UTF-8 in the remote output
        # (a Latin-1 filename, a binary fragment on stderr) made communicate() raise
        # UnicodeDecodeError out of the worker, which then died before mark_finished.
        # The row keeps finishtimestamp NULL, so the task is dispatched again on every
        # pass, forever, with no error_msg to say why.
        errors="replace",
        bufsize=1,
        universal_newlines=True,
    )

    try:
        # without a timeout an rsync against a wedged host blocks the worker slot indefinitely
        stdout, stderr = proc.communicate(timeout=TASK_MAXTIME_SECONDS)
    except subprocess.TimeoutExpired:
        logfunc(f"ERROR: rsync timed out after {TASK_MAXTIME_SECONDS:.0f} seconds")
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)  # reap the process to avoid leaving a zombie
        return None

    if stdout:
        for line in stdout.split("\n"):
            logfunc(f"STDOUT: {line}")

    if stderr:
        for line in stderr.split("\n"):
            logfunc(f"STDERR: {line}")

    if proc.returncode != 0:
        logfunc(f"ERROR: rsync exited with code {proc.returncode}")

    return proc.returncode


def replace_lost_connection() -> None:
    """Close the database connection of this process if it is not usable, so that the next query opens a new one.

    Django does this at the start and at the end of each request, and the task runner has no
    request. Without it, a connection that the server closed (for example at a restart of the
    database) stays in the process, and each later query raises OperationalError.

    Not inside a transaction: there, the close would end the transaction of the caller. The main
    loop and the polls of a slot call this outside a transaction, and a test that runs in a
    transaction keeps its connection.
    """
    if not transaction.get_connection().in_atomic_block:
        close_old_connections()


def remote_pidfile(taskid: int, token: str) -> str:
    """Return the remote file of one attempt that holds the session id of its remote command; see stop_remote_job.

    The token is new for each attempt. Thus the file of an attempt is not the file of a different
    attempt of the same task, which a new task runner can start while the old slot still stops.
    """
    return f"{REMOTE_RESULT_DIR}/{jobfilename(taskid, f'.{token}.pid')}"


def stop_remote_job(
    taskid: int, token: str, sshoptions: t.Sequence[str], logfunc: t.Callable[[t.Any], None], *, delete_files: bool
) -> None:
    """Stop the remote command of one attempt on sc01 if it still runs.

    A kill of the local ssh client does not stop the remote command. The session has no tty, so
    sshd sends no signal to its processes when the client goes, and they run to their end or to
    the time limit of timeout. The remote command writes the id of its session to its pid file
    (see remote_pidfile), and this function stops each process of that session. The session
    must have a process whose arguments hold the token. Thus a session id that a different
    command uses now is not stopped.

    With delete_files, the remote files of the task go also: the task has no row, and nothing
    collects them. Without it, only the pid file goes, because the next attempt of the task
    writes the same file names.
    """
    pidfile = remote_pidfile(taskid, token)
    command = (
        f'p={pidfile}; if [ -s "$p" ]; then s=$(cat "$p"); '
        f'if ps -s "$s" -o args= 2>/dev/null | grep -q -F {token}; then pkill -TERM -s "$s"; fi; fi; rm -f "$p"'
    )
    if delete_files:
        command += f"; rm -rf {REMOTE_RESULT_DIR}/{jobfilename(taskid)}.* {REMOTE_RESULT_DIR}/task{taskid:05d}"

    try:
        result = subprocess.run(
            ["ssh", *sshoptions, REMOTE_SERVER, command],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=REMOTE_STOP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logfunc(f"ERROR: the stop of the remote command took more than {REMOTE_STOP_TIMEOUT_SECONDS:.0f}s")
        return

    if result.returncode == 0:
        logfunc(f"Stopped the remote command{' and deleted the remote files' if delete_files else ''}")
    else:
        logfunc(f"ERROR: could not stop the remote command (exit code {result.returncode}): {result.stderr.strip()}")


def request_stop(signum: int, frame: object) -> None:
    """Handle a stop signal in a slot process.

    In the poll loop of runtask, the signal only sets _stop_requested. The loop then stops the
    remote command and ends the attempt; see attempt_must_stop. Outside the loop, the slot ends at
    once, as with the default action of the signal.
    """
    global _stop_requested  # noqa: PLW0603
    _stop_requested = True
    if not _defer_stop:
        raise SystemExit(128 + signum)


@contextlib.contextmanager
def stop_signals_deferred() -> t.Generator[None]:
    """Make a stop signal wait for the poll loop of runtask, for the duration of the block; see request_stop."""
    global _defer_stop  # noqa: PLW0603
    _defer_stop = True
    try:
        yield
    finally:
        _defer_stop = False


def attempt_must_stop() -> bool:
    """Return whether this slot must stop its attempt, after a stop signal or the end of the main process.

    A slot process outlives a kill of the main process alone. The next task runner then finds
    the attempt open, and it starts the task again. Without this check, the old slot also
    continues, and two remote commands of the task write the same files.
    """
    if _stop_requested:
        return True

    parent = mp.parent_process()
    return parent is not None and not parent.is_alive()


def task_exists(taskid: int) -> bool:
    """Return true if the task exists in the database."""
    # a slot polls this during an attempt that can last hours, so it is the place where a slot
    # finds a connection that the database closed
    replace_lost_connection()
    try:
        Task.objects.all().get(id=taskid)
    except (ObjectDoesNotExist, IndexError):
        return False

    return True


def remove_task_resultfiles(taskid: int, logfunc: t.Callable[[t.Any], None] = log_general) -> None:
    """Delete any associated result files from a deleted task."""
    # Every file is named for its own task, whatever the type: the results (a .txt, .jpg and
    # perhaps a .pdf for forced photometry; a .zip for an image request; a .fits, .jpg and .txt
    # for a stack) and, for an image request, its private copy of the parent's data file. The
    # database row is already gone by the time this runs, so anything left behind is orphaned.
    # Glob patterns must be relative to the globbed directory.
    taskfiles = list(Path(settings.RESULTS_DIR).glob(pattern=jobfilename(taskid, ".*")))
    taskfiles += list(Path(settings.TASK_INPUTS_DIR).glob(pattern=jobfilename(taskid, ".*")))

    for taskfile in taskfiles:
        if Path(taskfile).exists():
            try:
                Path(taskfile).unlink(missing_ok=True)
            except OSError:
                logfunc(f"Error deleting file: {taskfile.name}")
            else:
                logfunc(f"Deleted {taskfile.name}")


def remote_result_filename(task) -> str | None:
    """Return the name of the one file whose absence means the task failed, or None for unknown types.

    A task can produce several files, but this is the one that has to exist.
    """
    if task.request_type == "FP":
        return jobfilename(task.id, ".txt")
    if task.request_type == "IMGZIP":
        # the remote script names the zip after the data file it is given, which is uploaded under
        # this task's own name below
        return jobfilename(task.id, ".zip")
    if task.request_type == "SSOSTACK":
        return jobfilename(task.id, ".fits")

    return None


def build_fp_command(task, remoteresultfile: Path) -> str:
    """Return the remote shell command for a forced photometry task."""
    # falsy exactly when there is no MPC target: task_mpc_name_not_blank keeps whitespace out of
    # the column, so this needs no normalising of its own
    if task.mpc_name:
        atlascommand = f"/atlas/bin/ssforce.sh '{task.mpc_name}'"
    else:
        atlascommand = f"/atlas/bin/force.sh {float(task.ra)} {float(task.dec)}"

    if task.use_reduced:
        atlascommand += " red=1"

    if task.radec_epoch_year:
        atlascommand += f" epoch={task.radec_epoch_year}"

    if task.propermotion_ra:
        atlascommand += f" pmra={task.propermotion_ra}"

    if task.propermotion_dec:
        atlascommand += f" pmdec={task.propermotion_dec}"

    # "is not None" rather than truthiness, so that an explicit bound of 0 is passed through
    # instead of being silently dropped
    if task.mjd_min is not None:
        atlascommand += f" m0={float(task.mjd_min)}"
    if task.mjd_max is not None:
        atlascommand += f" m1={float(task.mjd_max)}"

    atlascommand += " dodb=1 parallel=4"

    # 2026-06-03 KWS Temporary hack. Only specified users can request TDO data.
    if task.user_id in settings.TEST_USERS:
        atlascommand += " tdo=1"

    atlascommand += " | sort -n"
    atlascommand += f" | tee {remoteresultfile}; "
    atlascommand += f"~/atlas_gettaskimage.py {remoteresultfile}"
    atlascommand += " red" if task.use_reduced else " diff"

    return atlascommand


def build_ssostack_command(task, remoteresultfile: Path, remotedatafile: Path, remotetaskdir: Path) -> str:
    """Return the remote shell command for a solar system object image stack task."""
    atlascommand = f"/atlas/bin/stack_rock.sh '{task.mpc_name}'"
    atlascommand += (  # stack_rock.sh doesn't support float mjds
        f" {float(task.mjd_min) if task.mjd_min else 0:.0f}"
    )
    atlascommand += f" {float(task.mjd_max) if task.mjd_max else mjdnow():.0f}"
    atlascommand += f" outdir={remotetaskdir}"
    # 2026-06-03 KWS Need to add tdo to sites. The whole command is passed to ssh as a single
    # argument and evaluated by one remote shell, so plain quotes are what's needed here:
    # escaped quotes end up as literal characters and the site list is split into five arguments.
    if task.user_id in settings.TEST_USERS:
        atlascommand += " sites='hko mlo chl sth tdo'"
    atlascommand += f" | tee {remotedatafile}; "
    atlascommand += f" mv {remotetaskdir}/*.fits {remoteresultfile}; "
    atlascommand += f"~/atlas_gettaskimage_ssostack.py {remoteresultfile}; "
    atlascommand += f" rm -rf {remotetaskdir}"

    return atlascommand


def result_file_problem(localresultfile: Path) -> str | None:
    """Return why a forced photometry result file holds no usable data, or None if it does.

    A plain read rather than pandas: this runs once per task in a process spawned for that task,
    and importing pandas there cost each of them a quarter of a second and eighty megabytes.
    """
    lines = [
        line for line in localresultfile.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()
    ]
    # One line or fewer is the normal result for a target with no measurements: a header alone, or
    # one message line from the remote script. The task finishes with this message and no retry.
    datarows = lines[1:]
    if not datarows:
        return "No data returned"

    # the header names the columns. The plot and the result page read MJD and uJy, so a header
    # without them is diagnostic text from the remote host, not photometry.
    columns = lines[0].lstrip("#").split()
    if any(required not in columns for required in RESULT_REQUIRED_COLUMNS):
        return "Could not parse the result file"

    # a row with a different field count from the header, or with text where MJD or uJy must be,
    # is a diagnostic line mixed into the output
    requiredindices = [columns.index(required) for required in RESULT_REQUIRED_COLUMNS]
    for row in datarows:
        fields = row.split()
        if len(fields) != len(columns):
            return "Could not parse the result file"
        try:
            for index in requiredindices:
                float(fields[index])
        except ValueError:
            return "Could not parse the result file"

    return None


def runtask(task, logfunc, slotid: int | None = None) -> tuple[Path | None, str | None]:
    """Run the forced photometry on atlas sc01 and retrieve the result.

    `slotid` selects the ssh connection that the commands use; see ssh_options.

    returns (resultfilename, error_msg)
     - resultfilename will be None if it could not be created due to an error
     - error_msg is None unless there was an error that would make retries pointless (e.g. invalid object name)
    """
    filename = remote_result_filename(task)
    if filename is None:
        return None, None

    sshoptions = ssh_options(slotid, logfunc)
    if slotid is not None and sshoptions:
        open_ssh_master(slotid, sshoptions, logfunc)

    remoteresultdir = Path("~/atlasserver/results/")
    remotetaskdir = remoteresultdir / f"task{task.id:05d}"
    remoteresultfile = Path(remoteresultdir, filename)
    # the IMGZIP and SSOSTACK tasks use this file. The FP tasks do not use it.
    remotedatafile = Path(remoteresultdir, jobfilename(task.id, ".txt"))

    localresultfile = Path(settings.RESULTS_DIR, filename)
    settings.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # The remote command writes the id of its session first, so that a cancel can stop it; see
    # stop_remote_job. sshd makes the remote shell the leader of a new session, so $$ is that id.
    token = uuid.uuid4().hex
    pidfile = remote_pidfile(task.id, token)
    atlascommand = f"echo $$ > {pidfile}; nice -n 19 timeout {TASK_MAXTIME_SECONDS:.1f}s "
    if task.request_type == "FP":
        atlascommand += build_fp_command(task, remoteresultfile=remoteresultfile)

    elif task.request_type == "IMGZIP":
        # This task's own copy of the parent's data file, made when the request was created. A
        # request created before that copy existed reads the parent's file itself. Uploaded under
        # this task's name either way, so the zip the remote script writes is named for it too.
        localdatafile = task.inputfile()
        if not localdatafile.exists():
            localdatafile = Path(settings.RESULTS_DIR, jobfilename(task.parent_task_id, ".txt"))

        if not localdatafile.exists():
            # the parent forced photometry data file lists the images to fetch. Without it the task
            # can never succeed, so finish with an error instead of retrying it forever.
            return None, "The forced photometry data file for the parent task is no longer available."

        # copy out the FP data file first, so that it's available on sc01 for the image gathering script
        if run_rsync(["rsync", str(localdatafile), f"{REMOTE_SERVER}:{remotedatafile}"], logfunc, sshoptions) != 0:
            # without the data file the remote script cannot select any images, so retry later
            # instead of burning a remote run that is certain to fail
            return None, None

        atlascommand += f"~/atlas_gettaskimages.py {remotedatafile}"
        atlascommand += " red" if task.use_reduced else " diff"

    elif task.request_type == "SSOSTACK":
        atlascommand += build_ssostack_command(
            task,
            remoteresultfile=remoteresultfile,
            remotedatafile=remotedatafile,
            remotetaskdir=remotetaskdir,
        )

    # the exit status of the command of the task, and not of the removal of the pid file
    atlascommand = f"{atlascommand.rstrip().rstrip(';')}; status=$?; rm -f {pidfile}; exit $status"

    logfunc(f"Executing on {REMOTE_SERVER}: {atlascommand}")

    proc = subprocess.Popen(
        ["ssh", *sshoptions, f"{REMOTE_SERVER}", atlascommand],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        # replace, not the default strict: one byte of non-UTF-8 in the remote output
        # (a Latin-1 filename, a binary fragment on stderr) made communicate() raise
        # UnicodeDecodeError out of the worker, which then died before mark_finished.
        # The row keeps finishtimestamp NULL, so the task is dispatched again on every
        # pass, forever, with no error_msg to say why.
        errors="replace",
        bufsize=1,
        universal_newlines=True,
    )

    starttime = time.perf_counter()
    lastlogtime = 0.0
    lastcancelcheck = 0.0
    cancelled = False
    timed_out = False
    stopping = False
    with stop_signals_deferred():
        while not cancelled and not timed_out and not stopping:
            try:
                proc.communicate(timeout=1)

            except subprocess.TimeoutExpired:
                now = time.perf_counter()
                # the cancellation check is a database query, and the 1s communicate() timeout used
                # to run one per second per slot. Across 16 slots that was ~16 queries/sec forever,
                # and a single task at the 4 hour limit issued over ten thousand of them. Cancelling
                # a task that has already been running for minutes is not more urgent than this.
                if (now - lastcancelcheck) >= CANCEL_CHECK_SECONDS:
                    lastcancelcheck = now
                    cancelled = not task_exists(taskid=task.id)
                # no query: a flag and a pipe of the parent process
                stopping = attempt_must_stop()
                timed_out = (now - starttime) >= (
                    TASK_MAXTIME_SECONDS + 30
                )  # give a little extra time for cleanup after timeout
                if (now - lastlogtime) >= 10:
                    logfunc(f"ssh has been running for {now - starttime:.0f} seconds        ")
                    lastlogtime = now
            else:
                break

        if cancelled or timed_out or stopping:
            if timed_out:
                logfunc(
                    f"ERROR: ssh was killed after exceeding TASKMAXTIME limit of {TASK_MAXTIME_SECONDS:.0f} seconds"
                )
            elif stopping:
                logfunc("The task runner stops, so this slot stops its attempt")
            os.kill(proc.pid, SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)  # reap the process to avoid leaving a zombie

            # The kill above stops only the local ssh client. At a timeout, the remote timeout
            # stopped the command already. A cancelled task has no row, so its files go too.
            if cancelled or stopping:
                stop_remote_job(task.id, token, sshoptions, logfunc, delete_files=cancelled)
            return None, None  # don't finish with an error message, because we'll retry it later

    stdout, stderr = proc.communicate()
    logfunc(f"ssh finished after running for {time.perf_counter() - starttime:.1f} seconds")

    if proc.returncode == SSH_CONNECTION_FAILED:
        # the connection dropped, not the remote command. The remote tee has made the result file
        # already, empty, and collecting it would finish the task with "No data returned" for a
        # target that has data; a retry runs the command again
        logfunc("ERROR: the ssh connection failed, so the task will be retried")
        # The remote command has no tty, so it continues without its connection. The retry would
        # then run a second remote command that writes the same files. The stop fails while sc01
        # is not reachable, and then the remote command runs to its end.
        stop_remote_job(task.id, token, sshoptions, logfunc, delete_files=False)
        return None, None

    if stdout:
        stdoutlines = stdout.split("\n")
        logfunc(f"{REMOTE_SERVER} STDOUT: ({len(stdoutlines)} lines of output)")

    if stderr:
        for line in stderr.split("\n"):
            logfunc(f"{REMOTE_SERVER} STDERR: {line}")

    if not task_exists(taskid=task.id):  # check if the task was cancelled
        # the remote command ended, and nothing collects its files
        stop_remote_job(task.id, token, sshoptions, logfunc, delete_files=True)
        return None, None

    # copy files from sc01 to local machine, deleting the remote files after successful copy
    if task.request_type == "FP":
        copycommands = [
            # move the jpg task image from sc01 (deleting the remote file)
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(localresultfile)],
            [
                "rsync",
                "--remove-source-files",
                f"{REMOTE_SERVER}:{Path(remoteresultdir / filename).with_suffix('.jpg')}",
                str(settings.RESULTS_DIR),
            ],
        ]
    elif task.request_type == "SSOSTACK":
        # move the stack *.fits, *.jpg and *.txt data from sc01 (deleting the remote files)
        copycommands = [
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(settings.RESULTS_DIR)],
            [
                "rsync",
                "--remove-source-files",
                f"{REMOTE_SERVER}:{Path(remoteresultfile).with_suffix('.jpg')}",
                str(settings.RESULTS_DIR),
            ],
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remotedatafile}", str(settings.RESULTS_DIR)],
        ]
    else:  # IMGZIP
        # move the image zip from sc01 (deleting the remote file)
        copycommands = [
            ["rsync", "--remove-source-files", f"{REMOTE_SERVER}:{remoteresultfile}", str(settings.RESULTS_DIR)]
        ]
        # the data file is collected from the remote host too, so it does not accumulate there.
        # Into the private input directory, never the results directory: the web server serves
        # that one, and this file is a copy of a data file the parent may since have deleted.
        settings.TASK_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
        copycommands.append(
            [
                "rsync",
                "--remove-source-files",
                f"{REMOTE_SERVER}:{remotedatafile}",
                str(task.inputfile()),
            ]
        )

    for copycommand in copycommands:
        run_rsync(copycommand, logfunc, sshoptions)

    # got an error message (probably no observations in time range) and no fits file, but task is completed
    if task.request_type == "SSOSTACK" and (
        not localresultfile.exists() and localresultfile.with_suffix(".txt").exists()
    ):
        # the fallback matters: an empty error message is falsy, and the task would be retried
        # forever instead of being marked as finished
        errortext = localresultfile.with_suffix(".txt").read_text()[:512].strip()
        return localresultfile, errortext or "No image stack was produced."

    if not localresultfile.exists():
        # task failed somehow
        return None, None

    if task.request_type == "FP" and (problem := result_file_problem(localresultfile)):
        logfunc(f"ERROR: {problem}")
        return localresultfile, problem

    return localresultfile, None


def mark_started(task) -> None:
    """Record that an attempt at the task has started.

    A task that fails without a definite error is left unfinished and picked up again by the
    dispatch loop, so this runs once per attempt.

    starttimestamp is overwritten each time, so that finish minus start measures the attempt which
    actually produced the result -- which is what says how long a comparable task will take, and so
    what the wait estimate is built on. What that leaves out is that the task needed more than one
    go, which the timestamps cannot express, so the attempts are counted alongside them.

    A queryset update rather than a save(), and then the same values onto the instance, both for
    the reasons given in mark_finished below -- `task` is a copy read when the queue was scanned,
    and do_task logs model_to_dict(task) immediately after calling this.
    """
    now = dt.datetime.now(dt.UTC)
    starttimestamp = now.replace(microsecond=0)

    # A task in a slot has no queue position. The positions of the tasks that wait are relative
    # to the lowest one (Task.min_queuepos_relative), and thus they are correct after this write
    # with no renumbering.
    #
    # task_modified_datetime is written by name, because a queryset update does not apply
    # auto_now. The ETag of the task list uses it (views.get_tasklist_etag). The start time is a
    # whole second, and two tasks of one user can start in one second: without this field, the
    # second start does not change the ETag, and the queue page gets a 304 for a list in which
    # the task still waits.
    #
    # retry_after has no value during an attempt: the main loop writes it at the end of the
    # attempt (record_attempt_end).
    #
    # attempt_open is the marker of an attempt whose end has no record. The record of the end
    # removes it. It is in the same write as the start, and thus each start has the marker.
    Task.objects.filter(pk=task.id).update(
        starttimestamp=starttimestamp,
        attempt_count=models.F("attempt_count") + 1,
        queuepos_relative=None,
        retry_after=None,
        attempt_open=True,
        task_modified_datetime=now,
    )

    task.starttimestamp = starttimestamp
    task.attempt_count += 1
    task.queuepos_relative = None
    task.retry_after = None
    task.attempt_open = True
    task.task_modified_datetime = now


def mark_finished(task, error_msg: str | None) -> bool:
    """Record that a task has finished, in the database and on the in-memory instance.

    The database write has to be a queryset update rather than a save(): `task` is a copy read when
    the queue was scanned, and saving it would write back every other column as it was then.

    The same values are then applied to the instance, because notify_finished() reports on it. Until
    this was done, every callback said `finishtimestamp: null` and `success: true` no matter what
    had actually happened, because the instance still held the values it was loaded with.

    Return False if the task has no row in the database. The caller then sends no notification:
    see do_task.
    """
    now = dt.datetime.now(dt.UTC)
    finishtimestamp = now.replace(microsecond=0)

    # task_modified_datetime is written for the reason in mark_started: two tasks of one user can
    # finish in one second.
    #
    # attempt_open stays as it is. The finish of the task is not the record of the attempt: a
    # finished task with the marker is an attempt that still needs its record.
    updated = Task.objects.filter(pk=task.id).update(
        finishtimestamp=finishtimestamp,
        queuepos_relative=None,
        retry_after=None,
        error_msg=error_msg,
        task_modified_datetime=now,
    )

    task.finishtimestamp = finishtimestamp
    task.queuepos_relative = None
    task.retry_after = None
    task.task_modified_datetime = now
    task.error_msg = error_msg

    return updated > 0


def notify_finished(task, logfunc) -> None:
    """Tell the submitter that a task finished, by callback and/or email.

    Both are best-effort and neither may raise: the task is already marked finished by the time
    this runs, and an exception here would kill the worker before it could move on.
    """
    if task.callback_url:
        try:
            send_task_callback(task=task, logfunc=logfunc)
        except Exception as ex:  # noqa: BLE001 (a callback is a courtesy; no failure of it
            # may stop the task being marked finished)
            logfunc(f"ERROR: unexpected failure sending callback for task {task.id}: {ex}")

    send_email_if_needed(task=task, logfunc=logfunc)


def send_email_if_needed(task, logfunc) -> None:
    """Send an email to the user if requested and all tasks in the batch are finished."""
    if not task.send_email:
        logfunc(f"Completed successfully ({task.user.username} did not request an email)")
        return

    if task.from_api:
        logfunc("Requested email for an API-originated task (not sending email)")
        return

    if not task.user.email:
        logfunc(f"Tasked completed ok, but user {task.user.username} has no email address!")
        return

    # if we find an unfinished task in the same batch, hold off sending the email.
    # same batch here is defined as being queued by the same user with identical timestamps
    batchtasks_unfinished = 0
    batchtaskcount = 0

    taskdesclist = []
    localresultfilelist = []
    batchtasks = Task.objects.all().filter(user_id=task.user_id, send_email=True, timestamp=task.timestamp)

    for batchtask in batchtasks:
        batchtaskcount += 1
        if not batchtask.finishtimestamp and batchtask.id != task.id:
            batchtasks_unfinished += 1
        else:
            localresultfile = localresultfileprefix(batchtask.id) + ".txt"
            strtask = (
                f"Task {batchtask.id}: {batchtask.describe_target()} "
                f"{'img_reduced' if batchtask.use_reduced else 'img_difference'} "
                f"\n{batchtask.public_url()}\n"
            )

            if batchtask.comment:
                strtask += " comment: '" + batchtask.comment + "'"

            taskdesclist.append(strtask)
            localresultfilelist.append(localresultfile)

    if batchtasks_unfinished == 0:
        # Two tasks of one batch can finish in two slots at the same time. Each slot then finds
        # no unfinished task in the batch. The update below changes rows for the first slot only,
        # and thus only that slot sends the email.
        if not batchtasks.filter(email_sent=False).update(email_sent=True):
            logfunc("A different slot sends the email for this batch")
            return

        logfunc(f"Sending email to {task.user.email} containing {batchtaskcount} tasks")

        message = EmailMessage(
            subject="ATLAS forced photometry results",
            body=("Your forced photometry results are available for:\n\n" + "\n".join(taskdesclist) + "\n\n"),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[task.user.email],
        )

        attach_size_mb = 0.0

        for localresultfile in localresultfilelist:
            if Path(localresultfile).exists():
                filesize_mb = Path(localresultfile).stat().st_size / 1024.0 / 1024.0
                if (attach_size_mb + filesize_mb) < 22:
                    attach_size_mb += filesize_mb
                    message.attach_file(localresultfile)

        for localresultfile in localresultfilelist:
            pdfpath = Path(localresultfile).with_suffix(".pdf")
            if pdfpath.exists():
                filesize_mb = Path(pdfpath).stat().st_size / 1024.0 / 1024.0
                if (attach_size_mb + filesize_mb) < 22:
                    attach_size_mb += filesize_mb
                    message.attach_file(str(pdfpath))

        try:
            message.send()
        except OSError as ex:
            # smtplib.SMTPException is a subclass of OSError, thus this clause also catches it.
            # a refused recipient or an unreachable mail server must not propagate: it would kill
            # the worker before the task is marked finished, and the task would be re-run forever
            logfunc(f"ERROR: could not send email to {task.user.email}: {ex}")
    else:
        logfunc(
            f"Waiting to send email until remaining {batchtasks_unfinished} "
            f"of {batchtaskcount} batched tasks are finished."
        )


def send_pending_batch_emails(
    since: dt.datetime, logfunc=log_general, heartbeat: t.Callable[[], None] | None = None
) -> int:
    """Send the email of each batch whose last unfinished task went away before it finished, and return their number.

    A slot sends the email of a batch when it finishes the last unfinished task of the batch; see
    send_email_if_needed. When that task goes before it finishes (its user cancels it, or the
    maintenance deletes it), no slot finishes the batch, and the email of the finished tasks does
    not go. This step finds the batches with a finished task that has no email and with no
    unfinished task, and sends their email. The email_sent marker of send_email_if_needed makes
    sure that one batch gets one email, also when a slot finishes a task of it at the same time.

    Only the tasks that finished after `since`, which is the start of this task runner. A task that
    finished before that time can have email_sent False for a batch that got its email: the field
    did not exist when that email went.

    `heartbeat` is called before each send, as in remove_old_tasks: each send waits for the mail
    server, and this runs in the main loop.
    """
    candidates = (
        Task.live()
        .filter(
            send_email=True,
            from_api=False,
            email_sent=False,
            finishtimestamp__gt=since,
            user__is_active=True,
        )
        .exclude(user__email="")
        .select_related("user")
        .order_by("user_id", "timestamp", "id")
    )

    batches: set[tuple[int, dt.datetime]] = set()
    sent = 0
    for task in candidates:
        batch = (task.user_id, task.timestamp)
        if batch in batches:
            continue
        batches.add(batch)

        # a batch that still has an unfinished task gets its email when a slot finishes that task
        if Task.objects.filter(
            user_id=task.user_id, send_email=True, timestamp=task.timestamp, finishtimestamp__isnull=True
        ).exists():
            continue

        if heartbeat is not None:
            heartbeat()
        send_email_if_needed(task=task, logfunc=logfunc)
        sent += 1

    return sent


def next_task(running_taskids: t.Iterable[int], running_userids: t.Iterable[int]) -> Task | None:
    """Return the task to start in a free slot, or None if no task can start.

    This is the task with the lowest queue position whose user has fewer tasks in the slots than
    the maximum (status.MAX_USER_SLOTS). The position holds the priority of the high priority
    group and the fair share of the run time; see forcephot.queue.calculate_queue_positions.

    If each task that waits is a task of a user at the maximum, the result is the task with the
    lowest position: a slot does not stay idle while a task waits. The maximum thus divides the
    slots between the users who want them, and it does not decrease the work of the task runner.
    A user who arrives later waits only for the next task that finishes.

    `running_taskids` is the ids of the tasks in the slots. `running_userids` is the user of each
    of those tasks, and thus a user with three tasks in the slots is in it three times.

    A task with no position cannot start. A task loses its position at a start. If its attempt
    fails, or if its slot stops, it gets a position again at the next renumbering. Without this
    rule, such a task sorts before all the tasks that wait, and a task that always fails keeps a
    slot.

    A task of an inactive account has no position, and thus it cannot start; see
    forcephot.queue.calculate_queue_positions. This query does not read the users: a join to the
    table of the users makes the database start from that table, and this query runs on each
    pass of the main loop that has a free slot.
    """
    slotsofuser = collections.Counter(running_userids)
    users_at_maximum = [userid for userid, count in slotsofuser.items() if count >= runnerstatus.MAX_USER_SLOTS]
    waiting = (
        Task.queued()
        .filter(queuepos_relative__isnull=False)
        .exclude(id__in=list(running_taskids))
        .order_by("queuepos_relative")
    )
    task = waiting.exclude(user_id__in=users_at_maximum).first()
    if task is None and users_at_maximum:
        task = waiting.first()
    return task


def write_status(procs_taskids: dict[int, int], numslots: int, maintenance: bool = False) -> None:
    """Write a snapshot of the runner's state for the status endpoint to read.

    Nothing outside the runner could previously tell whether it was alive, how many slots were
    busy, or whether the queue had stalled — the only signal was the log files. The file is written
    atomically (write to a temporary name, then rename) so that a reader never sees a partial one.

    `maintenance` marks a snapshot written while the hourly sweep is blocking the main loop. The
    slot fields are frozen for that whole window (nothing is reaped or dispatched), so without the
    flag the file would confidently describe workers that may have already exited.
    """
    # One pass over the queued set for all three figures, rather than a first(), a count() and a
    # distinct count() each rebuilding the queryset and scanning again -- this runs every
    # STATUS_WRITE_SECONDS for as long as the runner is up, and the scan grows with the queue.
    #
    # distinct users is here because it is what bounds the rate the queue drains at: dispatch below
    # gives one user a maximum of MAX_USER_SLOTS slots, so the effective concurrency is
    # min(numslots, this times that maximum). A fact about the queue, counted on the runner's own
    # timescale instead of once per request from every open queue page.
    #
    # A user counts only with a task that can use a slot: a task with a queue position, or a task
    # in a slot. A user whose tasks all wait for a new attempt after a failure fills no slot.
    canuseslot = models.Q(queuepos_relative__isnull=False) | models.Q(id__in=list(procs_taskids.values()))
    queuestats = Task.queued().aggregate(
        queued_task_count=models.Count("id"),
        distinct_queued_users=models.Count("user_id", distinct=True, filter=canuseslot),
        oldest_queued=models.Min("timestamp"),
        # the tasks that wait for a new attempt after a failed attempt; see
        # forcephot.queue.calculate_queue_positions. The stats page shows this count.
        retry_wait_task_count=models.Count("id", filter=models.Q(retry_after__gt=dt.datetime.now(dt.UTC))),
    )
    oldest_queued = queuestats["oldest_queued"]

    # What the queue is made of, so that a wait can be priced by the work actually ahead rather
    # than by the type of the task doing the waiting. Dispatch orders one global queue across every
    # request type, and an IMGZIP task fetching a thousand images takes orders of magnitude longer
    # than an FP task fitting one light curve -- so an FP task behind a run of them waits on their
    # run times, not on its own.
    #
    # A second query rather than another aggregate: this one groups, and the counts above do not.
    #
    # Only the tasks that can use a slot, as for the count of the users above. A task that waits
    # for a new attempt after a failure is in no pass, and a large batch of such tasks must not
    # change the price of the passes.
    queued_by_request_type = {
        row["request_type"]: row["count"]
        for row in Task.queued().filter(canuseslot).values("request_type").annotate(count=models.Count("id"))
    }

    status = {
        "written": dt.datetime.now(dt.UTC).isoformat(),
        "pid": os.getpid(),
        "numslots": numslots,
        "slots_busy": len(procs_taskids),
        "running_taskids": sorted(procs_taskids.values()),
        "maintenance": maintenance,
        "queued_task_count": queuestats["queued_task_count"],
        "distinct_queued_users": queuestats["distinct_queued_users"],
        "retry_wait_task_count": queuestats["retry_wait_task_count"],
        "queued_by_request_type": queued_by_request_type,
        # the maximum number of slots for the tasks of one user; the wait estimate of the queue
        # page uses it for the tasks of the reader that are ahead of a task
        "max_user_slots": runnerstatus.MAX_USER_SLOTS,
        "oldest_queued_task_time": oldest_queued.isoformat() if oldest_queued is not None else None,
    }

    try:
        statuspath = runnerstatus.STATUS_PATH
        statuspath.parent.mkdir(parents=True, exist_ok=True)
        tmppath = statuspath.with_suffix(".tmp")
        tmppath.write_text(json.dumps(status))
        tmppath.replace(statuspath)
    except OSError as ex:
        # the status file is diagnostic only; failing to write it must never stop the runner
        log_general(f"WARNING: could not write status file: {ex}")


def current_usage(lastusage: dict[int, taskqueue.Usage], logfunc=log_general) -> dict[int, taskqueue.Usage]:
    """Return the usage of each user, or `lastusage` if the query fails.

    The main loop calls this before each renumbering, and that loop starts every task. Thus an
    exception must not go out of this function: the renumbering then uses the usage of the last
    call, and the next renumbering asks again.
    """
    try:
        return taskqueue.usage_by_user()
    except Exception as ex:  # noqa: BLE001 (the main loop dispatches every task)
        logfunc(f"ERROR: could not update the usage of the users: {ex}")
        return lastusage


def handler(signal_received, frame):
    """Handle any cleanup here."""
    log_general("SIGINT or CTRL-C detected. Exiting")
    sys.exit(0)


class EndedAttempt(t.NamedTuple):
    """An attempt whose slot process ended: the task, its user, and the seconds that it had the slot."""

    taskid: int
    userid: int
    seconds: float
    # the time of the end, which gives the day of the usage record. None is the time of the record.
    when: dt.datetime | None = None
    # False for an attempt that a stop of the task runner cut: its task gets no wait; see
    # open_attempts
    failed: bool = True


def record_attempt_end(
    taskid: int, userid: int, seconds: float, when: dt.datetime | None = None, failed: bool = True
) -> bool:
    """Record the end of one attempt at a task, whose process had a slot for this number of seconds.

    Return True if this call made the record, and False if the attempt had its record before.

    `when` is the time of the end of the attempt, and the default is now. The time goes into the
    usage of the user, for the day of `when`; see forcephot.queue.charge_usage. This is the real
    time for which the attempt had a slot, also for an attempt that failed and for a task that
    its user deleted while it ran.

    A task that is not finished gets the time of its next possible attempt (Task.retry_after).
    The wait is from the end of the attempt and not from its start: an attempt can run for hours
    before it fails, and a wait from its start is then over at its end. With `failed` false, the
    task gets no wait: see open_attempts.

    The marker of the attempt (Task.attempt_open) makes one record for one attempt. This function
    removes the marker, and it makes the record only if the marker was there. A second call for
    the same attempt thus adds no usage. A task that its user deleted has no row for a marker,
    and its attempt always gets the record.

    The marker and the two records are one transaction: an attempt has all of them or none of
    them. An error of the database goes to the caller.
    """
    with transaction.atomic():
        marked = Task.objects.filter(pk=taskid, attempt_open=True).update(attempt_open=False)
        if not marked and Task.objects.filter(pk=taskid).exists():
            return False

        taskqueue.charge_usage(userid, seconds, when=when)
        attempts = (
            Task.objects.filter(pk=taskid, finishtimestamp__isnull=True).values_list("attempt_count", flat=True).first()
            if failed
            else None
        )
        if attempts is not None:
            wait = dt.timedelta(seconds=taskqueue.retry_wait_seconds(attempts))
            # From the end of the attempt, and not from the time of this write: the main loop
            # can make this record late, and the task had no queue position during that time.
            ended = dt.datetime.now(dt.UTC) if when is None else when
            Task.objects.filter(pk=taskid, finishtimestamp__isnull=True).update(retry_after=ended + wait)
    return True


def open_attempts(logfunc=log_general) -> list[EndedAttempt]:
    """Return each attempt that a task runner left with no record of its end.

    The main loop calls this before it starts a task and before it writes the status file. No
    slot process of this task runner exists at that time. Each attempt with the marker
    Task.attempt_open is thus an attempt of a task runner that stopped while the attempt had a
    slot.

    The usage of such an attempt is the time from its start to its end:

    - A finished task has the time of its end: its slot process finished after the main loop
      stopped.
    - The end of an attempt at a task that is not finished is not known. The status file holds
      the last time at which the main loop wrote it, at an interval of STATUS_WRITE_SECONDS. The
      attempt had its slot until that time or longer, and that time is the end in the record.
      With no status file, the record has no run time.

    A task that is not finished gets no wait before its next attempt. The attempt did not fail:
    the task runner stopped it.

    This function only reads. The main loop records the result as it records each other attempt
    that ended (record_ended_attempts): it keeps an attempt whose record fails, and the task of
    that attempt gets no queue position before the record is there. An error of the database
    goes to the caller.
    """
    now = dt.datetime.now(dt.UTC)
    laststatus: dt.datetime | None = None
    try:
        laststatus = now - dt.timedelta(seconds=runnerstatus.read_status()[1])
    except (OSError, KeyError, TypeError, ValueError):
        logfunc("There is no status file of a task runner. An attempt with no end gets a record with no run time.")

    attempts: list[EndedAttempt] = []
    rows = Task.objects.filter(attempt_open=True).values_list("pk", "user_id", "starttimestamp", "finishtimestamp")
    for taskid, userid, started, finished in rows:
        end = finished or laststatus
        seconds = 0.0
        if end is not None and started is not None:
            # between zero and the time from the start to now: the time in the status file and
            # the start time of the task are from two processes
            seconds = min(max(0.0, (end - started).total_seconds()), (now - started).total_seconds())
        attempts.append(EndedAttempt(taskid=taskid, userid=userid, seconds=seconds, when=end, failed=False))

    if attempts:
        logfunc(f"A task runner left {len(attempts)} attempts with no record of their end")
    return attempts


def open_attempts_when_readable(
    logfunc=log_general, sleep: t.Callable[[float], None] = time.sleep
) -> list[EndedAttempt]:
    """Return the result of open_attempts, and try again after each error of the database.

    The main loop does not start before this read succeeds. Without the read, the main loop does
    not know which tasks have an attempt with no record, and the first renumbering gives each of
    them a queue position. The task runner cannot start a task without the database, and thus
    this wait stops no start that is possible.
    """
    while True:
        try:
            return open_attempts(logfunc=logfunc)
        except Exception as ex:  # noqa: BLE001 (an error of the database must not stop the task runner)
            logfunc(f"ERROR: could not read the attempts that a task runner left with no record: {ex}")
        sleep(taskqueue.RECALC_MAX_INTERVAL_SECONDS)
        # Django opens a new connection for the next query if this one is not usable
        close_old_connections()


class SlotClock(t.NamedTuple):
    """The times of the attempt in a slot, from which the main loop gets the run time of the attempt."""

    # time.perf_counter() of the main loop at the start of the attempt
    started: float
    # time.time() at the start of the attempt
    startedwall: float
    # A double that the main loop and the slot process share. The slot process writes time.time()
    # there at its end (run_slot). The value is 0.0 before that write.
    endedwall: t.Any

    def seconds(self) -> float:
        """Return the seconds for which the attempt had its slot. Call this when the process has ended.

        The main loop can find the end of a slot process late: the maintenance blocks the loop
        for minutes. The time from the start to now is thus too long, and the slot process gives
        the time of its own end. That time is from the wall clock, because the values of
        time.perf_counter() of two processes cannot be compared.

        The time from the start to now is the result for a process that did not write its end,
        which is a process that the system stopped. It is also the upper limit of the result,
        because the wall clock can change.
        """
        untilnow = time.perf_counter() - self.started
        endedwall = float(self.endedwall.value)
        if endedwall <= 0.0:
            return untilnow
        return min(untilnow, max(0.0, endedwall - self.startedwall))

    def end(self, seconds: float) -> dt.datetime:
        """Return the time of the end of the attempt, for the run time that seconds() gave.

        The usage record of the attempt is on the day of this time. The main loop can make the
        record later, and on a later day: it can find the end of the process late, and it tries
        a record that failed again.
        """
        return dt.datetime.fromtimestamp(self.startedwall + seconds, dt.UTC)


def record_ended_attempts(attempts: list[EndedAttempt], logfunc=log_general) -> list[EndedAttempt]:
    """Record the end of each of these attempts, and return the attempts whose record failed.

    The main loop is the only writer of these records. It calls this for each slot process that
    ends, with each exit code: a process that finished its task, a process whose attempt failed,
    and a process that the system stopped. A slot process thus cannot leave an attempt with no
    record, and no attempt has two records. At its start, the main loop also calls this for the
    attempts that a task runner left with no record; see open_attempts.

    The main loop keeps the result and gives it here again. An error of the database for a
    moment thus does not lose a record. Until the record of an attempt is there, the main loop
    gives the task no queue position. A task with a position can start, and a start before the
    record loses the record. A failed attempt also needs its time for the next attempt first.

    If the main loop itself stops, the attempts in the slots keep their marker
    (Task.attempt_open), and the next task runner records them; see open_attempts.
    """
    failed: list[EndedAttempt] = []
    lasterror: Exception | None = None
    for attempt in attempts:
        try:
            record_attempt_end(
                attempt.taskid, attempt.userid, attempt.seconds, when=attempt.when, failed=attempt.failed
            )
        except Exception as ex:  # noqa: BLE001 (the main loop dispatches every task)
            failed.append(attempt)
            lasterror = ex

    if failed:
        logfunc(f"ERROR: could not record the end of {len(failed)} attempts: {lasterror}")
    return failed


def start_in_slot(task: Task, slotid: int, endedwall: t.Any = None) -> mp.Process:
    """Start the task in a slot, and return the process of the slot.

    `endedwall` is the shared double of SlotClock, in which the process writes the time of its end.

    This function records the start before it makes the process. The queue order charges a user
    for each task in a slot (forcephot.queue.calculate_queue_positions), and the main loop can
    renumber the queue before the new process runs its first statement. If the process recorded
    the start, that renumbering has the task in a slot with no start: each task of the user that
    waits is then one task too far forward, and the user gets the next free slot also.

    If the process does not start, this function removes the record of the start, and the
    exception goes to the caller. No attempt ran: the task keeps its queue position, and its
    number of attempts does not increase.
    """
    before = (task.starttimestamp, task.queuepos_relative, task.retry_after, task.attempt_count, task.attempt_open)
    mark_started(task)
    try:
        proc = mp.Process(target=run_slot, kwargs={"task": task, "slotid": slotid, "endedwall": endedwall})
        proc.start()
    except Exception:
        task.starttimestamp, task.queuepos_relative, task.retry_after, task.attempt_count, task.attempt_open = before
        Task.objects.filter(pk=task.id).update(
            starttimestamp=task.starttimestamp,
            queuepos_relative=task.queuepos_relative,
            retry_after=task.retry_after,
            attempt_count=task.attempt_count,
            attempt_open=task.attempt_open,
            task_modified_datetime=dt.datetime.now(dt.UTC),
        )
        raise
    return proc


def run_slot(task, slotid: int, endedwall: t.Any = None) -> None:
    """Run one attempt at the task, as the process of a slot, and give the time of its end.

    The main loop recorded the start; see start_in_slot. The time of the end goes to the main
    loop through the shared double, also when the attempt stops with an exception; see SlotClock.
    """
    # SIGINT and SIGHUP come from `atlastaskrunner stop` and `restart`, to each process of the
    # session of the task runner. A slot that ends at once leaves its remote command running.
    for stopsignal in (SIGHUP, SIGINT, SIGTERM):
        signal(stopsignal, request_stop)

    try:
        do_task(task, slotid, started=True)
    finally:
        if endedwall is not None:
            endedwall.value = time.time()


def do_task(task, slotid: int, started: bool = False) -> None:
    """Run a task in a particular slot and send a result email if requested.

    `started` is true when the caller has recorded the start of this attempt; see start_in_slot.
    """

    def logfunc_slotonly(x) -> None:
        log_general(f"slot {slotid:2d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

    def logfunc(x) -> None:
        log_general(f"slot {slotid:2d} task {task.id:05d}: {x}", suffix=f"_slot{slotid:02d}")

        # also log to the main process
        log_general(f"slot {slotid:2d} task {task.id:05d}: {x}")

    if not started:
        mark_started(task)

    logfunc(f"Starting {'API' if task.from_api else 'web'} task for {task.user.username} ({task.user.email}):")
    for key, value in model_to_dict(task).items():
        logfunc_slotonly(f"{key:>17}: {value}")

    runtask_starttime = time.perf_counter()

    # The usage of this attempt is not recorded here. The main loop records it when this
    # process ends; see record_ended_attempts.
    localresultfile, error_msg = runtask(task=task, logfunc=logfunc_slotonly, slotid=slotid)

    runtask_duration = time.perf_counter() - runtask_starttime

    if attempt_must_stop():
        # runtask stopped the attempt and its remote command. The task stays unfinished with no
        # retry wait, and the next task runner starts it again; see open_attempts.
        logfunc("The attempt stopped because the task runner stops. The next task runner starts the task again.")
        return

    if not task_exists(taskid=task.id):  # task was cancelled
        logfunc("Task was cancelled during execution (no longer in database)")

        # in case a result file was created, delete it
        remove_task_resultfiles(taskid=task.id, logfunc=logfunc)
    else:
        logfunc(f"Task ran for {runtask_duration:.1f} seconds")

        def finish(error_msg: str | None) -> None:
            # the task is marked finished before the email is sent, so that a mail failure can
            # never leave the task looking unfinished and get it re-run
            if mark_finished(task=task, error_msg=error_msg):
                notify_finished(task=task, logfunc=logfunc)
            else:
                # The row went between the check above and the write of the finish: its user
                # deleted the task, or the maintenance deleted the task of an inactive account.
                # A notification would tell about a task and a result that are not there.
                logfunc("Task was deleted at the end of its execution (no longer in database)")
                remove_task_resultfiles(taskid=task.id, logfunc=logfunc)

        if error_msg:
            # an error occurred and the task should not be retried (e.g. invalid
            # minor planet center object name or no data returned)
            logfunc(f"Error_msg: {error_msg}")
            finish(error_msg)

        elif localresultfile and localresultfile.exists():
            # the result file is served from disk; nothing parses it into the database
            finish(None)

        else:
            # left unfinished on purpose, with no limit on the attempts: the dispatch loop picks
            # the task up again, so a task that fails while the remote host is down completes
            # when the host is back. The slot becomes free at once: the wait before the next
            # attempt is in Task.retry_after, and a task that waits there uses no slot.
            waittime = taskqueue.retry_wait_seconds(task.attempt_count)
            logfunc(
                "ERROR: Task was not completed successfully. "
                f"The task waits {waittime:.0f} seconds before the next attempt."
            )


def remove_old_tasks(
    days_ago: int,
    harddeleterecord: bool = False,
    request_type: str | None = None,
    is_archived: bool | None = None,
    from_api: bool | None = None,
    logfunc=log_general,
    heartbeat: t.Callable[[], None] | None = None,
) -> None:
    """Remove old tasks matching given criteria, optionally deleting their result files.

    `heartbeat` is called after every batch so that a long sweep does not look like a dead runner
    to whatever is watching the status file.
    """
    now = dt.datetime.now(dt.UTC)
    filteropts: dict[str, t.Any] = {
        "finishtimestamp__isnull": False,
        "finishtimestamp__lt": now - dt.timedelta(days=days_ago),
    }

    if request_type is not None:
        filteropts["request_type"] = request_type

    if not harddeleterecord:
        if is_archived:
            msg = "is_archived=True requires harddeleterecord=True (archiving already-archived tasks is a no-op)"
            raise ValueError(msg)
        # exclude tasks that are already soft deleted from soft deletion query
        is_archived = False

    strarchived = ""
    if is_archived is not None:
        filteropts["is_archived"] = is_archived
        strarchived = "archived " if is_archived else "non-archived "

    strfromapi = ""
    if from_api is not None:
        filteropts["from_api"] = from_api
        strfromapi = "API " if from_api else "web"

    matchingtasks = Task.objects.all().filter(**filteropts)

    taskcount = matchingtasks.count()

    strrequesttype = f"{request_type} " if request_type else ""
    strdeletetype = "hard deleted" if harddeleterecord else "archived"
    logfunc(
        f"There are {taskcount} {strarchived}{strrequesttype}{strfromapi}tasks that finished more than {days_ago} days"
        f" ago to be {strdeletetype}"
    )

    if taskcount > 0:
        strdeleteaction = (
            "Deleting files and database rows" if harddeleterecord else "Deleting files and marking archived"
        )
        taskid_examples = list(matchingtasks.values_list("id", flat=True)[:10])
        logfunc(f"  {strdeleteaction}... (first few task ids: {taskid_examples})")

        # The sweeps reach back up to 183 days, so the widest of them can match a lot of rows.
        # Take one bounded batch at a time rather than reading every matching id into memory, and
        # do the database write once per batch rather than once per task.
        #
        # No offset is needed: processing a task removes it from this queryset, because it is
        # either deleted or marked archived (and the non-archived case filters on is_archived
        # above). The pass limit is a safety net so that a write which somehow left a row matching
        # could not spin here forever.
        maxpasses = taskcount // MAINTENANCE_BATCH_SIZE + 2
        for _ in range(maxpasses):
            # One transaction per batch, with the batch's rows locked before their children are
            # read. RequestImages holds the same lock on a parent while it creates an image request,
            # so a request created at the same time is either seen here, and goes with the parent,
            # or is refused, because the parent has gone. A batch costs a fixed number of queries
            # whatever its size: the children come from one prefetch.
            with transaction.atomic():
                tasks = list(matchingtasks.select_related("parent_task")[:MAINTENANCE_BATCH_SIZE])
                if not tasks:
                    break

                taskids = {task.id for task in tasks}
                list(Task.objects.select_for_update().filter(id__in=taskids).values_list("id", flat=True))
                prefetch_related_objects(tasks, Task.prefetch_imagerequests())

                if harddeleterecord:
                    # Everything this batch removes: the rows it selected, and the image requests
                    # the delete cascades to, whose files the collector's bulk SQL would never
                    # reclaim. Read under the lock; the children the batch did not select are the
                    # ones still needing their files reclaimed.
                    childids = set(Task.objects.filter(parent_task_id__in=taskids).values_list("id", flat=True))
                    going = childids | taskids
                    cascaded = list(Task.objects.filter(id__in=childids - taskids))
                    batch = tasks + cascaded
                else:
                    going = taskids
                    batch = tasks

                for index, task in enumerate(batch):
                    # the batch is named, because every row in it is still live until the write below
                    task.delete_result_files(going=going)
                    # inside the batch as well as after it: 500 tasks times several filesystem
                    # operations each can alone outlast the staleness window on a slow results
                    # mount. The heartbeat rate-limits itself, so calling it often costs almost
                    # nothing.
                    if heartbeat is not None and index % 50 == 49:
                        heartbeat()

                if harddeleterecord:
                    # one statement for the parents and their image requests alike; the rows are
                    # about to go, so there is no point marking them archived first
                    Task.objects.filter(id__in=going).delete()
                else:
                    Task.objects.filter(id__in=taskids).update(is_archived=True, task_modified_datetime=now)

            # After the write, not before it. A reader that arrives between the two finds no cache
            # entry, reads a data file that is still there, and keeps the plot for thirty days.
            for task in batch:
                task.forget_derived_cache()

            if heartbeat is not None:
                heartbeat()
        else:
            logfunc(f"  WARNING: stopped after {maxpasses} batches with tasks still matching")

        logfunc("  Done.")


# How long a registration may wait for its address to be confirmed before the account goes.
# The verification link stops working after settings.PASSWORD_RESET_TIMEOUT, but the row did not,
# so an address registered and never confirmed was held for good: the real owner could neither
# register it, log in, nor reset a password, because reset skips inactive accounts.
UNVERIFIED_ACCOUNT_DAYS: t.Final = 7


def remove_unverified_accounts(days_ago: int = UNVERIFIED_ACCOUNT_DAYS, logfunc=log_general) -> int:
    """Delete the accounts that registered more than `days_ago` ago and never confirmed their address.

    Only inactive rows that carry the pending-verification marker: an account an administrator
    switched off has no marker, and a confirmed one lost its marker.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
    stale = PendingEmailVerification.objects.filter(created__lt=cutoff, user__is_active=False)
    userids = list(stale.values_list("user_id", flat=True))
    if not userids:
        return 0

    # The cutoff is checked again by the delete itself: a link resent since the ids were read has
    # dated the marker afresh, and that account stays. The marker goes with the account, by cascade.
    usermodel = get_user_model()
    _total, removed = usermodel.objects.filter(
        id__in=userids, is_active=False, pending_verification__created__lt=cutoff
    ).delete()
    count = removed.get(usermodel._meta.label, 0)  # noqa: SLF001
    logfunc(f"Removed {count} accounts that did not confirm their address within {days_ago} days")

    return count


def remove_tasks_of_inactive_users(logfunc=log_general, heartbeat: t.Callable[[], None] | None = None) -> int:
    """Delete the unfinished tasks of the accounts that are not active, and return their number.

    An administrator makes an account inactive in the admin site to stop its use of the server.
    The account stays in the database, and without this function its tasks stay in the queue.
    The queue order gives no position to a task of an inactive account, and thus the task runner
    does not start it (forcephot.queue.calculate_queue_positions). This function deletes those
    tasks. The row of a task in a slot goes also. Its slot process finds no row in a maximum of
    CANCEL_CHECK_SECONDS, as for a task that its user deleted. It then stops the remote command
    and removes the result files.

    The finished tasks of the account stay. The other sweeps of the maintenance remove them at
    their usual age.

    An account that waits for the confirmation of its address is also inactive. It cannot sign
    in, and thus it has no task.

    `heartbeat` is called during the work, as in remove_old_tasks.
    """
    taskids = list(
        Task.objects.filter(finishtimestamp__isnull=True, user__is_active=False).values_list("id", flat=True)
    )
    if not taskids:
        return 0

    logfunc(f"Unfinished tasks of inactive accounts: {len(taskids)}. The maintenance deletes them now.")
    removed = 0
    for index, taskid in enumerate(taskids):
        # One task at a time, with the removal of the files of Task.delete. Between the read of
        # the ids and this point, the task can finish, its user can delete it, and an
        # administrator can make the account active again. The two conditions are thus read
        # again here, with a lock on the row of the task and on the row of its account. The
        # slot process of the task writes the finish to the same row (mark_finished). It thus
        # writes before this read, and the task stays, or after the deletion, and it then finds
        # no row and sends no notification.
        with transaction.atomic():
            task = (
                Task.objects.select_for_update()
                .filter(pk=taskid, finishtimestamp__isnull=True, user__is_active=False)
                .first()
            )
            if task is not None:
                task.delete()
                removed += 1
        if heartbeat is not None and index % 50 == 49:
            heartbeat()

    # the tasks of the other users move forward in the queue
    taskqueue.request_recalc()
    logfunc(f"  The maintenance deleted {removed} of them.")
    return removed


def do_maintenance(heartbeat: t.Callable[[], None] | None = None, emailsince: dt.datetime | None = None):
    """Remove old tasks and associated files according to their type and age.

    `heartbeat` is called between sweeps and periodically within them. This runs in the main loop
    and blocks it, and a long sweep would otherwise leave the status file untouched for minutes,
    which from outside is indistinguishable from a dead runner: the queue page would tell every
    user that their tasks are not being processed while all the slots were in fact busy.
    """
    # no maxtime parameter: one used to be accepted and silently ignored (its only reference was a
    # commented-out call), which read as a five-minute bound on the sweep that did not exist

    def logfunc(x):
        log_general(f"Maintenance: {x}")

    sweeps: list[dict[str, t.Any]] = [
        {"days_ago": 14, "harddeleterecord": False, "request_type": "IMGZIP"},
        {"days_ago": 14, "harddeleterecord": False, "request_type": "SSOSTACK"},
        {"days_ago": 183, "harddeleterecord": False, "request_type": "FP"},
        # archived (user-deleted) API tasks: remove the database records and files
        {"days_ago": 7, "harddeleterecord": True, "is_archived": True, "from_api": True},
        # other API tasks
        {"days_ago": 31, "harddeleterecord": True, "from_api": True},
        # Any old tasks
        {"days_ago": 183, "harddeleterecord": True},
    ]

    for sweep in sweeps:
        if heartbeat is not None:
            heartbeat()
        remove_old_tasks(**sweep, logfunc=logfunc, heartbeat=heartbeat)

    remove_unverified_accounts(logfunc=logfunc)

    remove_tasks_of_inactive_users(logfunc=logfunc, heartbeat=heartbeat)

    removed = taskqueue.remove_old_usage_records()
    logfunc(f"Removed {removed} usage records that are older than {taskqueue.USAGE_RETENTION_DAYS} days")

    # after the sweeps, because a sweep can delete the last unfinished task of a batch
    if emailsince is not None:
        batches = send_pending_batch_emails(since=emailsince, logfunc=logfunc, heartbeat=heartbeat)
        logfunc(f"Checked {batches} batches whose last unfinished task went before it finished")


def main() -> None:
    """Run queued tasks and clean up on old tasks."""
    signal(SIGINT, handler)

    runnerstatus.LOG_DIR.mkdir(parents=True, exist_ok=True)

    def logfunc(x):
        log_general(x)

    logfunc("Starting forcedphot task runner...")
    # the start of this task runner; see send_pending_batch_emails
    runnerstarted = dt.datetime.now(dt.UTC)
    mp.set_start_method("spawn")
    numslots: int = runnerstatus.NUMSLOTS
    procs: list[mp.Process | None] = [None for _ in range(numslots)]
    procs_userids: dict[int, int] = {}  # user_id of the task each slot runs
    procs_taskids: dict[int, int] = {}  # id of the task each slot runs
    procs_clocks: dict[int, SlotClock] = {}  # the times of the attempt in each slot

    last_maintenancetime: float = float("-inf")
    last_statustime: float = float("-inf")
    last_queuerecalctime: float = float("-inf")
    last_queueflagchecktime: float = float("-inf")
    # what the queue-recalc counter read the last time positions were renumbered; see
    # forcephot.queue.recalc_generation. Starts below any real value so the first pass renumbers.
    last_recalc_generation: int = -1
    seen_generation: int = -1
    # the usage of each user, for the queue order; see forcephot.queue.usage_by_user
    usage: dict[int, taskqueue.Usage] = {}
    # A slot that became free asks for a renumbering. The task of the slot can be without a
    # position: an attempt that failed, or a process that stopped. That task waits for a position
    # before it can start again; see next_task.
    slot_became_free = False
    # The attempts that ended and whose record is not in the database yet; see
    # record_ended_attempts. At the start, these are the attempts that a task runner left with
    # no record. This read is before the first write of the status file, because the file of the
    # task runner that stopped gives the end of those attempts.
    unrecorded: list[EndedAttempt] = record_ended_attempts(open_attempts_when_readable(logfunc), logfunc=logfunc)
    # No task starts between the end of an attempt and the next renumbering that succeeds. The
    # value is the time at which the wait started, or None.
    dispatch_waits_since: float | None = None
    printedwaiting = False

    def refresh_status(maintenance: bool = False) -> None:
        """Write the status snapshot now, and reset the interval that would have written it."""
        nonlocal last_statustime
        write_status(procs_taskids=procs_taskids, numslots=numslots, maintenance=maintenance)
        last_statustime = time.perf_counter()

    def maintenance_heartbeat() -> None:
        """Keep the status file fresh during the sweep, at the normal cadence.

        Rate-limited so that callers can invoke it as often as they like; marked as maintenance
        because the slot fields are frozen while the sweep blocks the loop, and an unqualified
        snapshot would describe workers that may have already exited.
        """
        if (time.perf_counter() - last_statustime) >= runnerstatus.STATUS_WRITE_SECONDS:
            refresh_status(maintenance=True)

    while True:
        # an idle server polled twice a second all night for nothing. Once the queue has been seen
        # to be empty, wait longer between polls; the interval drops back as soon as work appears,
        # so the latency to pick up a new task is unchanged whenever the server is actually busy.
        time.sleep(IDLE_POLL_INTERVAL_SECONDS if printedwaiting else POLL_INTERVAL_SECONDS)

        # Before the first query of the pass. After a restart of the database, the queries below
        # otherwise raise on each pass, and the first one outside a try block stops main().
        replace_lost_connection()

        if (time.perf_counter() - last_maintenancetime) > 60 * 60:  # once per hour
            last_maintenancetime = time.perf_counter()
            # the sweep blocks this loop, so it refreshes the status file itself; without that a
            # sweep lasting longer than STATUS_WRITE_SECONDS * 4 makes the queue page tell every
            # user that their tasks are not being processed while all the slots are still busy.
            # Caught for the reason the renumbering below is: an exception here leaves main(),
            # the interpreter then waits for every running worker, and the supervisor restarts
            # into the same sweep before any dispatch. A failed sweep is retried in an hour.
            try:
                do_maintenance(heartbeat=maintenance_heartbeat, emailsince=runnerstarted)
            except Exception as ex:  # noqa: BLE001 (this loop dispatches every task)
                logfunc(f"ERROR: the maintenance sweep failed: {ex}")
            refresh_status()
            printedwaiting = False

        # The slots are examined before the renumbering. A slot that became free asks for a
        # renumbering, and the renumbering must not have the task of that slot as a task in a slot.
        attempt_ended = False
        for slotid, proc in enumerate(procs):
            if proc is not None and proc.exitcode is not None:
                proc.join()
                proc.close()
                procs[slotid] = None
                clock = procs_clocks.pop(slotid)
                seconds = clock.seconds()
                unrecorded.append(
                    EndedAttempt(
                        taskid=procs_taskids.pop(slotid),
                        userid=procs_userids.pop(slotid),
                        seconds=seconds,
                        when=clock.end(seconds),
                    )
                )
                attempt_ended = True
                slot_became_free = True
                if dispatch_waits_since is None:
                    dispatch_waits_since = time.perf_counter()

                numslotsfree = sum(1 if p is None else 0 for p in procs)
                logfunc(f"slot {slotid} is now free. {numslotsfree} of {numslots} slots are available")

        # Renumbering belongs here rather than in the submitting request: this loop is the only
        # thing that changes which task is running, it already reads the table twice a second, and
        # doing it here keeps positions fresh as tasks finish. The counter is a file read in
        # production, so it is consulted on its own interval rather than on every pass.
        recalc_requested = False
        if (time.perf_counter() - last_queueflagchecktime) > taskqueue.RECALC_CHECK_INTERVAL_SECONDS:
            last_queueflagchecktime = time.perf_counter()
            seen_generation = taskqueue.recalc_generation()
            # A free slot asks at this interval also, and not at each pass. While the remote host
            # is away, each attempt fails after some seconds, and a slot becomes free on almost
            # each pass. A renumbering locks each queued row.
            recalc_requested = seen_generation != last_recalc_generation or slot_became_free
            # the records that failed get a new try at this interval also
            attempt_ended = attempt_ended or bool(unrecorded)

        # Before the renumbering: the record of a failed attempt holds the time before which the
        # task gets no position.
        if attempt_ended:
            unrecorded = record_ended_attempts(unrecorded, logfunc=logfunc)

        if recalc_requested or ((time.perf_counter() - last_queuerecalctime) > taskqueue.RECALC_MAX_INTERVAL_SECONDS):
            # caught, because this loop is what dispatches every task: unguarded, a lock timeout or
            # a bad queue state would take the exception out of main() and stop the runner, and the
            # supervisor would restart it into the same state. Stale queue positions only change
            # the display. An exception that stops this loop stops every dispatch.
            #
            # The usage has its own guard: if its query fails, the renumbering uses the usage of
            # the last pass.
            usage = current_usage(usage, logfunc=logfunc)

            try:
                # a task whose attempt has no record yet gets no position, as a task in a slot
                noposition = set(procs_taskids.values()) | {attempt.taskid for attempt in unrecorded}
                taskqueue.calculate_queue_positions(usage=usage, running_taskids=noposition)
            except Exception as ex:  # noqa: BLE001 (this loop dispatches every task; stale
                # queue positions only change the display)
                logfunc(f"ERROR: could not update queue positions: {ex}")
            else:
                # only on success, and the value read *before* renumbering: a request that arrived
                # during the pass is still new next time, and a pass that failed leaves its request
                # outstanding so the next check retries it. Recording it either way would drop the
                # request and leave dispatch ordering by stale positions until the backstop.
                last_recalc_generation = seen_generation
                # also only on success: the task of the free slot has no position until a
                # renumbering gives it one, and the free slot waits for the new order
                slot_became_free = False
                dispatch_waits_since = None

            # stamped even on failure, so a persistent one is retried on an interval rather than on
            # every pass of the loop
            last_queuerecalctime = time.perf_counter()

        queuedtasks = Task.queued()

        if (time.perf_counter() - last_statustime) >= runnerstatus.STATUS_WRITE_SECONDS:
            refresh_status()

        if dispatch_waits_since is not None:
            # An attempt ended, and its run time is in the usage of its user now. The positions
            # are from before that. A task that starts cannot be stopped, and thus the free slot
            # waits for a renumbering that succeeds, which is RECALC_CHECK_INTERVAL_SECONDS away
            # in the usual condition.
            #
            # The wait has a limit. If each renumbering fails, the positions stay old, and a
            # start in a wrong order is then better than no start.
            if (time.perf_counter() - dispatch_waits_since) < taskqueue.RECALC_MAX_INTERVAL_SECONDS:
                continue
            logfunc("WARNING: the queue positions are old, and the task runner starts tasks with them")
            dispatch_waits_since = None

        slotid = procs.index(None) if None in procs else -1

        if slotid < 0:
            # every slot is busy, so there is nothing to dispatch and no reason to look at the queue
            continue

        # one query rather than a count() followed by a first(): the count was only used for a log
        # line, and is fetched below only when a task is actually dispatched
        task = next_task(procs_taskids.values(), procs_userids.values())

        if task is None:
            # nothing runnable. That is either an empty queue or a queue holding only tasks that
            # are in the slots or have no position yet, and only the former should slow the polling
            if not printedwaiting and not queuedtasks.exists():
                logfunc("Waiting for tasks...")
                printedwaiting = True
            continue

        printedwaiting = False
        logfunc(f"Unfinished tasks in queue: {queuedtasks.count()}")
        logfunc(f"Running task {task.id} in slot {slotid}")
        # Caught, for the reason the renumbering above is: an exception here leaves main(), and
        # the interpreter then waits for every running worker before the supervisor can start
        # the task runner again. The slot stays free. If the start is in the database and the
        # process is not there, the task has no position: the next renumbering gives it one.
        clock = SlotClock(
            started=time.perf_counter(), startedwall=time.time(), endedwall=mp.Value("d", 0.0, lock=False)
        )
        try:
            proc = start_in_slot(task, slotid, endedwall=clock.endedwall)
        except Exception as ex:  # noqa: BLE001 (this loop dispatches every task)
            logfunc(f"ERROR: could not start task {task.id} in slot {slotid}: {ex}")
            slot_became_free = True
            # the next start waits for the renumbering, and thus an error that stays does not
            # give a new attempt at a start on each pass of this loop
            if dispatch_waits_since is None:
                dispatch_waits_since = time.perf_counter()
            continue

        procs[slotid] = proc
        procs_userids[slotid] = task.user_id
        procs_taskids[slotid] = task.id
        procs_clocks[slotid] = clock


if __name__ == "__main__":
    main()
