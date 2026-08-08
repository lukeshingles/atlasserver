"""Optional completion callbacks for API-submitted tasks.

API clients have no way to be told that a task finished — result emails are skipped for
API-originated tasks — so every API user polls, which is a large share of the server's request
volume. A task may carry a callback_url that is POSTed a small JSON body once it finishes.

The URL comes from a user, so this is a server-side request forgery surface: without checks, a
caller could aim the server at a host only the server can reach and use the response status as an
oracle. validate_callback_url() is applied when the task is submitted and again immediately before
the request is sent.
"""

import json
import socket
import typing as t
import urllib.error
import urllib.request
from urllib.parse import urlparse

from django.conf import settings

from atlasserver.forcephot.netaddr import address_is_public

# a callback is a courtesy, not part of finishing the task: keep it short so that a slow or
# blackholed endpoint cannot hold a worker slot open
CALLBACK_TIMEOUT_SECONDS = 10

MAX_CALLBACK_URL_LENGTH = 500


class CallbackUrlError(ValueError):
    """Raised when a callback URL is missing, malformed or points somewhere it must not."""


def validate_callback_url(url: str) -> str:
    """Return the URL if it is a safe public https endpoint, otherwise raise CallbackUrlError.

    Resolution happens here and again just before the request is sent. That does not close the
    window entirely — a name can resolve differently between the two checks (DNS rebinding) — so
    the request itself is also sent without following redirects and without credentials, and its
    body is never surfaced to the user.
    """
    if not url:
        msg = "callback_url must not be empty"
        raise CallbackUrlError(msg)

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
        addrinfo = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError) as ex:
        msg = f"callback_url hostname could not be resolved: {ex}"
        raise CallbackUrlError(msg) from ex

    if settings.DEBUG:
        # a developer's callback target is usually on their own machine
        return url

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

    return url


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a redirect is a second URL that was never validated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_task_callback(task, logfunc: t.Callable[[t.Any], None]) -> bool:
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
        "task_url": f"https://fallingstar-data.com/forcedphot/queue/{task.id}/",
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
