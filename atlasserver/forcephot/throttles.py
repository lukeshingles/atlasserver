import hashlib
import time
import typing as t
from typing import override

from django.core.cache import caches
from django.views.decorators.debug import sensitive_variables
from rest_framework import exceptions
from rest_framework.authentication import BasicAuthentication
from rest_framework.authentication import get_authorization_header

# the same tuple ForcePhotPermission branches on, so the two cannot drift on what counts as a read.
# Safe methods are throttled far more loosely than writes, but not exempt: the queue page polls the
# task list every few seconds, so reads are the traffic most likely to be hammered, and they were
# previously unlimited -- an unauthenticated caller could poll a task detail as fast as the server
# would answer.
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.throttling import SimpleRateThrottle

from atlasserver.forcephot.locks import hold_lock
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

# how many locks the fixed-window counters share; see count_in_window
WINDOW_LOCK_STRIPES = 64


def window_lock_name(cachekey: str) -> str:
    """Return the name of the lock stripe of a cache key; see count_in_window."""
    return f"window-{int(hashlib.sha256(cachekey.encode()).hexdigest(), 16) % WINDOW_LOCK_STRIPES}"


def client_key(request: t.Any) -> str:
    """Return the hash of the client address of a request, for a cache key.

    A hash, because the cache keys appear in file names.
    """
    return hashlib.sha256(str(client_ip(request)).encode()).hexdigest()


def count_in_window(cachekey: str, window_seconds: float, *, increment: bool = True) -> int:
    """Count one event against a fixed window, and return how many the window then holds.

    With `increment` false, only report the count. The window start is stored with the count so
    that it stays fixed: incr() rewrites the entry with the cache's default timeout, which restarted
    the window on every attempt. time.time() rather than monotonic(), because the value outlives
    the process in a file cache; an elapsed time outside the window is treated as no window, which
    covers an expiry, a stepped clock and a reboot alike.

    The read and the write are one critical section under a lock the workers share, one of a fixed
    number of stripes chosen by the key: a wave of concurrent attempts otherwise counted as one.
    """
    throttlecache = caches["throttle"]

    with hold_lock(window_lock_name(cachekey)):
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


def take_send_permit(cachekey: str, interval_seconds: float) -> bool:
    """Take the one permit for cachekey in its interval. Return whether this caller has it.

    The file cache's add() checks for the key and then writes it, so two concurrent callers can
    both get True. The lock makes the check and the write one critical section across the workers.
    The lock uses the same stripes as count_in_window.
    """
    with hold_lock(window_lock_name(cachekey)):
        return caches["throttle"].add(cachekey, True, timeout=interval_seconds)


# Failed password checks allowed from one address per window, at each door that signs a person
# in: the token endpoint, a Basic header on any API view, the login page and the admin login (see
# login). The budget counts failures and not attempts. Thus the successful logins of a busy shared
# address do not lock it out. A check of the password of one known account uses the budget of
# that account (see account_password_failures_exceeded).
LOGIN_FAILURE_WINDOW_SECONDS: t.Final = 600
LOGIN_FAILURE_LIMIT: t.Final = 10

LOGIN_LIMIT_MESSAGE: t.Final = "Too many failed login attempts from this address. Please wait a few minutes."

# The text for a refused check of the password of one account.
ACCOUNT_GUESS_LIMIT_MESSAGE: t.Final = "Too many incorrect passwords for this account. Wait 10 minutes, then try again."


def login_failures_key(request: t.Any) -> str:
    """Return the cache key of the failed-login budget for the address a request came from."""
    return f"login-failures-{client_key(request)}"


def login_failures_exceeded(request: t.Any) -> bool:
    """Return whether this address has failed more password checks than the window allows."""
    return (
        count_in_window(login_failures_key(request), LOGIN_FAILURE_WINDOW_SECONDS, increment=False)
        >= LOGIN_FAILURE_LIMIT
    )


def note_login_failure(request: t.Any) -> int:
    """Record one failed password check for this address, and return the window's count."""
    return count_in_window(login_failures_key(request), LOGIN_FAILURE_WINDOW_SECONDS)


def account_password_failures_key(userpk: object) -> str:
    """Return the cache key of the failed-password budget of one account."""
    return f"account-password-failures-{userpk}"


def account_password_failures_exceeded(userpk: object) -> bool:
    """Return whether the checks of the password of this account failed more times than the window allows.

    These checks are on the activation page and on the pages of a signed-in account. There, the
    person who guesses has a link or a session for one account, and can send the guesses from many
    addresses. Thus this budget is for the account and not for the client address.
    """
    return (
        count_in_window(account_password_failures_key(userpk), LOGIN_FAILURE_WINDOW_SECONDS, increment=False)
        >= LOGIN_FAILURE_LIMIT
    )


def note_account_password_failure(userpk: object) -> int:
    """Record one failed check of the password of this account, and return the count of the window."""
    return count_in_window(account_password_failures_key(userpk), LOGIN_FAILURE_WINDOW_SECONDS)


@sensitive_variables("password")
def password_was_wrong(username: object, password: object) -> bool:
    """Return whether a refused login offered a wrong password, rather than a refused account.

    Only a wrong password is a guess. The backends refuse an inactive account, such as one not yet
    verified, and the admin one without staff access, in the same way as a wrong password; so the
    password is checked here against the named account. No password offered is no guess.

    A username or password that is a number counts as its text: the token endpoint's serializer
    reads a JSON number as a string, so a numeric username is a real account there.
    """
    # here rather than at the top: this module is imported while the apps are still loading
    from django.contrib.auth import get_user_model

    if isinstance(username, int | float) and not isinstance(username, bool):
        username = str(username)
    if isinstance(password, int | float) and not isinstance(password, bool):
        password = str(password)

    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        return False

    usermodel = get_user_model()
    user = usermodel._default_manager.filter(username=username).first()  # noqa: SLF001
    if user is None:
        # the same hash the check below costs, so that the answer takes as long for a name that
        # has no account as for one that has: ModelBackend does the same, and this check would
        # otherwise undo it and tell a caller which usernames exist
        usermodel().set_password(password)
        return True

    return not user.check_password(password)


class ThrottledBasicAuthentication(BasicAuthentication):
    """Basic authentication that refuses to check a password for an address over the budget.

    Authentication runs before the throttles in APIView.initial(), so this is the only place a
    Basic-header guess can be counted. A refusal is a 429, like every other limit on the site.
    """

    @override
    def authenticate_header(self, request: Request) -> str:
        """Return the Basic challenge only to a request that sent Basic credentials.

        DRF puts the challenge of the first authenticator in each 401. A browser that gets a
        Basic challenge for a fetch shows its own dialog for a username and a password. The queue
        page polls with the session, so without this rule each poll after the end of the session
        opens that dialog. A client that sent a Basic header still gets the Basic challenge. The
        other 401 responses name the token scheme, for which a browser shows no dialog.
        """
        scheme = get_authorization_header(request).split()[:1]
        if scheme and scheme[0].lower() == b"basic":
            # the challenge of BasicAuthentication.authenticate_header
            return f'Basic realm="{self.www_authenticate_realm}"'

        return "Token"

    @override
    @sensitive_variables("password")
    def authenticate_credentials(self, userid: str, password: str, request: t.Any = None) -> t.Any:
        if request is not None and login_failures_exceeded(request):
            raise exceptions.Throttled(detail=LOGIN_LIMIT_MESSAGE)

        try:
            return super().authenticate_credentials(userid, password, request)
        except exceptions.AuthenticationFailed:
            if request is not None and password_was_wrong(userid, password):
                note_login_failure(request)
            raise


class ForcedPhotRateThrottle(ScopedRateThrottle):
    """DRF's ScopedRateThrottle, with one scope for the reads of all the views.

    A view with `throttle_scope` is throttled. Its writes use that scope, and its reads use
    READ_SCOPE, so that reads and writes are counted separately. The cache key, the user id or
    the client address with the scope, comes from ScopedRateThrottle.
    """

    # not the default cache: see the "throttle" alias in settings for why these counters must not
    # share a directory with the queue-recalc flag and the PDF render locks
    cache = caches["throttle"]

    # allow_request() sets these, because ScopedRateThrottle.__init__ does not
    num_requests: int | None
    duration: int | None

    @override
    def allow_request(self, request: Request, view: "APIView") -> bool:
        writescope = getattr(view, self.scope_attr, None)

        # a view with no `throttle_scope` is not throttled
        if not writescope:
            return True

        self.scope = READ_SCOPE if request.method in SAFE_METHODS else writescope
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)

        # SimpleRateThrottle's check, and not ScopedRateThrottle's, which would set the scope again
        # from the view
        return SimpleRateThrottle.allow_request(self, request, view)
