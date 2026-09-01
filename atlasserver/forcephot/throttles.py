import hashlib
import time
import typing as t
from typing import override

from django.core.cache import caches
from rest_framework import exceptions
from rest_framework.authentication import BasicAuthentication

# the same tuple ForcePhotPermission branches on, so the two cannot drift on what counts as a read.
# Safe methods are throttled far more loosely than writes, but not exempt: the queue page polls the
# task list every few seconds, so reads are the traffic most likely to be hammered, and they were
# previously unlimited -- an unauthenticated caller could poll a task detail as fast as the server
# would answer.
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from atlasserver.forcephot.netaddr import client_ip

if t.TYPE_CHECKING:
    # only for the annotations. DRF resolves DEFAULT_THROTTLE_CLASSES while rest_framework.views is
    # still being imported, so importing APIView here at run time is a circular import that leaves
    # the whole app unable to start. rest_framework.permissions above is safe: views.py already
    # imports it at module scope.
    from rest_framework.views import APIView

# Scope used for safe methods, in place of whatever the view declares for writes. Separate so that
# a burst of reads cannot use up a user's ability to submit, and so the two can be tuned apart.
READ_SCOPE = "forcephotread"


def count_in_window(cachekey: str, window_seconds: float, *, increment: bool = True) -> int:
    """Count one event against a fixed window, and return how many the window then holds.

    With `increment` false, only report the count. The window start is stored with the count so
    that it stays fixed. incr() cannot be used: it rewrites the value with the cache's *default*
    timeout rather than the remaining one, which both shortened the window and restarted it on
    every attempt -- so a client that kept trying kept renewing its own block indefinitely.

    time.time(), not monotonic(): this value goes to a file-based cache that outlives the process
    and the host, and monotonic() is measured from an epoch that does not survive a reboot -- a
    stored value from the previous boot reads as being in the future here.

    An elapsed time outside the window is therefore treated as no window at all, which covers an
    expiry, a clock stepped by NTP, and any stale epoch. Without that the arithmetic below could
    compute a timeout of the machine's former uptime and lock a shared address out for weeks.
    """
    throttlecache = caches["throttle"]
    now = time.time()
    window = throttlecache.get(cachekey)
    elapsed = now - window[1] if window is not None else None

    if elapsed is None or not (0 <= elapsed < window_seconds):
        count, started = 0, now
    else:
        count, started = window[0], window[1]

    if not increment:
        return count

    count += 1
    throttlecache.set(cachekey, (count, started), timeout=window_seconds - (now - started))

    return count


# Failed password checks allowed from one address per window, on every path that takes a
# password: the token endpoint, a Basic header on any API view, the login page and the admin's.
#
# DRF authenticates before it throttles, so a wrong password in a Basic header was answered 401
# by the authenticator before any throttle ran and was never counted; and Django's own login
# views are not DRF views, so no throttle reached them at all. The budget counts failures rather
# than attempts, so a busy shared address is not locked out by its successful logins.
LOGIN_FAILURE_WINDOW_SECONDS: t.Final = 600
LOGIN_FAILURE_LIMIT: t.Final = 10

LOGIN_LIMIT_MESSAGE: t.Final = "Too many failed login attempts from this address. Please wait a few minutes."


def login_failures_key(request: t.Any) -> str:
    """Return the cache key of the failed-login budget for the address a request came from."""
    return f"login-failures-{hashlib.sha256(str(client_ip(request)).encode()).hexdigest()}"


def login_failures_exceeded(request: t.Any) -> bool:
    """Return whether this address has failed more password checks than the window allows."""
    return (
        count_in_window(login_failures_key(request), LOGIN_FAILURE_WINDOW_SECONDS, increment=False)
        >= LOGIN_FAILURE_LIMIT
    )


def note_login_failure(request: t.Any) -> int:
    """Record one failed password check for this address, and return the window's count."""
    return count_in_window(login_failures_key(request), LOGIN_FAILURE_WINDOW_SECONDS)


class ThrottledBasicAuthentication(BasicAuthentication):
    """Basic authentication that refuses to check a password for an address over the budget.

    Authentication runs before the throttles in APIView.initial(), so this is the only place a
    Basic-header guess can be counted. A refusal is a 429, like every other limit on the site.
    """

    @override
    def authenticate_credentials(self, userid: str, password: str, request: t.Any = None) -> t.Any:
        if request is not None and login_failures_exceeded(request):
            raise exceptions.Throttled(detail=LOGIN_LIMIT_MESSAGE)

        try:
            return super().authenticate_credentials(userid, password, request)
        except exceptions.AuthenticationFailed:
            if request is not None:
                note_login_failure(request)
            raise


class ForcedPhotRateThrottle(SimpleRateThrottle):
    """Limit the rate of API calls by different amounts for various parts of the API.

    Any view that has the `throttle_scope` property set will be throttled. The unique cache key is
    generated by concatenating the user id of the request with the scope being applied, so reads
    and writes are counted separately.
    """

    scope_attr = "throttle_scope"

    # not the default cache: see the "throttle" alias in settings for why these counters must not
    # share a directory with the queue-recalc flag and the PDF render locks
    cache = caches["throttle"]

    def __init__(self) -> None:
        """Do none of SimpleRateThrottle's setup: the rate is not known until the view is known."""

    @override
    def allow_request(self, request: Request, view: "APIView") -> bool:
        # We can only determine the scope once we're called by the view.
        writescope = getattr(view, self.scope_attr, None)

        # If a view does not have a `throttle_scope` always allow the request
        if not writescope:
            return True

        self.scope = READ_SCOPE if request.method in SAFE_METHODS else writescope

        # Determine the allowed request rate as we normally would during
        # the `__init__` call.
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)

        # We can now proceed as normal.
        return super().allow_request(request, view)

    @override
    def get_cache_key(self, request: Request, view: "APIView") -> str:
        """If `view.throttle_scope` is not set, don't apply this throttle.

        Otherwise generate the unique cache key by concatenating the user id
        with the '.throttle_scope` property of the view.
        """
        ident = request.user.pk if request.user.is_authenticated else self.get_ident(request)

        return self.cache_format % {"scope": self.scope, "ident": ident}
