"""Template context shared by every page."""

import typing as t

from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from atlasserver.forcephot.models import Task


def static_version(request: HttpRequest) -> dict[str, t.Any]:
    """Expose the cache-busting suffix appended to static asset URLs.

    The built JS bundles and main.css are served straight out of STATIC_ROOT under stable names, so
    a browser holding an old copy alongside a freshly deployed one renders the new markup with the
    old script or the old styles. That was handled by hand-edited "?ver=20260806" strings in six
    places across two templates, which only worked as long as whoever changed a bundle remembered
    all six.

    ManifestStaticFilesStorage would be the usual answer and does not fit here: STATIC_ROOT is the
    repo's own static/ directory rather than a collect destination, and the task runner writes
    result files continuously into static/results/ inside it, so hashing that tree would be a
    change to how the site is deployed rather than a change to a setting.
    """
    return {"static_version": settings.STATIC_VERSION}


def queued_task_count(request: HttpRequest) -> dict[str, t.Any]:
    """Expose how many of the signed-in user's own tasks are waiting or running.

    The navbar shows this as a badge on the Queue link, so that "is anything still going?" can be
    answered from any page rather than only from the queue itself.

    Task.queued() rather than a filter written here, so the badge counts what the queue page counts:
    that method is the project's one definition of the set the queue positions cover, and a second
    filter would be free to drift from it (over archived tasks, most likely).

    Lazy, because this lands in the context of every template render, and several of those draw no
    navbar and so have no badge to fill: the two stats fragments the stats page fetches, and the
    password reset email body. None of them should pay for a count. Anonymous visitors get None
    rather than 0: they have no tasks to count, and the template hides the badge either way.

    A template can use the result as it would the number -- truth, rendering and comparison are all
    proxied -- but int() on it raises, so Python callers should compare or add rather than coerce.
    """
    if not request.user.is_authenticated:
        return {"queued_task_count": None}

    return {"queued_task_count": SimpleLazyObject(lambda: Task.queued().filter(user=request.user).count())}
