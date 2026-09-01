"""Locks that the web workers share through the filesystem.

A lock is a file that one process creates with O_EXCL. Every local filesystem makes that atomic,
so exactly one of the processes that try at once gets the lock. The file cache offers add(), but
add() checks for the key and then writes it, so two workers can both take the last render slot,
and the cache culls entries when it is full, held ones included.

A holder that is killed leaves its file behind. Every lock therefore has a lifetime, after which
it counts as abandoned and the next taker takes it over. The take-over is a rename, which is
atomic as well, so two takers cannot both take one abandoned lock.
"""

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

from django.conf import settings


def _lockpath(name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        msg = f"lock name is not a plain file name: {name!r}"
        raise ValueError(msg)

    return Path(settings.LOCKS_DIR, f"{name}.lock")


def _create(path: Path) -> bool:
    """Create the lock file, and return False if it exists already."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False

    os.close(fd)
    return True


def _take_over_if_abandoned(path: Path, stale_after: float) -> bool:
    """Remove the file of a lock whose holder is gone, and return whether it was removed.

    Moved aside by a rename, so that two takers of one abandoned lock cannot both remove it: the
    second rename finds no file.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        # released just now, so the caller can try to create it again
        return True

    if age < stale_after:
        return False

    abandoned = path.with_name(f"{path.name}.{os.getpid()}.abandoned")
    try:
        path.rename(abandoned)
    except FileNotFoundError:
        return False

    abandoned.unlink(missing_ok=True)
    return True


@contextlib.contextmanager
def try_lock(name: str, *, stale_after: float) -> Iterator[bool]:
    """Hold the named lock for the body, or yield False at once if another process holds it.

    `stale_after` is the number of seconds after which a lock counts as abandoned. It must exceed
    the longest time a holder can legitimately hold the lock.
    """
    path = _lockpath(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not _create(path) and not (_take_over_if_abandoned(path, stale_after) and _create(path)):
        yield False
        return

    try:
        yield True
    finally:
        path.unlink(missing_ok=True)
