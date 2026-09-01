"""Django views for the forcephot app."""

import contextlib
import datetime
import functools
import hashlib
import logging
import math
import os
import statistics
import typing as t
import uuid
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import override
from urllib.parse import urlsplit

import pandas as pd
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.contrib.auth.views import PasswordResetView
from django.contrib.gis.geoip2 import GeoIP2
from django.contrib.gis.geoip2 import GeoIP2Exception
from django.core.cache import caches
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Count
from django.db.models import Max
from django.db.models.functions import Trunc
from django.http import FileResponse
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseNotFound
from django.http import HttpResponseNotModified
from django.http import JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.cache import cache_control
from django.views.decorators.cache import cache_page
from django.views.decorators.cache import never_cache
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from drf_spectacular.utils import OpenApiResponse
from geoip2.errors import AddressNotFoundError
from geoip2.errors import GeoIP2Error
from maxminddb import InvalidDatabaseError
from rest_framework import filters
from rest_framework import permissions
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.exceptions import Throttled
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.serializers import BaseSerializer
from rest_framework.utils.urls import replace_query_param
from rest_framework.views import APIView

from atlasserver.forcephot.filters import TaskFilter
from atlasserver.forcephot.forms import email_is_taken
from atlasserver.forcephot.forms import EmailChangeForm
from atlasserver.forcephot.forms import RegistrationForm
from atlasserver.forcephot.forms import ResendVerificationForm
from atlasserver.forcephot.forms import ThrottledAuthenticationForm
from atlasserver.forcephot.misc import country_code_to_name
from atlasserver.forcephot.misc import country_region_to_name
from atlasserver.forcephot.misc import datetime_to_mjd
from atlasserver.forcephot.misc import make_pdf_plot
from atlasserver.forcephot.misc import PDF_PLOT_TIMEOUT_SECONDS
from atlasserver.forcephot.misc import resultplotdatajs_cachekey
from atlasserver.forcephot.misc import splitradeclist
from atlasserver.forcephot.models import PendingEmailVerification
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.netaddr import address_is_public
from atlasserver.forcephot.netaddr import client_ip
from atlasserver.forcephot.pagination import TaskPagination
from atlasserver.forcephot.queue import next_queuepos_relative
from atlasserver.forcephot.queue import request_recalc as request_queue_recalc
from atlasserver.forcephot.queue import typical_runtime_seconds
from atlasserver.forcephot.serializers import ForcePhotTaskSerializer
from atlasserver.forcephot.serializers import stack_requests_allowed
from atlasserver.forcephot.throttles import count_in_window
from atlasserver.forcephot.throttles import ForcedPhotRateThrottle
from atlasserver.forcephot.throttles import login_failures_exceeded
from atlasserver.forcephot.throttles import LOGIN_LIMIT_MESSAGE
from atlasserver.forcephot.throttles import note_login_failure
from atlasserver.forcephot.verification import load_email_change_token
from atlasserver.forcephot.verification import send_email_change_confirmation
from atlasserver.forcephot.verification import send_verification_email
from atlasserver.forcephot.verification import site_name as verification_site_name
from atlasserver.forcephot.verification import token_generator as verification_token_generator
from atlasserver.forcephot.verification import user_from_uidb64
from atlasserver.forcephot.webhooks import callback_urls_validated_once

# not atlasserver.taskrunner.main: importing that module runs django.setup() and pulls pandas and
# multiprocessing into every web worker, permanently, to read two constants
from atlasserver.taskrunner import status as runnerstatus

MAX_USER_IMGZIP_TASKS = 5
MAX_USER_TASKS = 500

# Shortest gap between two verification emails to the same address, see resend_verification.
RESEND_VERIFICATION_INTERVAL_SECONDS: t.Final = 60

# The same, for the address-change confirmations one account can ask for. See change_email.
EMAIL_CHANGE_INTERVAL_SECONDS: t.Final = 60

# None of the limiters below is a mutual exclusion. The production cache is file-based, where
# add() is has_key() followed by set() and a read-modify-write is not atomic either, so two
# simultaneous callers can both be admitted. They bound a sustained flood; all three are sized so.

# Registrations allowed from one client address per window. A budget rather than a single slot,
# because this service's users arrive from universities behind shared addresses and two colleagues
# signing up together is ordinary; what it has to stop is a loop. See register.
REGISTRATION_WINDOW_SECONDS: t.Final = 600
REGISTRATION_WINDOW_LIMIT: t.Final = 3

# Submissions of any kind allowed from one client address per window, valid or not.
#
# RegistrationForm.clean_email answers "is this address registered here?", and that answer is only
# reachable on the invalid branch. A limiter that ran after is_valid() therefore metered the mail
# and left the answer free, which is an enumeration oracle over the whole user table -- the leak
# EmailChangeForm.clean_new_email and resend_verification both close.
#
# Looser than the budget above, and separate from it, because the two bound different things. That
# one bounds outgoing mail. This one bounds probes, and a mistyped password must not use up a
# colleague's registration.
REGISTRATION_ATTEMPT_LIMIT: t.Final = 20

# How many PDF plot renders the whole site may have in flight at once, across every task.
#
# The per-task lock in taskpdfplot bounds the renders of one task. This bounds the total. A render
# holds its request thread for up to PDF_PLOT_TIMEOUT_SECONDS while a spawned interpreter starts
# and imports matplotlib. Without a total, requests for a few tasks that have no .pdf yet could
# occupy every worker at once, and the site would answer nothing, the login page included. The
# view needs no credentials and is not a DRF view, so no throttle reaches it. Production serves
# the site with four single-threaded workers (ATLASSERVER_NPROCESSES in dotenv_example.txt), so
# this leaves most of them free whatever a crawler or a flood does.
#
# Named slots rather than a counter, for the reason given above: a read-modify-write on the
# file-based cache is not atomic, while add() does at least fail for a name that is already taken.
PDF_PLOT_RENDER_SLOTS: t.Final = 2

logger = logging.getLogger(__name__)


def entity_tag(*parts: object) -> str:
    """Return the parts as one strong entity-tag, valid whatever they contain.

    RFC 7232 allows only %x21 and %x23-7E between the quotes, so an entity-tag may hold neither a
    space nor a double quote -- and the parts handed to this hold both. str() of a datetime is
    "2026-08-11 12:34:56+00:00", and one caller mixes in request.get_full_path(), which is whatever
    the client asked for. An intermediary is entitled to drop a malformed header, which would cost
    the revalidation these exist for.

    Hashed rather than escaped, because that also bounds the header: the task list's tag grew with
    the query string, and nothing reads the parts back out -- an entity-tag is compared, not parsed.
    """
    # \x1f between them, so that ("a", "bc") and ("ab", "c") cannot produce the same tag
    joined = "\x1f".join(str(part) for part in parts)

    return f'"{hashlib.blake2b(joined.encode(), digest_size=16).hexdigest()}"'


def origin_only_referrer(view):
    """Serve every response of a view with Referrer-Policy: strict-origin.

    For the pages whose own URL carries a token. Those pages already opt out of analytics, which
    stops the token being reported for the page itself, but they still carry the site's navigation:
    following any of it sends the token-bearing URL as the Referer under the default same-origin
    policy, and the destination page does run analytics, where document.referrer would hand the
    token to a third party who could then replay it. strict-origin trims the Referer to the bare
    origin -- scheme and host, never the path the token lives in -- and sends nothing at all on an
    HTTPS-to-HTTP downgrade.

    strict-origin and not no-referrer, although no-referrer sounds like the stronger spelling of
    the same intent: these pages confirm by POSTing to their own URL, and the referrer policy of
    the page hosting a form also governs the Origin header its submission carries. Under
    no-referrer the Fetch spec serialises Origin as "null" even for a same-origin POST, and
    Django's CSRF middleware, finding an Origin header present, matches it against the trusted
    origins -- which "null" never is. Every real browser was answered 403 on the activation
    button (the test client does not emulate referrer policy, so only browsers saw it). Under
    strict-origin the same-origin POST carries its true Origin, and the Referer fallback for
    browsers that omit Origin is the bare origin, which passes too.

    Applied to the view rather than to the pages it renders, so that it covers the invalid-link and
    success responses too -- the token is in the URL whatever the answer turns out to be.
    """

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["Referrer-Policy"] = "strict-origin"
        return response

    return wrapper


def get_tasklist_etag(request, user_id: int, *, shows_owner_fields: bool = True) -> str:
    """Return an etag that changes whenever anything the given user's task pages show changes.

    `shows_owner_fields` says which of the two bodies this tag describes. The serializer keeps
    callback_url to the owner and to staff, so one URL has two representations; without this term
    the tag would be the same for both, and a caller presenting the other one's tag would be
    answered 304 with a body that was never meant for them.

    Scoped to one user's tasks rather than the whole table. A global aggregate was invalidated by
    every other user's activity, so a busy server would almost never produce a 304, and it cost a
    scan of every row rather than of one user's.

    Aggregating over the user (rather than over the rows of the page being returned) is what makes
    this cheap: it needs no knowledge of the page, so the caller can answer a conditional request
    before fetching or serialising anything. It also covers state that a page's own rows do not,
    such as an IMGZIP child finishing when it is reported on its parent's row but paginated
    elsewhere.
    """
    # one aggregate query for all of it. task_modified_datetime (auto_now) covers edits that change
    # none of the other timestamps, and the count covers deletions, which move no timestamp at all.
    # calculate_queue_positions() writes task_modified_datetime explicitly for the same reason: it
    # renumbers with a queryset .update(), which does not trigger auto_now.
    usertasks = Task.objects.filter(user_id=user_id).aggregate(
        Count("id"),
        Max("timestamp"),
        Max("starttimestamp"),
        Max("finishtimestamp"),
        Max("task_modified_datetime"),
    )

    return entity_tag(
        request.accepted_renderer.format,
        user_id,
        request.get_full_path(),
        shows_owner_fields,
        usertasks["id__count"],
        usertasks["timestamp__max"],
        usertasks["starttimestamp__max"],
        usertasks["finishtimestamp__max"],
        usertasks["task_modified_datetime__max"],
        # the queue position rendered for a task is relative to the front of the global queue
        Task.min_queuepos_relative(),
    )


# the URLs the queue page polls (api_url_base and friends) are built inside tasklist-react.html
# from {% url %} and the request, not passed in as view context: every action of the viewset can
# render that template (a plain browser-form POST receives it via the 201 response), and an action
# that forgot to supply them produced a page that polled fetch('') against itself


def cached_by_file_stat[T](cache: dict[Path, tuple[float, int, T]], path: Path, build: Callable[[Path], T]) -> T:
    """Return build(path), reusing the last result until the file's mtime or size changes.

    Turns a repeated read (or a repeated open, for the GeoIP database) into a stat, without the
    staleness that keeping the value forever would bring: both callers here read a file that is
    replaced underneath a long-lived worker process, one by a deployment and one by cron.

    No lock. Two threads racing build the value twice and one wins the dict, which costs the extra
    work rather than a wrong answer.
    """
    stat = path.stat()

    cached = cache.get(path)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    value = build(path)
    cache[path] = (stat.st_mtime, stat.st_size, value)
    return value


_geoip_reader_cache: dict[Path, tuple[float, int, GeoIP2]] = {}


def geoip_reader() -> GeoIP2:
    """Return a reader for the city database, rebuilding it only when the file changes.

    Constructing one opens and memory-maps the database, which was being paid again on every
    request that created a task. Not held forever, because update_geoipdatabase.sh replaces the
    database from cron and a reader kept across the replacement answers from the mapping it opened
    for the rest of the worker process's life.

    The path is resolved here rather than left to GeoIP2's own directory search, because there is
    nothing to stat until it has been. GEOIP_CITY is Django's own setting for the file name.
    """
    path = Path(settings.GEOIP_PATH, getattr(settings, "GEOIP_CITY", "GeoLite2-City.mmdb"))

    return cached_by_file_stat(_geoip_reader_cache, path, lambda dbpath: GeoIP2(path=dbpath))


def geoip_reader_forget() -> None:
    """Drop any held reader, so the next lookup reopens the database.

    The seam tests reset the module state through, rather than reaching into the private dict.
    """
    _geoip_reader_cache.clear()


def client_location_fields(request) -> dict[str, str | None]:
    """Return the country_code/region fields for the request's client IP, when it can be located.

    One implementation rather than one per task-creating view: the private-range list and the
    header handling drifted apart once before, and a divergence here quietly feeds wrong countries
    into the usage statistics.

    Never raises. This decorates a task with a statistic, so no failure of it is worth failing a
    submission for — an unresolvable lookup used to escape as a 500 (and an admin email) from the
    middle of task creation.
    """
    # str | None values: the GeoIP database does not know a region for every address, and the
    # model fields are nullable, so a None is stored as-is rather than dropped
    fields: dict[str, str | None] = {}

    ip = client_ip(request)
    # the shared definition, not a second copy of it: the database cannot place any address that
    # webhooks refuses to send to, and the two used to disagree because this end carried a
    # hand-written prefix list that missed 172.16/12 and every IPv6 form
    if ip is None or not address_is_public(ip):
        return fields

    try:
        location = geoip_reader().city(ip)
    except AddressNotFoundError:
        # a public address the database simply does not list (an unallocated or anonymised range,
        # or most IPv6). Routine, and silent for that reason: logging it would put a line in the
        # log for a share of perfectly ordinary submissions and bury the failures below in them
        return fields
    except (GeoIP2Exception, GeoIP2Error, InvalidDatabaseError, OSError):
        # the database is missing, unreadable or corrupt (OSError covers the stat in geoip_reader,
        # which is where a deleted file surfaces). Unlike the case above this is a real fault, so
        # it is reported at error level, where the "atlasserver" logger writes it to the log file.
        # Deliberately not mail_admins: a database that has gone bad would mail on every request.
        logger.exception("GeoIP lookup failed for %s", ip)
        return fields

    fields["country_code"] = location["country_code"]
    fields["region"] = location["region_code"]

    return fields


def request_is_from_api(request) -> bool:
    """Return whether a request came from a script rather than from the queue page.

    This decides more than a statistic: a task marked from_api is never sent a result email (API
    clients are expected to use the completion callback or to poll), so getting it wrong silently
    costs a web user the email they asked for. The test used to be whether a Referer header was
    present, which a browser is free not to send — a user behind a no-referrer policy submitted from
    the web form, ticked "Email me when completed", and never heard anything back.

    Which authenticator succeeded is not a guess: the queue page submits with a session cookie,
    while the documented API examples authenticate with a token (or Basic).
    """
    return not isinstance(getattr(request, "successful_authenticator", None), SessionAuthentication)


class ForcePhotPermission(permissions.BasePermission):
    """Object-level permission to only allow owners of an object to change it.

    Assumes the model instance has a `user` attribute.
    """

    message = "You must be the owner of this object."

    @override
    def has_permission(self, request, view) -> bool:
        return bool(request.method in permissions.SAFE_METHODS or request.user.is_authenticated)

    @override
    def has_object_permission(self, request, view, obj) -> bool:
        # Reading a task is public, including to anonymous callers, and is meant to be: a task and
        # the results it produced describe measurements from a public survey, so a link to one can
        # be shared with somebody who has no account here. Only changing it is restricted.
        # (The task *list* is a different question and stays scoped to the requesting user: it is
        # a person's own queue, not a public index.)
        if request.method in permissions.SAFE_METHODS:
            return True

        # staff and instance owner have all permissions; the rule itself lives on the model,
        # because RequestImages and the serializer's callback_url filter apply the same one
        return obj.is_owned_by(request.user)


class ForcePhotTaskViewSet(viewsets.ModelViewSet[Task]):
    """API endpoint that allows force.sh tasks to be created and deleted."""

    # the prefetch feeds Task._imagerequest_task(), so serialising a page of tasks costs one query
    # for the whole page rather than one (in practice three) per task
    queryset = (
        Task.objects.all()
        .order_by("-timestamp", "-id")
        .select_related("user", "parent_task")
        .prefetch_related(Task.prefetch_imagerequests())
    )
    serializer_class = ForcePhotTaskSerializer
    permission_classes = [ForcePhotPermission]
    throttle_scope = "forcephottasks"
    ordering_fields = ["timestamp", "id"]
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    filterset_class = TaskFilter
    ordering = "-id"
    template_name = "tasklist-react.html"

    @override
    def create(self, request: Request, *args: t.Any, **kwargs: t.Any) -> Response:
        """Create new tasks if the user is authenticated and the request is valid."""
        if not request.user.is_authenticated or self.request.user.pk is None:
            raise PermissionDenied

        usertaskcount = Task.live().filter(starttimestamp__isnull=True, user_id=self.request.user.pk).count()

        # Each callback URL is resolved once for the whole submission: splitradeclist validates
        # every row, and the serializer validates the list again. See webhooks._validated_urls.
        with callback_urls_validated_once():
            # a radeclist can hold up to 100 targets, so the limit must account for the size of
            # this request rather than only the tasks that are already queued
            if "radeclist" in request.data:
                datalist = splitradeclist(request.data)
                newtaskcount = len(datalist)
                serializer = self.get_serializer(data=datalist, many=True)
            else:
                newtaskcount = 1
                serializer = self.get_serializer(data=request.data)

            if usertaskcount + newtaskcount > MAX_USER_TASKS:
                msg = (
                    f"ERROR: You have too many queued tasks ({usertaskcount} queued"
                    f" + {newtaskcount} requested > {MAX_USER_TASKS})."
                )
                raise serializers.ValidationError({"non_field_errors": msg})
            serializer.is_valid(raise_exception=True)

        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @override
    def perform_create(self, serializer: BaseSerializer[Task]) -> None:
        """Create new task(s)."""
        usertaskcount_before = (
            Task.live().filter(starttimestamp__isnull=True, user_id=self.request.user.pk).count()
            if self.request.user and self.request.user.pk is not None
            else 0
        )

        extra_fields: dict[str, Any] = {
            "user": self.request.user,
            "timestamp": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        }

        extra_fields.update(client_location_fields(self.request))
        extra_fields["from_api"] = request_is_from_api(self.request)

        # One transaction for the inserts and the positions. Each insert is otherwise committed on
        # its own with queuepos_relative NULL, and the runner polls twice a second with
        # order_by("queuepos_relative"), where NULL sorts first: a task submitted a moment ago was
        # dispatched ahead of everything already waiting, which is the window
        # next_queuepos_relative() exists to close.
        with transaction.atomic():
            serializer.save(**extra_fields)

            # take the ids from the saved objects rather than serializer.data: accessing .data here
            # would cache the response body before the updates below have been applied
            # save() above populated this, so it is not None; many=True makes it a list. Narrowed
            # rather than assumed, because everything below indexes into it.
            created = serializer.instance
            newtasks: list[Task] = list(created) if isinstance(created, list) else ([created] if created else [])

            # Provisional positions at the back of the queue. The real ordering is round-robin
            # across users and is assigned by the task runner; this only stops a task submitted
            # between two renumberings from showing no position at all, and costs one aggregate
            # for the whole batch.
            nextqueuepos = next_queuepos_relative()

            # set on the instances as well as written, so that the response serialises the values
            # without a reload per task: a radeclist can hold 100 targets
            for i, task in enumerate(newtasks):
                task.userqueuedtasks_on_submit = usertaskcount_before + i
                task.queuepos_relative = nextqueuepos + i

            # one statement rather than an UPDATE per task
            Task.objects.bulk_update(newtasks, ["userqueuedtasks_on_submit", "queuepos_relative"])

        request_queue_recalc()

    @override
    def perform_update(self, serializer: BaseSerializer[Task]) -> None:
        """Update a task."""
        # don't pass user=request.user here: a staff user editing someone else's task would
        # otherwise take ownership of it
        serializer.save()

    @override
    def perform_destroy(self, instance: Task) -> None:
        """Delete a task, and if the task is queued (not finished), then update queue positions."""
        update_queue_positions = not instance.finishtimestamp
        instance.delete()
        if update_queue_positions:
            request_queue_recalc()

    @override
    # pyrefly: ignore [bad-override]
    # returns HttpResponseNotModified/NotFound as well as Response, which the
    # base's annotation does not allow but DRF supports: finalize_response passes
    # anything that is not a Response straight through.
    def list(self, request: Request, *args: t.Any, **kwargs: t.Any):
        """List tasks belonging to the current user."""
        if request.user.is_authenticated:
            listqueryset = self.filter_queryset(self.get_queryset().filter(is_archived=False, user=request.user))
        else:
            listqueryset = Task.objects.none()
            raise PermissionDenied

        if request.accepted_renderer.format == "html":
            # only what the template reads: it builds its URLs and its user id itself. A COUNT
            # for a "usertaskcount" it never rendered was paid on every load of the queue page.
            return Response(
                template_name=self.template_name,
                data={"name": "Task Queue", "allow_stack_rock": stack_requests_allowed({"request": request})},
            )

        # answer a conditional request before paginating: an unchanged page then costs one
        # aggregate instead of a count, a page fetch, a prefetch and a full serialisation.
        # the anonymous case raised PermissionDenied above, so this is set -- stated as a check
        # rather than a comment, because get_tasklist_etag would otherwise take an Optional
        userpk = request.user.pk
        if userpk is None:
            raise PermissionDenied

        etag = get_tasklist_etag(request, userpk)
        if etag == request.META.get("HTTP_IF_NONE_MATCH"):
            return HttpResponseNotModified()

        page = self.paginate_queryset(listqueryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            paginator = self.paginator
            if not isinstance(paginator, TaskPagination):
                msg = f"expected TaskPagination, got {type(paginator).__name__}"
                raise TypeError(msg)
            return paginator.get_paginated_response(serializer.data, headers={"ETag": etag})

        return Response(self.get_serializer(listqueryset, many=True).data)

    @override
    # pyrefly: ignore [bad-override]
    # returns HttpResponseNotModified/NotFound as well as Response, which the
    # base's annotation does not allow but DRF supports: finalize_response passes
    # anything that is not a Response straight through.
    def retrieve(self, request: Request, *args: t.Any, **kwargs: t.Any):
        instance = self.get_object()
        if instance.is_archived:
            return HttpResponseNotFound("Page not found")

        if request.accepted_renderer.format == "html":
            return Response(
                template_name=self.template_name,
                # self.get_object() again here would repeat the lookup query
                data={"name": f"Task {instance.id}", "allow_stack_rock": stack_requests_allowed({"request": request})},
            )

        # scoped to the task's owner, not to request.user: a staff member may be viewing someone
        # else's task, and their own tasks say nothing about whether this one has changed
        # the detail view is readable by anyone, so the body depends on who is asking; the list
        # is scoped to the requester, so its body is always the owner's
        etag = get_tasklist_etag(request, instance.user_id, shows_owner_fields=instance.is_owned_by(request.user))
        if etag == request.META.get("HTTP_IF_NONE_MATCH"):
            return HttpResponseNotModified()

        return Response(self.get_serializer(instance).data, headers={"ETag": etag})


class RequestImages(APIView):
    permission_classes = [ForcePhotPermission]
    # the same budget as the viewset's create: this creates a task too, and ran a GeoIP lookup, an
    # aggregate and an insert per call with no limit at all
    throttle_scope = "forcephottasks"

    # this view takes no request body and redirects on success, so the schema generator cannot
    # infer a serializer for it
    @extend_schema(
        request=None,
        responses={
            302: OpenApiResponse(description="Image request created; redirects to the task list"),
            404: OpenApiResponse(description="No such finished forced photometry task"),
            429: OpenApiResponse(description="Too many image requests already queued"),
        },
        summary="Request the images behind a finished forced photometry task",
    )
    def post(self, request, pk):
        """Create an image request task for a finished forced photometry task.

        This creates a task, so it must not be reachable by GET: a safe method is not CSRF
        protected, and any page (or link prefetcher) could otherwise queue work for a logged-in user.
        """
        # no is_authenticated test here: ForcePhotPermission.has_permission refuses an anonymous
        # POST before this runs, and the object check below applies the ownership rule
        redirurl = reverse("task-list")

        # One transaction, with the parent row locked for its extent. Two overlapping requests
        # for one parent cannot then both find no live image request, and Task.delete takes the
        # same lock, so a delete of the parent waits until the copy of its data file below is
        # done rather than reclaiming the file under it.
        with transaction.atomic():
            try:
                parent_task = Task.live().select_for_update().get(id=pk, request_type="FP")
            except ObjectDoesNotExist:
                return HttpResponseNotFound("Page not found")

            self.check_object_permissions(request, parent_task)

            # One live image request per task. A second one is the same request again -- the child
            # inherits every field from the parent -- and it competes with the first for the result
            # file the runner writes for that parent. The queue page offers the button only while
            # there is none; this is the same rule for a caller that does not go through the page.
            if parent_task.live_imagerequest_ids():
                return JsonResponse(
                    {"non_field_errors": "An image request for this task is already queued."}, status=409
                )

            userimziptaskcount = Task.live().filter(user_id=request.user.pk, request_type="IMGZIP").count()
            if userimziptaskcount >= MAX_USER_IMGZIP_TASKS:
                msg = (
                    f"You have too many IMGZIP tasks ({userimziptaskcount} >= {MAX_USER_IMGZIP_TASKS})."
                    " Delete some before making new requests."
                )
                return JsonResponse({"non_field_errors": msg}, status=429)

            if not parent_task.error_msg and parent_task.finishtimestamp:
                # which fields the child inherits is the model's decision; see Task.new_imagerequest
                newtask = parent_task.new_imagerequest(user=request.user, from_api=request_is_from_api(request))
                for field, value in client_location_fields(self.request).items():
                    setattr(newtask, field, value)
                newtask.queuepos_relative = next_queuepos_relative()
                newtask.save()

                # the request's own copy of the data file, made now that it has an id. Without the
                # file nothing could ever be fetched, so the row is rolled back rather than queued
                # for a run that is certain to fail.
                if not newtask.copy_parent_datafile():
                    transaction.set_rollback(True)
                    msg = "The forced photometry data file for this task is no longer available."
                    return JsonResponse({"non_field_errors": msg}, status=404)

                request_queue_recalc()
                redirurl = replace_query_param(reverse("task-list"), "newids", str(newtask.id))

        return redirect(redirurl, request=request)


@cache_page(60 * 60 * 24, cache="usagestats")
def statscoordchart(request):
    # deferred for the same reason as atlasserver.taskrunner.main above: bokeh pulls in numpy and a
    # good deal else, and a module-scope import puts all of it permanently in every web worker to
    # serve this endpoint and statsusagechart, both of which are cached for a day
    import bokeh.models
    import bokeh.plotting
    from bokeh.embed import json_item

    tasks = list(Task.objects.all().order_by("-timestamp")[:20000].select_related("user"))

    dictsource: dict[str, Any] = {
        "ra": [tsk.ra for tsk in tasks],
        "dec": [tsk.dec for tsk in tasks],
        "taskid": [tsk.id for tsk in tasks],
        "username": [tsk.user.username for tsk in tasks],
    }
    source = bokeh.plotting.ColumnDataSource(dictsource)

    plot = bokeh.plotting.figure(
        tools="pan,wheel_zoom,box_zoom,reset",
        aspect_ratio=2,
        # the sky is dark on both themes, but the margin around it takes the colour of the page.
        # theme.js gives the title, the axes and the outline the colours that go with that page
        background_fill_color="#040154",
        border_fill_color=None,
        active_scroll="wheel_zoom",
        title="Recently requested coordinates",
        x_axis_label="Right ascension (deg)",
        y_axis_label="Declination (deg)",
        x_range=bokeh.models.Range1d(start=0, end=360),
        y_range=bokeh.models.Range1d(start=-90.0, end=90.0),
        sizing_mode="stretch_both",
        output_backend="webgl",
    )

    plot.grid.visible = False

    # The toolbar of this figure is inside a shadow root, which no rule in main.css can reach. The
    # logo therefore goes here, where bokeh builds the toolbar.
    plot.toolbar.logo = None

    rcirc = plot.circle(
        "ra", "dec", source=source, color="white", radius=0.05, hover_color="orange", alpha=0.7, hover_alpha=1.0
    )

    plot.add_tools(
        bokeh.models.HoverTool(
            tooltips="Task @taskid, RA Dec: @ra @dec, user: @username",
            mode="mouse",
            point_policy="follow_mouse",
            renderers=[rcirc],
        )
    )

    # json_item rather than components: the page builds the chart itself with
    # Bokeh.embed.embed_item, which lets theme.js colour the models before bokeh draws them
    return JsonResponse({"item": json_item(plot)})


# The colours of the four series of statsusagechart, from the bottom of a stack outward.
#
# These are the data, so they are held here rather than in theme.js with the colours of the frame.
# One step of each hue serves both themes: each clears the lightness band, the chroma floor and the
# 3:1 contrast floor against the white page and against the #212529 dark one, and each touching
# pair separates by dE 8.4 or more under protanopia and deuteranopia.
#
# The red is a state and not a fourth series. It marks work the task runner picked up and did not
# finish, which is a fact about the server rather than a busier version of the queue, so it is held
# apart from the three hues that carry the ordinary series.
USAGE_SERIES = (
    ("fp", "Finished, photometry", "#3987e5"),
    ("img", "Finished, images", "#199e70"),
    ("queue", "Queued", "#c98500"),
    ("stall", "Attempted, unfinished", "#d03b3b"),
)

# The part of each half of statsusagechart that is left empty next to the zero line. Without it the
# two arms meet, and one day reads as a single bar through the axis rather than as two.
USAGE_ARM_SPLIT = 0.031

# The radius of the corners a stack ends with.
USAGE_CAP_RADIUS = 3


def _usage_arm_ticks(peak: float) -> list[float]:
    """Return the tick values from 0 to the first round number at or above `peak`, in tasks.

    The step is 1, 2, 2.5 or 5 times a power of ten, whichever first gives three or fewer steps.

    A step must be a whole number of tasks, so the magnitude stops at one and a step of 2.5 is
    passed over. A fraction of a task cannot be labelled: the labels are written to no decimal
    place, which put "2" on the tick at 2.5 and "8" on the tick at 7.5.

    The last tick is what its half of the chart is drawn against, and it is at or above the peak.
    Thus the tallest bar of a half stops below the top gridline rather than at the frame.
    """
    if peak <= 0:
        return [0.0]

    magnitude = 10 ** max(0, math.floor(math.log10(peak / 3)))
    step = next(
        m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= peak / 3 and float(m * magnitude).is_integer()
    )

    return [n * step for n in range(math.ceil(peak / step) + 1)]


# the same 15 minutes as statsshortterm, which windows over the same data. This was 30 seconds,
# which had the most expensive chart on the stats page being rebuilt from a 14-day scan of the task
# table for practically every visitor.
@cache_page(60 * 15, cache="usagestats")
def statsusagechart(request):
    # deferred: see statscoordchart
    import bokeh.models
    import bokeh.plotting
    from bokeh.embed import json_item

    days_back = 14

    def get_days_ago_counts(tasks) -> list[float]:
        taskcounts = (
            tasks.annotate(queueday=Trunc("timestamp", "day")).values("queueday").annotate(taskcount=Count("id"))
        )
        dictcounts = {(today - task["queueday"]).total_seconds() // 86400: task["taskcount"] for task in taskcounts}

        return [dictcounts.get(d, 0.0) for d in reversed(range(days_back))]

    today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    windowstart = today - datetime.timedelta(days=days_back)

    unfinished = Task.objects.filter(timestamp__gt=windowstart, finishtimestamp__isnull=True)
    finished = Task.objects.filter(timestamp__gt=windowstart, finishtimestamp__isnull=False)

    # A task the runner has started at least once and has not finished is a different fact from a
    # task that is waiting its turn: the first says the runner is letting go of work it picked up.
    # main.py increments attempt_count as it starts a task, so a count above zero is that signal.
    counts = {}
    for origin, from_api in (("api", True), ("web", False)):
        # Each half of the chart counts the image requests of its own submitters. An unfiltered
        # IMGZIP series put every image request on the web side, so a task that waited on the API
        # side moved across when it finished.
        #
        # The image series covers every request type that is not forced photometry, which is what
        # keeps a day's total from falling as its tasks finish: the two unfinished series count a
        # task of any type, so a type that no finished series counted would leave the queued band
        # and reappear nowhere. Task.RequestType has a third member, the Solar System image stack.
        counts[origin] = {
            "fp": get_days_ago_counts(finished.filter(from_api=from_api, request_type="FP")),
            "img": get_days_ago_counts(finished.filter(from_api=from_api).exclude(request_type="FP")),
            "queue": get_days_ago_counts(unfinished.filter(from_api=from_api, attempt_count=0)),
            "stall": get_days_ago_counts(unfinished.filter(from_api=from_api, attempt_count__gte=1)),
        }

    # The API carries about a hundred times the traffic of the web form, so the two halves cannot
    # share a scale: on one axis the web bars are a line on the floor. Each half is therefore drawn
    # against its own gridlines, and their labels name the tasks that each half stands for.
    peaks = {
        origin: max(sum(series[day] for series in counts[origin].values()) for day in range(days_back))
        for origin in counts
    }
    armticks = {origin: _usage_arm_ticks(peak) for origin, peak in peaks.items()}

    data: dict[str, Any] = {
        "queueday": [(today - datetime.timedelta(days=d)).strftime("%b %d") for d in reversed(range(days_back))]
    }

    for origin, direction in (("api", 1.0), ("web", -1.0)):
        # A half is scaled to its top gridline and not to its peak, which is what leaves the room
        # above the tallest bar that the name of the half is written in.
        top = armticks[origin][-1]
        # the axis units that one task is worth in this half, after the gap at the zero line
        unit = (1.0 - USAGE_ARM_SPLIT) / top if top else 0.0

        # the hover tool reads the tasks, and the bars read the axis units they are drawn in
        for key, _label, _color in USAGE_SERIES:
            data[f"{origin}_{key}"] = counts[origin][key]
            data[f"{origin}_{key}_near"] = []
            data[f"{origin}_{key}_far"] = []

        # The outermost segment of a stack is drawn by a bar of its own, so that the corners it
        # ends with can be rounded: bokeh gives a renderer one border_radius for all of its bars,
        # and which of the four series is outermost changes from day to day.
        #
        # It is the whole segment and not the last stretch of it. A bar that covered only the end
        # would round its corners onto the square end underneath, which is the same colour, and the
        # stack would still end square; a bar that stopped just short of it would leave a hairline
        # of page between the two.
        capcolor: list[str] = []
        capnear: list[float] = []
        capfar: list[float] = []

        for day in range(days_back):
            edge = USAGE_ARM_SPLIT
            outermost = None

            for key, _label, seriescolor in USAGE_SERIES:
                count = counts[origin][key][day]
                start, edge = edge, edge + count * unit
                data[f"{origin}_{key}_near"].append(direction * start)
                data[f"{origin}_{key}_far"].append(direction * edge)

                if count > 0:
                    outermost = (key, start, seriescolor)

            if outermost is None:
                # a day with no tasks at all: there is no segment to end, and nothing is drawn
                capcolor.append(USAGE_SERIES[0][2])
                capnear.append(direction * edge)
                capfar.append(direction * edge)
                continue

            key, start, seriescolor = outermost
            data[f"{origin}_{key}_far"][day] = direction * start
            capcolor.append(seriescolor)
            capnear.append(direction * start)
            capfar.append(direction * edge)

        data[f"{origin}_cap_color"] = capcolor
        data[f"{origin}_cap_near"] = capnear
        data[f"{origin}_cap_far"] = capfar

    datasource = bokeh.plotting.ColumnDataSource(data=data)

    plot = bokeh.plotting.figure(
        x_range=bokeh.models.FactorRange(factors=data["queueday"]),
        y_range=bokeh.models.Range1d(start=-1.08, end=1.42),
        tools=[],
        toolbar_location=None,
        # A height in pixels rather than a ratio. The container gives a width and no height, and
        # bokeh solved the first layout of a ratio against it with a frame of no height at all:
        # the chart drew as a legend over an axis. A height it is given needs no solving.
        sizing_mode="stretch_width",
        height=430,
        # canvas rather than svg: bokeh's svg backend raises "not implemented" from roundRect, so
        # the cap that rounds the end of a stack draws as a square corner there. Nothing here asks
        # for vector output -- this figure has no toolbar, and so no save button
        output_backend="canvas",
        # transparent, so the figure sits on the page and follows the theme with it. theme.js
        # colours the axes, the gridlines, the legend, the names of the halves and the zero line
        background_fill_color=None,
        border_fill_color=None,
    )

    # the gridlines of one half are the ticks of that half alone, so they run across the frame and
    # the day boundaries do not
    plot.xgrid.visible = False

    # No box around the frame, and no rule or ticks along either axis: the gridlines carry the
    # scale, and a second set of lines around them marks nothing. theme.js colours the lines a
    # chart draws and leaves alone the ones it is given no colour for, so these stay off.
    plot.outline_line_color = None
    plot.axis.axis_line_color = None
    plot.axis.major_tick_line_color = None
    plot.axis.minor_tick_line_color = None

    # the bars of each half, in the order they stack away from the zero line
    bars: dict[str, list[Any]] = {}

    for origin in ("api", "web"):
        # The corners of a bar that face away from the zero line, which are the ones a stack is
        # capped with. In the order bokeh takes them, which is the order CSS takes them: top left,
        # top right, bottom right, bottom left. A radius is a whole number of pixels.
        radius = (
            (USAGE_CAP_RADIUS, USAGE_CAP_RADIUS, 0, 0)
            if origin == "api"
            else (0, 0, USAGE_CAP_RADIUS, USAGE_CAP_RADIUS)
        )
        bars[origin] = [
            plot.vbar(
                x="queueday",
                bottom=f"{origin}_{key}_near",
                top=f"{origin}_{key}_far",
                source=datasource,
                color=color,
                line_width=0.0,
                width=0.58,
            )
            for key, _label, color in USAGE_SERIES
        ]

        cap = plot.vbar(
            x="queueday",
            bottom=f"{origin}_cap_near",
            top=f"{origin}_cap_far",
            source=datasource,
            color=f"{origin}_cap_color",
            line_width=0.0,
            width=0.58,
        )
        # Through update() rather than as a keyword of vbar() or an assignment on the glyph.
        # bokeh's own signature for vbar() does not name border_radius, and its type stub gives the
        # glyph's border_radius the type of the property that holds it rather than the type of a
        # value for it, so both spellings are rejected by a type checker.
        cap.glyph.update(border_radius=radius)

        # one hover per half, so a day reports the counts of the half the pointer is over. The cap
        # answers for the end of the stack, which is the part of a bar a pointer meets first
        plot.add_tools(
            bokeh.models.HoverTool(
                tooltips=[("Day", "@queueday")]
                + [(label, f"@{origin}_{key}") for key, label, _color in reversed(USAGE_SERIES)],
                renderers=[*bars[origin], cap],
            )
        )

    # The ticks of the two halves are the same line of code twice over, but they are not the same
    # numbers: each half is scaled to its own gridlines, so a tick at the same height means about
    # a hundred times more tasks above the axis than below it.
    tickpositions = [0.0]
    ticklabels: dict[float | str, Any] = {0.0: "0"}
    for origin, direction in (("api", 1.0), ("web", -1.0)):
        ticks = armticks[origin]
        unit = (1.0 - USAGE_ARM_SPLIT) / ticks[-1] if ticks[-1] else 0.0
        for value in ticks[1:]:
            position = direction * (USAGE_ARM_SPLIT + value * unit)
            tickpositions.append(position)
            ticklabels[position] = f"{value:,.0f}"

    plot.yaxis.ticker = bokeh.models.FixedTicker(ticks=tickpositions)
    plot.yaxis.major_label_overrides = ticklabels

    # The zero line is the one thing the two halves share, so it is drawn once and for the pair
    # rather than as the axis of either one.
    plot.add_layout(bokeh.models.Span(location=0.0, dimension="width", line_width=1.5))

    # Each half names itself and its own peak. This is what keeps a reader from taking the two
    # heights as comparable, so it is not decoration.
    #
    # The upper name sits in the room the top gridline leaves inside the frame, beside the legend.
    # The lower one goes under the day labels, which is the axis label's own place: below the
    # bottom gridline there is only a margin, and a name written in it would sit on the lowest
    # bars.
    plot.add_layout(
        bokeh.models.Label(
            x=bokeh.models.Node(target="frame", symbol="top_right"),
            y=bokeh.models.Node(target="frame", symbol="top_right"),
            x_offset=-6,
            y_offset=-6,
            text=f"API \u2014 peak {peaks['api']:,.0f}/day",
            text_align="right",
            text_baseline="top",
            text_font_size="11px",
            text_font_style="bold",
        )
    )

    # A title in the panel below the frame, which is the one place under the day labels that text
    # can go: bokeh clips an annotation to the frame, so a Label here is not drawn at all, and an
    # axis label is centred on the end of the axis and runs off the right of the chart.
    plot.add_layout(
        bokeh.models.Title(
            text=f"Web \u2014 peak {peaks['web']:,.0f}/day",
            align="right",
            text_font_size="11px",
            text_font_style="bold",
        ),
        "below",
    )

    # One legend for the pair, built from the bars of the upper half. It lies along the top of the
    # frame: moved to the panel above the frame it left the frame no height at all, and the chart
    # drew as a legend over an axis.
    legend = bokeh.models.Legend(location="top_left", orientation="horizontal", border_line_width=0)
    # A series with no tasks in the fortnight is left out of it. On a healthy fortnight the runner
    # has attempted nothing and left it unfinished, and a colour that is nowhere on the chart is
    # not a key to the chart. Both halves are counted: a series the web form has no tasks for is
    # still named while the API has some.
    legend.items = [
        bokeh.models.LegendItem(label=label, renderers=[bar])
        for (key, label, _color), bar in zip(reversed(USAGE_SERIES), reversed(bars["api"]), strict=True)
        if any(sum(counts[origin][key]) for origin in counts)
    ]
    plot.add_layout(legend)

    # json_item rather than components: the page builds the chart itself with
    # Bokeh.embed.embed_item, which lets theme.js colour the models before bokeh draws them
    return JsonResponse({"item": json_item(plot)})


@cache_page(60 * 60 * 4, cache="usagestats")
def statslongterm(request):
    dictparams = {}
    now = datetime.datetime.now(datetime.UTC)
    thirtydaytasks = Task.objects.filter(timestamp__gt=now - datetime.timedelta(days=30))

    dictparams["thirtydaytasks"] = thirtydaytasks.count()
    dictparams["thirtydaytaskrate"] = "{:.1f}/day".format(dictparams["thirtydaytasks"] / 30.0)

    countrylist = (
        thirtydaytasks.filter(country_code__isnull=False)
        .exclude(country_code="XX")
        .values_list("country_code")
        .annotate(task_count=Count("country_code"), user_count=Count("user_id", distinct=True))
        .order_by("-task_count", "country_code")[:15]
    )

    dictparams["countrylist"] = [
        (country_code_to_name(code), task_count, user_count) for code, task_count, user_count in countrylist
    ]

    regionlist = (
        thirtydaytasks.filter(country_code__isnull=False)
        .exclude(country_code="XX")
        .values_list("country_code", "region")
        .annotate(task_count=Count("country_code"))
        .order_by("-task_count", "country_code", "region")[:15]
    )

    dictparams["regionlist"] = [
        (country_region_to_name(country_code, region), task_count) for country_code, region, task_count in regionlist
    ]

    dictparams["thirtyddayusers"] = thirtydaytasks.values_list("user_id").distinct().count()

    return render(request, "statslongterm.html", dictparams)


@cache_page(60 * 15, cache="usagestats")
def statsshortterm(request):
    now = datetime.datetime.now(datetime.UTC)
    sevendaytasks = Task.objects.filter(timestamp__gt=now - datetime.timedelta(days=7))
    sevendaytaskcount = sevendaytasks.count()

    dictparams: dict[str, t.Any] = {
        "sevendaytasks": sevendaytaskcount,
        "sevendayusers": sevendaytasks.values_list("user_id").distinct().count(),
    }
    dictparams["sevendaytaskrate"] = "{:.1f}/day".format(dictparams["sevendaytasks"] / 7.0)

    # excluding "" as well as NULL: both mean no MPC target, and task_mpc_name_not_blank keeps
    # anything else that would have meant it out of the column
    dictparams["sevendaympctasks"] = int(sevendaytasks.exclude(mpc_name__isnull=True).exclude(mpc_name="").count())
    dictparams["sevendayimgtasks"] = int(sevendaytasks.filter(request_type="IMGZIP").count())

    sevendaytasks_finished = sevendaytasks.filter(finishtimestamp__isnull=False)

    def mean_seconds(values: list[float | None]) -> float | None:
        """Return the mean of the values that have one, or None if none of them do."""
        known = [value for value in values if value is not None]
        return statistics.fmean(known) if known else None

    def mean_seconds_str(values: list[float | None]) -> str:
        """Format the mean of a list of second counts, ignoring the unanswerable ones and empties."""
        mean = mean_seconds(values)
        return "-" if mean is None else f"{mean:.1f}s"

    if sevendaytasks_finished.count() > 0:
        dictparams["sevendayavgwaittime"] = mean_seconds_str([tsk.waittime() for tsk in sevendaytasks_finished])

        sevendaytasks_finished_firstusertasks = sevendaytasks_finished.filter(userqueuedtasks_on_submit=0)
        dictparams["sevendayavgwaittimeuserfirst"] = mean_seconds_str(
            [tsk.waittime() for tsk in sevendaytasks_finished_firstusertasks]
        )

        sevenday_runtimes = [tsk.runtime() for tsk in sevendaytasks_finished]
        dictparams["sevendayavgruntime"] = mean_seconds_str(sevenday_runtimes)
        num_job_processors = runnerstatus.NUMSLOTS
        # one definition of which values count, shared with the string above
        sevenday_mean_runtime = mean_seconds(sevenday_runtimes) or 0.0
        dictparams["sevendayloadpercent"] = (
            f"{100.0 * sevendaytaskcount * sevenday_mean_runtime / (7 * 24.0 * 60 * 60) / num_job_processors:.1f}%"
        )
    else:
        dictparams |= {
            "sevendayavgwaittime": "-",
            "sevendayavgwaittimeuserfirst": "-",
            "sevendayavgruntime": "-",
            "sevendayloadpercent": "0%",
        }

    return render(request, "statsshortterm.html", dictparams)


@login_required
def queuepositions(request):
    """Return just the queue position of each of the current user's unfinished tasks.

    The queue page polls the full task list every couple of seconds, and most of the time the only
    thing that has changed is how far up the queue the user's tasks have moved. This endpoint
    answers that question with two indexed queries and a few hundred bytes, rather than a page of
    fully serialised tasks and the filesystem stats that go with them.
    """
    queueoffset = Task.min_queuepos_relative()
    # the same set the offset was measured against, so the subtraction below cannot drift
    positions = (
        Task.queued()
        .filter(user_id=request.user.pk, queuepos_relative__isnull=False)
        .values_list("id", "queuepos_relative", "request_type")
    )

    return JsonResponse(
        {
            "queuepositions": {str(taskid): queuepos - queueoffset for taskid, queuepos, _ in positions},
            # What each of those tasks is. Dispatch runs one task per user at a time, so a user's
            # own tasks ahead of this one are waited through in series -- and an IMGZIP among them
            # is a quarter of an hour of that wait where an FP task is a minute. Another column on
            # a query already being made, and only ever this user's own tasks.
            "queuedtypes": {str(taskid): requesttype for taskid, _, requesttype in positions},
            "queueoffset": queueoffset,
        }
    )


# A browser can answer a second request from its cache, for one write interval of the task runner.
# Each page of the site asks for this status at load. Thus several tabs that open together would
# make several requests for a file that changes every fifteen seconds. The interval stops there. A
# longer interval would need stale-while-revalidate, and this endpoint must not give that
# directive. runnerstatus.js asks again when a hidden tab becomes visible. That reader needs the
# status at that moment, and not the status from before the tab became hidden.
@cache_control(private=True, max_age=int(runnerstatus.STATUS_WRITE_SECONDS))
def taskrunnerstatus(request):
    """Report whether the task runner is alive and what it is doing.

    Reads the snapshot the runner writes every few seconds. `stale` is the field to alert on: it
    means the runner has not refreshed the file recently, which is what a crashed or wedged runner
    looks like from outside. The runner writes into its own log directory, which the web server
    process can read but does not serve, so the file is not reachable as a static asset.

    A status file that cannot be interpreted is reported the same way as a missing one. This
    endpoint exists to say that something is wrong with the runner, so it must not be the thing that
    raises: a 500 here tells the caller nothing and mails the admins as well.

    Every answer is 200, an outage included. The status code is about this endpoint, and not about
    the task runner it reports on: a request that read the file, measured its age and found the
    runner gone is a request that succeeded. An outage used to answer 503, and that made the one
    state the box exists to show the one state a reader could not be shown. A 5xx body is the body
    any intermediary is free to replace with an error page of its own, and this site is behind a
    reverse proxy. runnerstatus.js then fails to parse the answer, and a request it cannot read
    tells it nothing about the runner, so it draws no sentence and the box collapses -- an outage
    rendered as though nothing were wrong. The healthy path answers 200 and was never affected,
    which is why this survived: the fault could only appear while the runner was down.

    `stale` and `running` in the body carry the state instead. They already did -- the docstring
    above named `stale` as the field to alert on -- so no caller loses anything it was not already
    reading, and a monitor that watched the status code should watch `stale` now.
    """
    try:
        # Shared with the page render, which colours the box from the same read; see status.py.
        status, age_seconds = runnerstatus.read_status()
    except (OSError, KeyError, TypeError, ValueError) as ex:
        # never written, unreadable, half-written despite the atomic rename, or written by a
        # version that recorded something else. A naive `written` lands here as a TypeError.
        # typical_runtime_seconds is present but empty, as it is on the stale branch below: one
        # response shape for both, so a reader can subscript it without checking which failure it got
        return JsonResponse(
            {
                "running": False,
                "stale": True,
                "typical_runtime_seconds": {},
                "user_queued_task_count": 0,
                "detail": f"no usable status file ({type(ex).__name__})",
            },
        )

    stale = age_seconds > runnerstatus.STALE_AFTER_SECONDS

    # The version of this response, from the one field that changes with each write of the file.
    # The medians and the count below can change while `written` does not. But the task runner
    # writes the file every STATUS_WRITE_SECONDS, and thus this version holds for that interval
    # at most. The poll asks each minute.
    etag = f'"{status["written"]}"'

    # A browser that holds the current version asks with If-None-Match, and the answer is 304 with
    # no body. This comes before the medians and the count, because a 304 makes both unnecessary.
    if not stale and request.headers.get("If-None-Match") == etag:
        return HttpResponseNotModified()

    # Only when the task runner runs. A wait estimate is a claim about work in progress. When the
    # status file is stale, no work is in progress, and the queue page discards the figure. A
    # computation at that time would cost a cache read, and at times a query, for each poll of
    # each open tab. Every reader would discard the value.
    typical_runtimes: dict[str, float] = {} if stale else typical_runtime_seconds()

    # How many tasks this reader has in the queue. renderInto shows the queue counts to a reader
    # who waits on the queue. This field is how a page that is not the queue page knows it. The
    # count is for the signed-in user. The response is Cache-Control private, and thus no shared
    # cache can serve one reader the count of another.
    user_queued_task_count = (
        Task.queued().filter(user_id=request.user.pk).count() if request.user.is_authenticated else 0
    )

    response = JsonResponse(
        {
            **status,
            "running": not stale,
            "stale": stale,
            "status_age_seconds": round(age_seconds, 1),
            "typical_runtime_seconds": typical_runtimes,
            "user_queued_task_count": user_queued_task_count,
        },
    )
    # Still no ETag on an outage. The tag is the write time of the file, and while the runner is
    # gone that value stands still: a browser holding it would revalidate into a 304 and keep
    # showing the outage from its cache, which is right only until the runner comes back. An
    # outage response is small, and it is answered again within the minute.
    if not stale:
        response["ETag"] = etag
    return response


@functools.cache
def bokeh_cdn_scripts() -> tuple[dict[str, str], ...]:
    """Return the BokehJS CDN URLs to load on the stats page, each with its integrity hash.

    Cached: the answer depends only on the installed bokeh, but get_sri_hashes_for_version reads
    and parses a JSON file from site-packages on every call, and the stats page is not cached.

    Both come from the installed bokeh package. The template used to hardcode the version in the
    URL, which had to be kept in step with the pin in pyproject.toml by hand: the charts are built
    server-side by that package and rendered by this script, so a mismatch breaks the page. Taking
    the version from bokeh itself means the pin is the only place it is written.

    bokeh ships the hashes for its own release (verified against the files served by the CDN), so
    the integrity attribute costs nothing to keep correct across upgrades -- unlike a hash pasted
    into the template, which an upgrade would silently invalidate.
    """
    from bokeh import __version__ as bokeh_version
    from bokeh.resources import get_sri_hashes_for_version

    hashes = get_sri_hashes_for_version(bokeh_version)

    scripts = []
    for component in ("bokeh", "bokeh-widgets"):
        filename = f"{component}-{bokeh_version}.min.js"
        scripts.append(
            {
                "src": f"https://cdn.pydata.org/bokeh/release/{filename}",
                # a release that bokeh has no hash for would otherwise render integrity="", which
                # browsers treat as no integrity at all rather than as a failure
                "integrity": f"sha384-{hashes[filename]}",
            }
        )

    # a tuple, not the list: functools.cache hands the same object to every request for the life of
    # the worker, so a mutable one is a shared object waiting for someone to append to it
    return tuple(scripts)


def stats(request):
    dictparams = {
        "name": "Usage Statistics",
        "queuedtaskcount": Task.objects.filter(finishtimestamp__isnull=True).count(),
        "bokehscripts": bokeh_cdn_scripts(),
    }

    lastfinishedtask = Task.objects.filter(finishtimestamp__isnull=False).order_by("finishtimestamp").last()
    if lastfinishedtask is not None:
        dictparams["lastfinishtime"] = f"{lastfinishedtask.finishtimestamp:%Y-%m-%d %H:%M:%S %Z}"
    else:
        dictparams["lastfinishtime"] = "N/A"

    return render(request, "stats.html", dictparams)


class SiteOriginPasswordResetView(PasswordResetView):
    """Django's password reset, with the mailed link pinned to the configured origin.

    django.contrib.sites is not installed, so Django builds the link from the Host header, and
    ALLOWED_HOSTS legitimately holds wildcard entries. Anyone who can serve one subdomain of
    qub.ac.uk or fallingstar-data.com could therefore have this send a victim a valid reset link
    that points at their own server, which discloses the token when the victim opens it.

    This is the rule verification.absolute_url already applies to the mail this project sends
    itself. The reset mail is the one Django owns, so it is applied here instead.

    An unset SITE_ORIGIN keeps Django's own behaviour, which is what development and the tests
    want; settings.py makes the variable mandatory in production.
    """

    @override
    def setup(self, request, *args, **kwargs) -> None:
        """Pin the values the reset mail template reads, before the form is handled."""
        super().setup(request, *args, **kwargs)

        if settings.SITE_ORIGIN:
            # one answer for both, from the helper the other mail uses, so the link and the name
            # in the text cannot differ
            host = verification_site_name(request)
            self.extra_email_context = {
                "protocol": urlsplit(settings.SITE_ORIGIN).scheme,
                "domain": host,
                "site_name": host,
            }


class ThrottledLoginView(LoginView):
    """Django's login page, with the failed-login budget from throttles applied.

    The form refuses the password check for an address over the budget and records each failure.
    This view turns that refusal into a 429 with a Retry-After, like every other limit on the
    site, so it is visible to the access log and to anything watching it.
    """

    authentication_form = ThrottledAuthenticationForm

    @override
    def form_invalid(self, form):
        from atlasserver.forcephot.throttles import LOGIN_FAILURE_WINDOW_SECONDS

        refused = any(error.code == "throttled" for error in form.non_field_errors().as_data())
        response = self.render_to_response(self.get_context_data(form=form), status=429 if refused else 200)
        if refused:
            response["Retry-After"] = str(int(LOGIN_FAILURE_WINDOW_SECONDS))

        return response


class ObtainAuthTokenThrottled(ObtainAuthToken):
    """DRF's token endpoint, with the rate limit that its base class removes.

    ObtainAuthToken sets throttle_classes = (), which overrides DEFAULT_THROTTLE_CLASSES, so the
    one endpoint on the site that takes a password was also the only unmetered one. A success
    returns a token that does not expire, so an unbounded guessing loop is worth more here than
    against a session cookie.

    ForcedPhotRateThrottle allows any view that declares no throttle_scope, so naming the scope is
    what turns the limit on.

    No authenticators, because APIView.initial() authenticates before it throttles. With the site
    defaults in place, a wrong password in an Authorization header made BasicAuthentication answer
    401 before the throttle ran, so the counter never moved and one extra header turned the limit
    off. Nothing here reads request.user: ObtainAuthToken takes the credentials from the body
    through its own serializer, and its permission_classes are already empty. The throttle then
    keys on the client address, which is what a login endpoint wants.

    Dropping SessionAuthentication also drops the CSRF check it enforces. That is safe here: the
    view acts only on the credentials in its body, and its JSON reply is unreadable across origins.

    A Basic header on any other view is metered by ThrottledBasicAuthentication (see throttles),
    which counts failed password checks per address and refuses the check itself once over budget.
    """

    # a tuple, like the empty throttle_classes and permission_classes ObtainAuthToken itself
    # declares; DRF only iterates these
    authentication_classes = ()
    throttle_classes = [ForcedPhotRateThrottle]
    throttle_scope = "forcephotlogin"

    @override
    def post(self, request, *args, **kwargs):
        # The same failed-login budget as every other door (see throttles): the rate above bounds
        # how often this endpoint may be asked, and this stops an address that has exhausted its
        # guesses elsewhere from spending more here, or from getting a token for a lucky one.
        if login_failures_exceeded(request):
            raise Throttled(detail=LOGIN_LIMIT_MESSAGE)

        try:
            return super().post(request, *args, **kwargs)
        except serializers.ValidationError:
            # the serializer refuses wrong credentials with a validation error
            note_login_failure(request)
            raise


def register(request):
    if request.method == "POST":
        clientkey = hashlib.sha256(str(client_ip(request)).encode()).hexdigest()

        # Counted before the form is bound, so that a rejected submission spends it too; see
        # REGISTRATION_ATTEMPT_LIMIT for why the answer to a rejected one has to be metered.
        attempts = count_in_window(f"registration-attempt-{clientkey}", REGISTRATION_WINDOW_SECONDS)

        if attempts > REGISTRATION_ATTEMPT_LIMIT:
            # An unbound form, and the message passed beside it rather than through add_error:
            # binding runs clean_email, and its answer is the one this limit exists to meter. The
            # username and the address are put back as initial values instead, so that a caller
            # behind an address they share with a whole institute does not lose what they typed.
            blocked = RegistrationForm(
                initial={"username": request.POST.get("username", ""), "email": request.POST.get("email", "")}
            )
            response = render(
                request,
                "registration/register.html",
                {
                    "form": blocked,
                    "name": "Register",
                    "limitmessage": (
                        "Too many registration attempts from this address. Please wait a few "
                        "minutes before trying again."
                    ),
                },
                # 429 like every other limit on the site, rather than a 200 that reads as an
                # ordinary page render in the access log and to anything watching it
                status=429,
            )
            # the window is fixed, so this is the longest the caller can be asked to wait
            response["Retry-After"] = str(int(REGISTRATION_WINDOW_SECONDS))

            return response

        form = RegistrationForm(request.POST)
        if form.is_valid():
            # Bounded per client address: every valid submission sends mail synchronously, so a
            # loop of distinct addresses is an open relay as far as the SMTP quota is concerned,
            # and plus-addressing aims them all at one inbox without tripping the unique index.
            # Tighter than the attempt budget above, and separate from it, because this one bounds
            # the mail rather than the probes.
            registrations = count_in_window(f"registration-{clientkey}", REGISTRATION_WINDOW_SECONDS)

            if registrations > REGISTRATION_WINDOW_LIMIT:
                form.add_error(
                    None,
                    "Several accounts have just been registered from this address. Please wait a "
                    "few minutes before registering another.",
                )
                return render(request, "registration/register.html", {"form": form, "name": "Register"})

            # Account and send in one transaction: a row created without its link is worse than
            # none, because it holds the username and address while being unable to log in, verify
            # (nothing was sent) or recover (password reset skips inactive users).
            try:
                with transaction.atomic():
                    user = form.save(commit=False)
                    user.is_active = False
                    user.save()

                    # in the same transaction as the account: the marker is what later tells this
                    # inactive row apart from one an administrator switched off, so an account
                    # without it would be unable to verify and unable to be recovered
                    PendingEmailVerification.objects.create(user=user)

                    send_verification_email(request, user)
            except IntegrityError:
                # Separate from the send failure below, whose "try again shortly" is a dead end
                # here: username and address are both checked before this save and either can be
                # taken in between. Which one is asked rather than read off the exception, whose
                # text varies by backend -- naming the wrong field is what this exists to stop.
                username = form.cleaned_data.get("username")
                email = form.cleaned_data.get("email")

                if username and get_user_model().objects.filter(username=username).exists():
                    logger.info("registration lost the race for a username")
                    form.add_error("username", "An account with this username already exists.")
                elif email and email_is_taken(email):
                    logger.info("registration lost the race for an email address")
                    form.add_error("email", "An account with this email address already exists.")
                else:
                    # neither is taken now, so the collision was with a row that has since gone,
                    # or with something this does not model. Retrying is genuinely the right advice
                    logger.exception("registration failed on an integrity error it could not attribute")
                    form.add_error(None, "We could not create your account just now. Please try again.")
            except Exception:
                logger.exception("Could not send the verification email for a new registration")
                form.add_error(
                    None,
                    "We could not send the verification email just now, so your account has not "
                    "been created. Please try again in a few minutes.",
                )
            else:
                return render(
                    request,
                    "registration/verification_sent.html",
                    {"name": "Check your email", "email": user.email},
                )
    else:
        form = RegistrationForm()

    # "name" is what the base template builds the page <title> from, the same way the pages
    # declared with extra_context in urls.py do
    return render(request, "registration/register.html", {"form": form, "name": "Register"})


def awaiting_verification(user) -> bool:
    """Whether this inactive account is one that registered and has not confirmed its address.

    Asked of the PendingEmailVerification row rather than inferred. is_active alone cannot answer
    it -- unchecking that is equally how an administrator disables an account -- and the previous
    answer, "has never logged in", is wrong for an account disabled before its first login, or one
    that only ever used an API token.
    """
    return not user.is_active and PendingEmailVerification.objects.filter(user=user).exists()


def unverified_account_for(email: str):
    """Return the account awaiting verification at this address, or None."""
    return (
        # the queryset spelling of awaiting_verification()
        get_user_model()
        .objects.filter(email__iexact=email, is_active=False, pending_verification__isnull=False)
        .order_by("pk")
        .first()
    )


@origin_only_referrer
def verify_email(request, uidb64: str, token: str):
    """Activate an account from the link in its verification email, once its owner confirms.

    The link is not self-acting. Mail security gateways, link scanners and some clients fetch every
    URL in an incoming message, so anything a GET here performs is performed by that machinery
    rather than by the recipient -- and this endpoint decides whether someone owns an address and
    then logs the caller in. Registering with a victim's address and letting their own gateway
    follow the link would otherwise hand the attacker a verified, active account.
    """
    user = user_from_uidb64(uidb64)

    # the same test the resend path applies, repeated here so that the rule guards the flag itself
    # rather than only the one door that hands out links
    if user is None or not awaiting_verification(user) or not verification_token_generator.check_token(user, token):
        # deliberately the same page for an unknown id, a bad token and an expired one: which of
        # those it was is not something the person following the link can act on differently, and
        # distinguishing them tells an attacker which user ids exist
        return render(
            request,
            "registration/verification_invalid.html",
            {"name": "Verification link is not valid"},
            status=400,
        )

    if request.method != "POST":
        return render(
            request,
            "registration/verify_email_confirm.html",
            {"name": "Confirm your email address", "email": user.email},
        )

    # The checks above are repeated here against a locked row, because on their own they only make
    # the link single-use in sequence. What retires the token is the write -- the hash covers
    # is_active -- so two POSTs carrying the same link can both pass the checks before either
    # activates, and both reach login(). That is the case where someone else has a copy of the
    # link: a burst of parallel requests would keep them a session that the first use was supposed
    # to have closed off.
    with transaction.atomic():
        # t.Any for the reason verification.py spells out: the type checkers disagree about whether
        # get_user_model() yields the concrete User or AbstractBaseUser, and only one of them
        # believes it has is_active and email
        locked: t.Any = get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        consumed = (
            locked is not None
            and awaiting_verification(locked)
            and verification_token_generator.check_token(locked, token)
        )

        if consumed:
            locked.is_active = True
            locked.save(update_fields=["is_active"])

            # the address is proved, so the account is an ordinary one from here on
            PendingEmailVerification.objects.filter(user=locked).delete()

    if not consumed:
        return render(
            request,
            "registration/verification_invalid.html",
            {"name": "Verification link is not valid"},
            status=400,
        )

    # the token hashes is_active, so following the link a second time lands on the invalid page
    # rather than here. Log in anyway on the first pass: the user has just proved they hold the
    # address, and making them type the password again immediately serves nothing.
    login(request, locked, backend="django.contrib.auth.backends.ModelBackend")

    return redirect(reverse("task-list"))


def resend_verification(request):
    """Send a fresh verification link to an address that has an unverified account.

    Without this, an account whose link expired is stranded: it cannot log in, and it cannot use
    password reset either, because PasswordResetForm only considers active users. The address is
    also taken as far as registration is concerned, so the person cannot simply sign up again.
    """
    sent = False
    form = ResendVerificationForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]

        # One send per address per interval; without it this endpoint floods any inbox with an
        # unverified account. The address is hashed because cache keys appear in filenames, and the
        # throttle cache is used because an unauthenticated caller can create one key per address
        # it posts -- "default" holds the queue signal and PDF locks behind a 300-entry cap.
        emailkey = hashlib.sha256(email.lower().encode()).hexdigest()
        may_send = caches["throttle"].add(
            f"resendverification-{emailkey}", True, timeout=RESEND_VERIFICATION_INTERVAL_SECONDS
        )

        if may_send:
            user = unverified_account_for(email)
            if user is not None:
                # guarded like the send in register(), and for the same reason: an unreachable mail
                # relay is a temporary condition, and letting it out of a plain Django view answers
                # with a 500 rather than the page below. Nothing has been written to undo.
                try:
                    send_verification_email(request, user)
                except Exception:
                    logger.exception("Could not resend a verification email")

        # reported as sent either way, whether or not an account exists and whether or not the
        # rate limit allowed this one: neither is something a stranger should be able to probe for
        sent = True

    return render(
        request,
        "registration/resend_verification.html",
        {"form": form if not sent else ResendVerificationForm(), "sent": sent, "name": "Resend verification email"},
    )


@login_required
def change_email(request):
    if request.method == "POST":
        form = EmailChangeForm(request.user, request.POST)
        if form.is_valid():
            # not saved here. The new address has to be confirmed at the new address, or the
            # verification done at registration could be undone just by changing the address
            # afterwards.
            new_email = form.cleaned_data["new_email"]

            # Rate limited like the resend path, and for the same reason. Nothing is stored until
            # the link is followed, so there is no pending-change record to make a second attempt a
            # no-op: without this, one logged-in account can post this form in a loop and have the
            # server mail an arbitrary address as fast as it will go. Keyed on the account rather
            # than the target, so changing the address each time does not buy another send.
            limitkey = f"emailchange-{request.user.pk}"
            may_send = caches["throttle"].add(limitkey, True, timeout=EMAIL_CHANGE_INTERVAL_SECONDS)

            if may_send:
                # guarded like register()'s send: an unreachable relay would otherwise leave a
                # plain Django view raising, and answer a routine form post with a 500
                try:
                    send_email_change_confirmation(request, request.user, new_email)
                except Exception:
                    logger.exception("Could not send an email change confirmation")
                    # and release the slot, or the retry this advises would be met by the
                    # "just sent" message below for the next minute -- telling the user their mail
                    # is on its way when the send is exactly what failed
                    caches["throttle"].delete(limitkey)
                    form.add_error(
                        None,
                        "We could not send the confirmation email just now. Please try again in a "
                        "few minutes. Your address has not been changed.",
                    )
                else:
                    return render(
                        request,
                        "registration/verification_sent.html",
                        {"name": "Check your email", "email": new_email, "emailchange": True},
                    )
            else:
                # said plainly: this is the user's own account and their own action, so unlike the
                # resend page there is nothing to conceal by pretending it was sent
                form.add_error(
                    None,
                    "A confirmation email was just sent. Please wait a minute before requesting another.",
                )
    else:
        form = EmailChangeForm(request.user)

    return render(
        request,
        "registration/email_change_form.html",
        {"form": form, "name": "Email address change"},
    )


@origin_only_referrer
def confirm_email_change(request, token: str):
    """Apply an email change once the link sent to the new address has been confirmed.

    Posted, not applied on GET, for the reason spelled out in verify_email: a scanner fetching the
    link out of the recipient's mailbox would otherwise make the change before they had read it.
    """
    loaded = load_email_change_token(token, max_age=settings.PASSWORD_RESET_TIMEOUT)

    if loaded is not None:
        user, new_email = loaded

        if request.method != "POST":
            return render(
                request,
                "registration/email_change_confirm.html",
                {"name": "Confirm your new email address", "email": new_email},
            )

        # re-checked at use, not only when the link was sent: somebody else may have taken the
        # address in between, and the database index would turn that into a 500 rather than a
        # message the user can act on.
        #
        # The check cannot close the window on its own, though -- two people confirming the same
        # free address at once both pass it, and the loser's save() raises. Catching that turns the
        # race into the same answer the check gives, instead of a 500.
        if not email_is_taken(new_email, exclude_user=user):
            changed = False
            try:
                with transaction.atomic():
                    # The token is checked a second time here, from inside the transaction and
                    # with the row locked. load_email_change_token() above read the credentials the
                    # token is bound to before this began, so a password reset or an administrative
                    # deactivation landing in between would be overtaken by this write -- and the
                    # address it hands over is where the next password reset would be sent.
                    # Re-running the same loader (it re-reads the user) rather than repeating its
                    # rules keeps one statement of what makes a link valid.
                    locked: t.Any = get_user_model().objects.select_for_update().filter(pk=user.pk).first()
                    revalidated = load_email_change_token(token, max_age=settings.PASSWORD_RESET_TIMEOUT)

                    if (
                        locked is not None
                        and revalidated is not None
                        and revalidated[0].pk == locked.pk
                        and revalidated[1] == new_email
                    ):
                        locked.email = new_email
                        locked.save(update_fields=["email"])
                        changed = True
            except IntegrityError:
                logger.info("email change to an address claimed concurrently was refused")
            else:
                if changed:
                    return render(
                        request,
                        "registration/email_change_done.html",
                        {"name": "Email address changed", "email": new_email},
                    )

    # emailchange picks the copy for this flow: the shared page otherwise offers a verification
    # resend, which does nothing for an account that is already active
    return render(
        request,
        "registration/verification_invalid.html",
        {"name": "Confirmation link is not valid", "emailchange": True},
        status=400,
    )


# never_cache, because the response body carries the token in the clear -- it is the one page here
# that does. Without it a shared browser keeps a history snapshot that Back reaches after logout,
# with no login_required in the way.
@never_cache
@login_required
def api_token(request):
    """Let a user see, create, replace and delete their own API token.

    Tokens never expire, and until this page existed there was no way to rotate one: a user whose
    token had leaked (into a shell history, a notebook, a pasted traceback) could only ask an
    administrator to intervene. /api-token-auth/ hands one out but will not replace one.

    A plain Django view rather than a DRF endpoint. It is reached from a browser with a session, it
    is a destructive action guarded by CSRF, and routing it through DRF would mean a token-holder
    could authenticate with the very token they are replacing.
    """
    token = Token.objects.filter(user=request.user).first()
    justcreated = False

    if request.method == "POST":
        action = request.POST.get("action")

        if action in {"create", "regenerate"}:
            # The key is the primary key, so rotating means delete-then-create; one transaction so
            # a failure between them cannot leave the user with none. Locked because Token.user is
            # one-to-one: two rotations arriving together would both delete nothing, both insert,
            # and one would raise.
            with transaction.atomic():
                get_user_model().objects.select_for_update().filter(pk=request.user.pk).first()
                Token.objects.filter(user=request.user).delete()
                token = Token.objects.create(user=request.user)
            justcreated = True

        elif action == "delete":
            Token.objects.filter(user=request.user).delete()
            # post/redirect/get, so a reload does not repeat a destructive action. The create and
            # regenerate paths above deliberately fall through and render instead: the key is only
            # recoverable from this response, so a redirect would show a page that cannot display
            # what the user just asked for.
            return redirect(reverse("apitoken"))

        else:
            return HttpResponseBadRequest("Unknown action")

    return render(
        request,
        "apitoken.html",
        {"name": "API Token", "token": token, "justcreated": justcreated},
    )


_plotscript_cache: dict[Path, tuple[float, int, str]] = {}

# The result file columns that the light curve plot is built from. "m" and "dm" are the pipeline's
# own magnitude and its error: the plot shows those rather than converting the flux itself, so that
# it agrees with the file the visitor downloads. "mag5sig" is the 5 sigma depth of the image, which
# is what a point too faint to measure is drawn at. "chi/N" is read by the quality cut below.
#
# "F" is the filter band, and it is named here because df.query() below resolves it by name: a file
# without it got past this guard and then raised pandas' UndefinedVariableError, which subclasses
# NameError rather than ValueError, so it escaped the handler this guard was written to work with
# and answered 500 (and mailed the admins) instead of drawing an empty plot.
PLOTCOLUMNS = frozenset({"#MJD", "uJy", "duJy", "m", "dm", "mag5sig", "chi/N", "F"})


# the arguments are named for the result file columns they carry, which is why they are not all
# lowercase
def plotpoint(mjd: float, uJy: float, duJy: float, m: float, dm: float, mag5sig: float) -> str:  # noqa: N803
    """Return one light curve point, as the JavaScript literal the plotting script reads.

    A point is an upper limit when the image was not deep enough to measure it: either the flux is
    zero or below, or the magnitude is fainter than the 5 sigma depth of that image. Such a point is
    drawn at the depth, with no error, which is what tells the plotting script to draw an arrow.

    The flux and its error are kept whatever the point is. A flux is a measurement at any depth, and
    the flux plot shows it; only the magnitude stops meaning anything.

    An image with no recorded depth leaves the point as it was measured, because there is nothing to
    draw a limit at.

    Every magnitude is tested with math.isfinite, because "nan" and "inf" are not JavaScript
    literals: one of them in this string stops the whole plotting script with a ReferenceError. A
    point with no usable magnitude is sent with none, and the magnitude plot leaves it out.
    """
    if math.isfinite(mag5sig) and (uJy <= 0 or m > mag5sig):
        return f"[{mjd},{uJy},{duJy},{mag5sig},null]"
    if not (math.isfinite(m) and math.isfinite(dm)):
        return f"[{mjd},{uJy},{duJy},null,null]"
    return f"[{mjd},{uJy},{duJy},{m},{dm}]"


def read_plotscript(path: Path) -> str:
    """Return the contents of the light curve plotting script, re-reading it only when it changes.

    This is appended to every result plot response and deliberately kept outside the generated-data
    cache, so that a redeployed script takes effect immediately instead of after the cache timeout.
    Keying on the file's mtime and size preserves that while turning a disk read per request into a
    stat per request.
    """
    # explicit encoding, because read_text() would otherwise decode with the locale encoding, and
    # under mod_wsgi that is often the C locale's ASCII. The script carries non-ASCII characters
    # (the "Flux / µJy" axis label) now that babel emits them literally instead of as \xNN escapes,
    # so a locale-dependent read is the difference between a working plot and a 500 on every one.
    return cached_by_file_stat(_plotscript_cache, path, lambda scriptpath: scriptpath.read_text(encoding="utf-8"))


def resultplotdatajs(request, taskid):
    if not taskid:
        return HttpResponseNotFound("Page not found")

    try:
        task = Task.live().get(id=taskid)
    except ObjectDoesNotExist:
        return HttpResponseNotFound("Page not found")
    if not task.finishtimestamp:
        return HttpResponseNotFound("Page not found")

    # Two things vary in this response and the tag has to cover both: the generated plot data,
    # which changes with the task, and the plotting script appended below, which changes when the
    # built asset is redeployed. STATIC_VERSION is derived from the mtimes of those built assets,
    # lightcurveplotly.min.js among them.
    #
    # It used to be the UTC date alone. That is the same before and after a deploy, so a browser
    # that had loaded a plot earlier the same day was told 304 and went on running the previous
    # script -- which, across the jQuery removal, meant a page of blank plots and "Can't find
    # variable: $" until midnight. Quoted, because a bare token is not a valid entity-tag.
    etag = None if settings.DEBUG else entity_tag(settings.STATIC_VERSION, task.task_modified_datetime)

    if "HTTP_IF_NONE_MATCH" in request.META and etag == request.META["HTTP_IF_NONE_MATCH"]:
        return HttpResponseNotModified()

    strjs = caches["taskderived"].get(resultplotdatajs_cachekey(taskid), default=None)

    if strjs is None:
        jsout = []

        resultfilepath = None
        localresultfile = task.localresultfile()
        if localresultfile is not None:
            resultfilepath = Path(settings.STATIC_ROOT, localresultfile)
            if not resultfilepath.is_file():
                resultfilepath = None

        dfforcedphot = None
        if resultfilepath is not None:
            try:
                dfforcedphot = pd.read_csv(
                    resultfilepath,
                    sep=r"\s+",
                    escapechar="#",
                    # One dtype mapping rather than dtype=float plus converters for these four:
                    # pandas warns for every column named in both -- four ParserWarnings per plot
                    # request -- and then applies the converter and ignores the dtype anyway.
                    #
                    # A defaultdict, because the factory is what still types the columns that are
                    # not named. With a plain dict they would be inferred instead, and a column of
                    # whole numbers would come back as int64, a column with a stray non-numeric
                    # token as object -- where dtype=float raises the ValueError caught below.
                    dtype=defaultdict(lambda: float, {"F": str, "Obs": str, "uJy": int, "duJy": int}),
                )
            except ValueError:
                # an empty, truncated or ragged result file: EmptyDataError and ParserError are
                # both ValueError subclasses, and a non-numeric token in a well-formed row (e.g.
                # a diagnostic line mixed into the output) fails the dtype=float conversion with
                # a plain ValueError
                dfforcedphot = None
            else:
                # A file that is well formed but has no column this view reads is treated the same
                # way as one that does not parse: an empty plot rather than a server error. This
                # comes first, because every line below reads one of those columns.
                if not PLOTCOLUMNS.issubset(dfforcedphot.columns):
                    dfforcedphot = None
                else:
                    ujy_min = int(-1e10)
                    ujy_max = int(1e10)
                    dfforcedphot = dfforcedphot[(dfforcedphot["uJy"] > ujy_min) & (dfforcedphot["uJy"] < ujy_max)]

                    # The cut that plot_atlas_fp.py makes before it draws the PDF plot, applied here
                    # so that the interactive plot shows the same points. A very large flux error or
                    # a poor profile fit means the measurement is not usable.
                    #
                    # The PDF plot also sigma clips with a rolling window. That is not done here: it
                    # removes points that the result file does contain, and this plot is read beside
                    # that file.
                    dfforcedphot = dfforcedphot[(dfforcedphot["duJy"] <= 4000) & (dfforcedphot["chi/N"] <= 100)]

        # with no rows, the limits below would be NaN, which is not a JavaScript literal
        if dfforcedphot is not None and not dfforcedphot.empty:
            jsout.append('"use strict";\n')
            jsout.extend(
                (
                    "var jslcdata = new Array();\n",
                    "var jslabels = new Array();\n",
                )
            )
            divid = f"plotforcedflux-task-{taskid}"

            for color, filterband in [(11, "c"), (12, "o"), (8, "w")]:
                # @filterband is resolved by pandas from this scope, so the names must agree
                dffilter = dfforcedphot.query("F == @filterband", inplace=False)

                jsout.extend(
                    (
                        '\njslabels.push({"color": '
                        + str(color)
                        + ', "display": false, "label": "'
                        + filterband
                        + '"});\n',
                        "jslcdata.push(["
                        + ", ".join(
                            [
                                plotpoint(mjd, uJy, duJy, m, dm, mag5sig)
                                for _, (mjd, uJy, duJy, m, dm, mag5sig) in dffilter[
                                    ["#MJD", "uJy", "duJy", "m", "dm", "mag5sig"]
                                ].iterrows()
                            ]
                        )
                        + "]);\n",
                    )
                )
            mjd_today = datetime_to_mjd(datetime.datetime.now(datetime.UTC))
            xmin = dfforcedphot["#MJD"].min()
            xmax = dfforcedphot["#MJD"].max()
            ymin = max(-200, dfforcedphot.uJy.min())
            ymax = min(40000, dfforcedphot.uJy.max())

            jsout.extend(
                (
                    "var jslclimits = {",
                    f'"xmin": {xmin}, "xmax": {xmax}, "ymin": {ymin}, "ymax": {ymax},',
                    f'"discoveryDate": {xmin},',
                    f'"today": {mjd_today},',
                    "};\n",
                    f'jslimitsglobal["#{divid}"] = jslclimits;\n',
                    f'jslcdataglobal["#{divid}"] = jslcdata;\n',
                    f'jslabelsglobal["#{divid}"] = jslabels;\n',
                    f'var lcdivname = "#{divid}";\n',
                )
            )
        strjs = "".join(jsout)

        # an empty result (e.g. a transiently unreadable file) must not be stored, or the plot
        # would stay blank until the entry expires even after the file is fixed
        if strjs:
            caches["taskderived"].set(resultplotdatajs_cachekey(taskid), strjs)

    if strjs:
        # the plotting code is appended outside of the cache, so that a redeployed
        # lightcurveplotly script takes effect immediately rather than after the cache timeout
        plotscript = "js/queuepage/src/lightcurveplotly.js" if settings.DEBUG else "js/lightcurveplotly.min.js"
        strjs += read_plotscript(Path(settings.STATIC_ROOT, plotscript))

    # only set the header when there is one: Django stringifies whatever it is given, so passing the
    # DEBUG-mode None sent the literal header "ETag: None"
    headers: dict[str, str] = {"ETag": etag} if etag is not None else {}

    return HttpResponse(strjs, content_type="text/javascript", headers=headers)


@contextlib.contextmanager
def pdfplot_render_slot() -> Iterator[bool]:
    """Hold one of the site's PDF render slots for the body, or yield False if none is free."""
    cache = caches["default"]

    for slot in range(PDF_PLOT_RENDER_SLOTS):
        # the timeout outlives the render, so a worker killed mid-render cannot hold a slot for
        # good; the same reasoning as the per-task lock below
        slotkey = f"pdfplot-slot-{slot}"
        if cache.add(slotkey, True, timeout=PDF_PLOT_TIMEOUT_SECONDS + 30):
            try:
                yield True
            finally:
                cache.delete(slotkey)
            return

    yield False


def _pdfplot_unavailable() -> HttpResponse:
    """Tell the client the plot is not ready yet, rather than that it does not exist.

    A 404 here would be wrong twice over: the task and its data do exist, and a browser (or the
    queue page) has no reason to retry a 404, whereas this is very likely to succeed shortly.
    """
    return HttpResponse(
        "The PDF plot for this task is still being generated. Please try again shortly.",
        status=503,
        headers={"Retry-After": "30"},
    )


def taskpdfplot(request, taskid):
    if not taskid:
        return HttpResponseNotFound("Page not found")

    try:
        item = Task.live().get(id=taskid)
    except ObjectDoesNotExist:
        return HttpResponseNotFound("Page not found")
    if resultfile := item.localresultfile():
        resultfilepath = Path(settings.STATIC_ROOT, resultfile)
        pdfpath = resultfilepath.with_suffix(".pdf")

        # deleting a task's .pdf forces it to be re-rendered on the next request, which is how to
        # refresh plots after a change to plot_atlas_fp
        if not pdfpath.is_file():
            # Herd control: the queue page links a PDF for every finished task, so a crawler or a
            # reload would otherwise fork matplotlib once per concurrent request. Not a mutex (see
            # the note by the limiter constants); losing the race costs one extra render, not the
            # tenth. The timeout outlives the render so a killed worker cannot wedge the task.
            lockkey = f"pdfplot-lock-{taskid}"
            if not caches["default"].add(lockkey, True, timeout=PDF_PLOT_TIMEOUT_SECONDS + 30):
                return _pdfplot_unavailable()

            try:
                with pdfplot_render_slot() as hasslot:
                    # the lock above is keyed on the task, so it says nothing about requests for
                    # other tasks. See PDF_PLOT_RENDER_SLOTS for why the total is bounded as well.
                    if not hasslot:
                        return _pdfplot_unavailable()

                    # A private path published by rename, so a killed child leaves its wreckage
                    # there rather than at the final name, and two concurrent renders cannot damage
                    # each other. The uniqueness goes in the stem so .pdf stays the suffix:
                    # plot_atlas_fp calls savefig() with no explicit format, and matplotlib infers
                    # it from the extension.
                    renderpath = pdfpath.with_name(
                        f"{pdfpath.stem}.{os.getpid()}.{uuid.uuid4().hex}.partial{pdfpath.suffix}"
                    )

                    try:
                        completed = make_pdf_plot(
                            taskid=taskid,
                            localresultfile=resultfilepath,
                            taskcomment=item.comment,
                            separate_process=True,
                            outputpath=renderpath,
                        )

                        if completed and renderpath.is_file():
                            renderpath.replace(pdfpath)
                    finally:
                        renderpath.unlink(missing_ok=True)

                    if not completed:
                        logger.warning("PDF plot generation for task %d exceeded its time limit and was killed", taskid)
                        return _pdfplot_unavailable()
            finally:
                caches["default"].delete(lockkey)

        if pdfpath.is_file():
            return FileResponse(pdfpath.open("rb"))

    return HttpResponseNotFound("ERROR: Could not generate PDF plot (perhaps a lack of data points?)")


# Results are public on purpose, like the task detail itself — the reasoning lives on
# ForcePhotPermission.has_object_permission. The file views below (and taskpdfplot and
# resultplotdatajs above) therefore take a task id and no credentials; note the serializer also
# links results at their STATIC_URL path, which Apache serves without consulting Django at all.
#
# Public is not the same as archived, so they all read Task.live(), like retrieve. Without that
# filter, these views serve a deleted task's data file, and taskpdfplot and resultplotdatajs
# rebuild the .pdf and the cached plot that the delete reclaimed. The files themselves go with the
# row (see Task.delete_result_files), so a static link published before the delete stops resolving
# at the same moment.
#
# no @cache_page on the result file views: Django's cache middleware skips streaming responses, so
# decorating a view that returns a FileResponse only adds a lookup that can never hit
def _taskresultfile_response(
    taskid: int, getfile: Callable[[Task], str | Path | None]
) -> FileResponse | HttpResponseNotFound:
    """Serve one of a task's result files, or 404 when the task or the file does not exist."""
    task = Task.live().filter(id=taskid).first() if taskid else None
    resultfile = getfile(task) if task is not None else None
    if resultfile:
        resultfilepath = Path(settings.STATIC_ROOT, resultfile)

        if resultfilepath.is_file():
            return FileResponse(resultfilepath.open("rb"))

    return HttpResponseNotFound("Page not found")


def taskresultdata(request, taskid: int) -> FileResponse | HttpResponseNotFound:
    return _taskresultfile_response(taskid, lambda task: task.localresultfile())


def taskpreviewimage(request, taskid: int) -> FileResponse | HttpResponseNotFound:
    return _taskresultfile_response(taskid, lambda task: task.localresultpreviewimagefile)


def taskimagezip(request, taskid: int) -> FileResponse | HttpResponseNotFound:
    return _taskresultfile_response(taskid, lambda task: task.localresultimagezipfile)
