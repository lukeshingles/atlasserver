"""Optional completion callbacks for API-submitted tasks.

API clients have no way to be told that a task finished — result emails are skipped for
API-originated tasks — so every API user polls, which is a large share of the server's request
volume. A task may carry a callback_url that is POSTed a small JSON body once it finishes.

The URL comes from a user, so this is a server-side request forgery surface: without checks, a
caller could aim the server at a host only the server can reach and use the response status as an
oracle. validate_callback_url() is applied when the task is submitted and again immediately before
the request is sent.
"""

import contextlib
import email.message
import json
import socket
import threading
import typing as t
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextvars import ContextVar
from typing import override
from urllib.parse import urlparse

from django.conf import settings

from atlasserver.forcephot.netaddr import address_is_public

if t.TYPE_CHECKING:
    # only for the annotation: importing models here at run time would pull Django's app registry
    # into a module the task runner imports before django.setup()
    from atlasserver.forcephot.models import Task

# a callback is a courtesy, not part of finishing the task: keep it short so that a slow or
# blackholed endpoint cannot hold a worker slot open
CALLBACK_TIMEOUT_SECONDS = 10

MAX_CALLBACK_URL_LENGTH = 500

# socket.getaddrinfo takes no timeout, so it blocks for as long as the resolver does. The name is
# the caller's, and production serves the whole site with four single-threaded workers, so one
# name whose server stalls could take a worker out of service for minutes.
CALLBACK_RESOLVE_TIMEOUT_SECONDS = 5.0

# The most resolver threads that may run at once. A resolver that does not answer leaves its
# thread behind (see resolve_within_timeout), and a caller who names such a host on every
# submission would otherwise grow one thread per attempt until the resolver gave up on each.
CALLBACK_RESOLVER_THREADS = 8
_resolver_slots = threading.BoundedSemaphore(CALLBACK_RESOLVER_THREADS)

# The URLs that the current request has already validated, or None outside a submission.
#
# One submission validates the same URL over and over: a radeclist copies callback_url into every
# row, splitradeclist validates each row, and views.create validates the whole list again -- 200
# resolutions for one 100-line request. The submission view opens a scope with
# callback_urls_validated_once(), and the second and later checks of one URL within that scope
# are answered from the first. Nothing outlives the request, and nothing is shared between
# requests or threads: a context variable is per task and per thread. The send path runs outside
# any scope, so the check it makes before a request is always current, which is what lets it
# notice a name that has moved (see send_task_callback).
_validated_urls: ContextVar[set[str] | None] = ContextVar("validated_callback_urls", default=None)


class CallbackUrlError(ValueError):
    """Raised when a callback URL is missing, malformed or points somewhere it must not."""


@contextlib.contextmanager
def callback_urls_validated_once() -> Iterator[None]:
    """Open the scope within which each callback URL is validated once. For the submission view."""
    token = _validated_urls.set(set())
    try:
        yield
    finally:
        _validated_urls.reset(token)


def resolve_within_timeout(hostname: str, port: int) -> list[t.Any]:
    """Return what getaddrinfo answers for the name, or raise CallbackUrlError if it is too slow.

    A thread, because a blocking libc call cannot be interrupted any other way. It is a daemon and
    is never joined a second time: an abandoned resolver thread costs a stack until the resolver
    gives up, where waiting for it costs the request that is holding a worker.
    """
    answer: list[t.Any] = []
    failure: list[BaseException] = []

    # The slot is the thread's, not the caller's: an abandoned thread keeps it until the resolver
    # answers or gives up, which is what bounds the number of them. A caller who cannot get a slot
    # within the timeout is answered as a caller whose name cannot be resolved in time is.
    # bound to the object the slot was taken from, so that a thread abandoned by its caller gives
    # the slot back to that semaphore and not to whatever the name refers to by then
    slots = _resolver_slots
    if not slots.acquire(timeout=CALLBACK_RESOLVE_TIMEOUT_SECONDS):
        msg = f"callback_url hostname could not be resolved within {CALLBACK_RESOLVE_TIMEOUT_SECONDS:.0f} seconds"
        raise CallbackUrlError(msg)

    def resolve() -> None:
        try:
            answer.extend(socket.getaddrinfo(hostname, port))
        except Exception as ex:  # noqa: BLE001  # re-raised by the caller below, never swallowed
            failure.append(ex)
        finally:
            slots.release()

    thread = threading.Thread(target=resolve, name="callback-url-resolve", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        # no thread could be started; the slot would otherwise be lost with it
        slots.release()
        raise
    thread.join(CALLBACK_RESOLVE_TIMEOUT_SECONDS)

    if thread.is_alive():
        msg = f"callback_url hostname could not be resolved within {CALLBACK_RESOLVE_TIMEOUT_SECONDS:.0f} seconds"
        raise CallbackUrlError(msg)

    if failure:
        # Only the classes that describe the address itself become a rejection. Anything else is a
        # fault of this server -- getaddrinfo raises a plain OSError when the process is out of
        # file descriptors -- and must reach the error handler and the administrators, rather than
        # telling a caller with a perfectly good webhook that their hostname is wrong.
        if not isinstance(failure[0], (socket.gaierror, UnicodeError, ValueError)):
            raise failure[0]

        msg = f"callback_url hostname could not be resolved: {failure[0]}"
        raise CallbackUrlError(msg) from failure[0]

    return answer


def validate_callback_url(url: str) -> str:
    """Return the URL if it is a safe public https endpoint, otherwise raise CallbackUrlError.

    Resolution happens here and again just before the request is sent. That does not close the
    window entirely — a name can resolve differently between the two checks (DNS rebinding) — so
    the request itself is also sent without following redirects and without credentials, and its
    body is never surfaced to the user.

    Within the scope that callback_urls_validated_once() opens, the second and later checks of one
    URL are answered from the first. See _validated_urls.
    """
    if not url:
        msg = "callback_url must not be empty"
        raise CallbackUrlError(msg)

    validated = _validated_urls.get()
    if validated is not None and url in validated:
        return url

    if len(url) > MAX_CALLBACK_URL_LENGTH:
        msg = f"callback_url must be at most {MAX_CALLBACK_URL_LENGTH} characters"
        raise CallbackUrlError(msg)

    parsed = urlparse(url)

    # http is allowed only in development, where the callback target is typically a local script
    allowed_schemes = ("https", "http") if settings.DEBUG else ("https",)
    if parsed.scheme not in allowed_schemes:
        msg = f"callback_url must use {' or '.join(allowed_schemes)}"
        raise CallbackUrlError(msg)

    if not parsed.hostname:
        msg = "callback_url must include a hostname"
        raise CallbackUrlError(msg)

    if parsed.username or parsed.password:
        msg = "callback_url must not contain credentials"
        raise CallbackUrlError(msg)

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as ex:
        # urlsplit resolves the port lazily and raises for one that is not a number or is out of
        # range, and Django's URLValidator accepts both. Guarded here because the caller reports
        # only CallbackUrlError, so a bare ValueError would be a server error over a bad address.
        msg = f"callback_url has an invalid port: {ex}"
        raise CallbackUrlError(msg) from ex

    addrinfo = resolve_within_timeout(parsed.hostname, port)

    # skipped in development, where a developer's callback target is usually on their own machine.
    # Written as a guarded block rather than an early return, so that both paths reach the scope.
    if not settings.DEBUG:
        for family, _type, _proto, _canonname, sockaddr in addrinfo:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            # sockaddr is (host, port) for IPv4 and (host, port, flowinfo, scopeid) for IPv6; the
            # stubs type the first element as str | int because of AF_UNIX
            host = sockaddr[0]
            # the same definition of "public" as the GeoIP lookup in views.client_location_fields,
            # which must skip exactly the addresses this refuses; see the note on drift in netaddr
            if isinstance(host, str) and not address_is_public(host):
                msg = "callback_url must resolve to a public address"
                raise CallbackUrlError(msg)

    if validated is not None:
        validated.add(url)

    return url


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a redirect is a second URL that was never validated."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: t.IO[bytes],
        code: int,
        msg: str,
        headers: email.message.Message,
        newurl: str,
    ) -> None:
        return None


def send_task_callback(task: "Task", logfunc: t.Callable[[t.Any], None]) -> bool:
    """POST a completion notification for a finished task. Return whether it was accepted.

    Never raises: a callback that fails is logged and dropped. Retrying is deliberately not
    attempted — the task is already finished and recorded, the client can still poll, and a retry
    loop against an unresponsive endpoint would tie up the runner.
    """
    if not task.callback_url:
        return False

    try:
        url = validate_callback_url(task.callback_url)
    except CallbackUrlError as ex:
        # the URL passed validation when the task was submitted, so this means DNS changed under it
        logfunc(f"Not sending callback for task {task.id}: {ex}")
        return False

    payload = {
        "task_id": task.id,
        "task_url": task.public_url(),
        "request_type": task.request_type,
        "finishtimestamp": task.finishtimestamp.isoformat() if task.finishtimestamp else None,
        "error_msg": task.error_msg,
        "success": not task.error_msg,
    }

    request = urllib.request.Request(  # noqa: S310 (the scheme is checked in validate_callback_url)
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "atlasserver-forcedphot-callback"},
        method="POST",
    )

    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=CALLBACK_TIMEOUT_SECONDS) as response:
            logfunc(f"Callback for task {task.id} returned HTTP {response.status}")
            return 200 <= response.status < 300
    except urllib.error.HTTPError as ex:
        # the endpoint answered, just not with a success status
        logfunc(f"Callback for task {task.id} returned HTTP {ex.code}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
        logfunc(f"Callback for task {task.id} failed: {ex}")
        return False
