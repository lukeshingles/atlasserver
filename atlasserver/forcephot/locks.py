"""Locks that the web workers share through the filesystem.

A lock is an advisory lock (flock) on a file named for it, taken without waiting. The kernel
grants it to one open file at a time, across processes and across the threads of one process, and
releases it when the holder closes the file or dies. A holder that is killed mid-render therefore
leaves nothing behind that a later request would have to judge abandoned and take over, and no
take-over exists to race.

The file cache offers add(), but add() checks for the key and then writes it, so two workers can
both take the last render slot, and the cache culls entries when it is full, held ones included.

The lock files are never removed. A lock is a property of the open file, not of the file's
existence: a contender that opened the file before it was unlinked would hold a lock on a file
that nobody else can see.
"""

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

from django.conf import settings


def _lockpath(name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        msg = f"lock name is not a plain file name: {name!r}"
        raise ValueError(msg)

    return Path(settings.LOCKS_DIR, f"{name}.lock")


@contextlib.contextmanager
def hold_lock(name: str) -> Iterator[None]:
    """Hold the named lock for the body, waiting for it if another holder has it.

    For a short critical section, such as a read-modify-write of one cache entry, where a caller
    that finds the lock taken has nothing better to do than wait a moment.
    """
    path = _lockpath(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def try_lock(name: str) -> Iterator[bool]:
    """Hold the named lock for the body, or yield False at once if another holder has it."""
    path = _lockpath(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return

        yield True
    finally:
        # closing the file releases the lock
        os.close(fd)
