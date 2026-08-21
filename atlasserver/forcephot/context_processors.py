"""Template context shared by every page."""

import typing as t

from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from atlasserver.forcephot.models import Task
from atlasserver.taskrunner import status as runnerstatus


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
    # By id rather than filter(user=request.user): request.user is User | AnonymousUser, and
    # is_authenticated is an ordinary property rather than a type guard, so it narrows the union for
    # a reader but not for a type checker. Taking the pk and checking it does narrow it, and it is
    # the id the query wants anyway. Closing over the id also keeps the request out of the lambda.
    userid = request.user.pk
    if not request.user.is_authenticated or userid is None:
        return {"queued_task_count": None}

    return {"queued_task_count": SimpleLazyObject(lambda: Task.queued().filter(user_id=userid).count())}


def sitenotice(request: HttpRequest) -> dict[str, t.Any]:
    """Expose what the server knows about the site notice box: its note, and the runner's state.

    The note shows on every page while ATLASSERVER_SITE_NOTICE has words, and an edit of that
    variable reaches the site with the restart that applies it. There is no control that removes
    or folds the note: the note gives a limit of the measurements, and a reader who put it away
    would read their measurements without it. An earlier control stored such a choice in a cookie
    (atlas-notice-dismissed); that cookie is ignored, and a reader who stored one gets the note.

    A note with no words is not rendered. That is what an operator leaves when they unset the
    variable to remove the note from the site.

    `site_notice` rather than `notice`, which is a name a view could hold too. This value is
    rendered with `|safe`, so a view context that happened to use the short name would win over
    this processor and put its own value into the box unescaped. The prefix is what keeps the
    site-wide box addressable only from here.

    `runner_stale` gives the box its colours before the browser paints it. runnerstatus.js writes
    the sentence, and it cannot run until the page is parsed: the box was thus painted in its
    healthy colours and turned warning-coloured a moment later, on every page load of an outage.
    Only the colours are rendered here, and not the sentence -- the sentence carries an age that
    goes out of date as the reader reads, and the module rewrites it every minute anyway.

    Lazy, for the reason queued_task_count above is: this lands in the context of every render,
    and several draw no box (the two stats fragments, the password reset email body). None of them
    should read the status file. A page that draws the box pays one read of a few hundred bytes,
    with no query and none of the medians the endpoint computes.
    """
    return {
        "site_notice": settings.SITE_NOTICE,
        "runner_stale": SimpleLazyObject(runnerstatus.runner_is_stale),
    }
