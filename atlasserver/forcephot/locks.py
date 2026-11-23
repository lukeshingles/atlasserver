"""Locks that the web workers share through the filesystem.

A lock is an advisory lock (flock) on a file named for it. The kernel grants it to one open file
at a time, across processes and threads, and releases it when the holder closes the file or dies,
so a holder killed mid-render leaves nothing to judge abandoned. The file cache's add() checks and
then writes, so two workers could both take the last render slot, and the cache culls held entries.
The lock files are never removed: a contender that opened a file before it was unlinked would hold
a lock nobody else can see.
"""

import contextlib
import fcntl
import os
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from collections.abc import Generator


def _lockpath(name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        msg = f"lock name is not a plain file name: {name!r}"
        raise ValueError(msg)

    return Path(settings.LOCKS_DIR, f"{name}.lock")


@contextlib.contextmanager
def _lockfile(name: str) -> Generator[int]:
    """Open the file of the named lock for the body, and close it after the body, which releases the lock."""
    path = _lockpath(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def hold_lock(name: str) -> Generator[None]:
    """Hold the named lock for the body, waiting for it if another holder has it.

    For a short critical section, such as a read-modify-write of one cache entry, where a caller
    that finds the lock taken has nothing better to do than wait a moment.
    """
    with _lockfile(name) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def try_lock(name: str) -> Generator[bool]:
    """Hold the named lock for the body, or yield False at once if another holder has it."""
    with _lockfile(name) as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return

        yield True
