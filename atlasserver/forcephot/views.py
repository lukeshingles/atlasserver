"""Django views for the forcephot app."""

import datetime
import ipaddress
import json
import logging
import math
import statistics
import typing as t
from collections.abc import Callable
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.gis.geoip2 import GeoIP2
from django.contrib.gis.geoip2 import GeoIP2Exception
from django.core.cache import caches
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied

# from django.contrib.auth.models import User
from django.db.models import Count
from django.db.models import Max
from django.db.models.functions import Trunc
from django.http import FileResponse
from django.http import HttpResponse
from django.http import HttpResponseNotFound
from django.http import HttpResponseNotModified
from django.http import JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.cache import cache_page
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
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.utils.urls import replace_query_param
from rest_framework.views import APIView

from atlasserver.forcephot.filters import TaskFilter
from atlasserver.forcephot.forms import EmailChangeForm
from atlasserver.forcephot.forms import RegistrationForm
from atlasserver.forcephot.misc import country_code_to_name
from atlasserver.forcephot.misc import country_region_to_name
from atlasserver.forcephot.misc import datetime_to_mjd
from atlasserver.forcephot.misc import make_pdf_plot
from atlasserver.forcephot.misc import PDF_PLOT_TIMEOUT_SECONDS
from atlasserver.forcephot.misc import resultplotdatajs_cachekey
from atlasserver.forcephot.misc import splitradeclist
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.netaddr import address_is_public
from atlasserver.forcephot.netaddr import client_address
from atlasserver.forcephot.pagination import TaskPagination
from atlasserver.forcephot.queue import next_queuepos_relative
from atlasserver.forcephot.queue import request_recalc as request_queue_recalc
from atlasserver.forcephot.serializers import ForcePhotTaskSerializer

# not atlasserver.taskrunner.main: importing that module runs django.setup() and pulls pandas and
# multiprocessing into every web worker, permanently, to read two constants
from atlasserver.taskrunner import status as runnerstatus

MAX_USER_IMGZIP_TASKS = 5
MAX_USER_TASKS = 500

logger = logging.getLogger(__name__)


def get_tasklist_etag(request, user_id: int) -> str:
    """Return an etag that changes whenever anything the given user's task pages show changes.

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

    return (
        f'"{request.accepted_renderer.format}.user{user_id}.path{request.get_full_path()}'
        f".count{usertasks['id__count']}.lastqueue{usertasks['timestamp__max']}"
        f".laststart{usertasks['starttimestamp__max']}.lastfinish{usertasks['finishtimestamp__max']}"
        f".lastmod{usertasks['task_modified_datetime__max']}"
        # the queue position rendered for a task is relative to the front of the global queue
        f'.queueoffset{Task.min_queuepos_relative()}"'
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


def client_ip(request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address the request came from, or None if it cannot be established.

    The policy (and the reasoning behind it) lives in netaddr.client_address; this only supplies
    the request pieces. NUM_PROXIES in REST_FRAMEWORK is set from the same TRUSTED_PROXY_COUNT so
    that the throttle's idea of the client cannot drift from this one.
    """
    return client_address(
        remote_addr=request.META.get("REMOTE_ADDR"),
        forwarded_for=request.META.get("HTTP_X_FORWARDED_FOR"),
        trusted_proxy_count=settings.TRUSTED_PROXY_COUNT,
    )


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

    def has_permission(self, request, view) -> bool:
        return bool(request.method in permissions.SAFE_METHODS or request.user.is_authenticated)

    def has_object_permission(self, request, view, obj) -> bool:
        # Reading a task is public, including to anonymous callers, and is meant to be: a task and
        # the results it produced describe measurements from a public survey, so a link to one can
        # be shared with somebody who has no account here. Only changing it is restricted.
        # (The task *list* is a different question and stays scoped to the requesting user: it is
        # a person's own queue, not a public index.)
        if request.method in permissions.SAFE_METHODS:
            return True

        if not request.user or not request.user.is_authenticated:
            return False

        # staff and instance owner have all permissions
        return request.user.is_staff or obj.user_id == request.user.id


class ForcePhotTaskViewSet(viewsets.ModelViewSet):
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
    # permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    permission_classes = [ForcePhotPermission]
    throttle_scope = "forcephottasks"
    ordering_fields = ["timestamp", "id"]
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    filterset_class = TaskFilter
    ordering = "-id"
    # filterset_fields = ['finishtimestamp']
    template_name = "tasklist-react.html"

    def create(self, request, *args, **kwargs) -> Response:
        """Create new tasks if the user is authenticated and the request is valid."""
        if not request.user.is_authenticated or self.request.user.pk is None:
            raise PermissionDenied

        usertaskcount = Task.objects.filter(
            starttimestamp__isnull=True,
            user_id=self.request.user.pk,
            is_archived=False,
        ).count()

        # a radeclist can hold up to 100 targets, so the limit must account for the size of this
        # request rather than only the tasks that are already queued
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

    def perform_create(self, serializer) -> None:
        """Create new task(s)."""
        usertaskcount_before = (
            Task.objects.filter(
                starttimestamp__isnull=True,
                user_id=self.request.user.pk,
                is_archived=False,
            ).count()
            if self.request.user and self.request.user.pk is not None
            else 0
        )

        extra_fields: dict[str, Any] = {
            "user": self.request.user,
            "timestamp": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        }

        extra_fields.update(client_location_fields(self.request))
        extra_fields["from_api"] = request_is_from_api(self.request)

        serializer.save(**extra_fields)

        # take the ids from the saved objects rather than serializer.data: accessing .data here
        # would cache the response body before the updates below have been applied
        newtasks = serializer.instance if isinstance(serializer.instance, list) else [serializer.instance]

        # Provisional positions at the back of the queue. The real ordering is round-robin across
        # users and is assigned by the task runner; this only stops a task submitted between two
        # renumberings from showing no position at all, and costs one aggregate for the whole batch.
        nextqueuepos = next_queuepos_relative()

        # one statement rather than an UPDATE per task: a radeclist can hold 100 targets, and that
        # was 100 round trips inside the request that the user is waiting on
        Task.objects.bulk_update(
            [
                Task(
                    id=task.id,
                    userqueuedtasks_on_submit=usertaskcount_before + i,
                    queuepos_relative=nextqueuepos + i,
                )
                for i, task in enumerate(newtasks)
            ],
            ["userqueuedtasks_on_submit", "queuepos_relative"],
        )

        request_queue_recalc()

        # the updates above went straight to the database, so reload the in-memory objects that
        # will be serialised into the response
        for task in newtasks:
            task.refresh_from_db()

    def perform_update(self, serializer) -> None:
        """Update a task."""
        # don't pass user=request.user here: a staff user editing someone else's task would
        # otherwise take ownership of it
        serializer.save()

    def perform_destroy(self, instance) -> None:
        """Delete a task, and if the task is queued (not finished), then update queue positions."""
        update_queue_positions = not instance.finishtimestamp
        instance.delete()
        if update_queue_positions:
            request_queue_recalc()

    def list(self, request, *args, **kwargs):
        """List tasks belonging to the current user."""
        if request.user.is_authenticated:
            listqueryset = self.filter_queryset(self.get_queryset().filter(is_archived=False, user_id=request.user))
        else:
            listqueryset = Task.objects.none()
            raise PermissionDenied

        if request.accepted_renderer.format == "html":
            return Response(
                template_name=self.template_name,
                data={
                    # 'serializer': serializer, 'data': serializer.data, 'tasks': page,
                    "name": "Task Queue",
                    "singletaskdetail": False,
                    "paginator": self.paginator,
                    "usertaskcount": listqueryset.count(),
                    "debug": settings.DEBUG,
                },
            )

        # answer a conditional request before paginating: an unchanged page then costs one
        # aggregate instead of a count, a page fetch, a prefetch and a full serialisation.
        # request.user.pk is not None here: the anonymous case raised PermissionDenied above
        etag = get_tasklist_etag(request, request.user.pk)
        if etag == request.META.get("HTTP_IF_NONE_MATCH"):
            return HttpResponseNotModified()

        page = self.paginate_queryset(listqueryset)

        if page is not None:
            # if request.GET.get('cursor') and page[0].id == listqueryset[0].id:
            #     return redirect(remove_query_param(request.get_full_path(), 'cursor'))
            serializer = self.get_serializer(page, many=True)
            # return self.get_paginated_response(serializer.data)
            paginator = self.paginator
            if not isinstance(paginator, TaskPagination):
                msg = f"expected TaskPagination, got {type(paginator).__name__}"
                raise TypeError(msg)
            return paginator.get_paginated_response(serializer.data, headers={"ETag": etag})

        return Response(self.get_serializer(listqueryset, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_archived:
            return HttpResponseNotFound("Page not found")

        if request.accepted_renderer.format == "html":
            # return redirect('/')
            # queryset = self.filter_queryset(self.get_queryset())
            # serializer = self.get_serializer(queryset, many=True)

            # tasks = [instance]
            # form = TaskForm()
            return Response(
                template_name=self.template_name,
                data={
                    # 'serializer': serializer, 'data': serializer.data, 'tasks': tasks, 'form': form,
                    # self.get_object() again here would repeat the lookup query
                    "name": f"Task {instance.id}",
                    "singletaskdetail": True,
                    "debug": settings.DEBUG,
                },
            )

        # scoped to the task's owner, not to request.user: a staff member may be viewing someone
        # else's task, and their own tasks say nothing about whether this one has changed
        etag = get_tasklist_etag(request, instance.user_id)
        if etag == request.META.get("HTTP_IF_NONE_MATCH"):
            return HttpResponseNotModified()

        return Response(self.get_serializer(instance).data, headers={"ETag": etag})


class RequestImages(APIView):
    # permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    permission_classes = [ForcePhotPermission]

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
        if not request.user.is_authenticated:
            raise PermissionDenied

        try:
            parent_task = Task.objects.get(id=pk, request_type="FP", is_archived=False)

            if parent_task.user.id != request.user.id and not request.user.is_staff:
                raise PermissionDenied

        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")

        redirurl = reverse("task-list")

        if self.request.user and self.request.user.is_authenticated:
            userimziptaskcount = Task.objects.filter(
                user_id=self.request.user.pk, request_type="IMGZIP", is_archived=False
            ).count()
            if userimziptaskcount >= MAX_USER_IMGZIP_TASKS:
                msg = f"You have too many IMGZIP tasks ({userimziptaskcount} >= {MAX_USER_IMGZIP_TASKS}). Delete some before making new requests."
                return JsonResponse({"non_field_errors": msg}, status=429)

        if not parent_task.error_msg and parent_task.finishtimestamp:
            # which fields the child inherits is the model's decision; see Task.new_imagerequest
            newtask = parent_task.new_imagerequest(user=request.user)
            for field, value in client_location_fields(self.request).items():
                setattr(newtask, field, value)
            newtask.queuepos_relative = next_queuepos_relative()
            newtask.save()
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
    from bokeh.embed import components

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
        # match_aspect=True,
        aspect_ratio=2,
        background_fill_color="#040154",
        active_scroll="wheel_zoom",
        title="Recently requested coordinates",
        x_axis_label="Right ascension (deg)",
        y_axis_label="Declination (deg)",
        x_range=bokeh.models.Range1d(start=0, end=360),
        y_range=bokeh.models.Range1d(start=-90.0, end=90.0),
        # frame_width=600,
        sizing_mode="stretch_both",
        output_backend="webgl",
    )

    plot.grid.visible = False

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

    script, strhtml = components(plot)

    return JsonResponse({"script": script, "div": strhtml})


# the same 15 minutes as statsshortterm, which windows over the same data. This was 30 seconds,
# which had the most expensive chart on the stats page being rebuilt from a 14-day scan of the task
# table for practically every visitor.
@cache_page(60 * 15, cache="usagestats")
def statsusagechart(request):
    # deferred: see statscoordchart
    import bokeh.layouts
    import bokeh.models
    import bokeh.plotting
    from bokeh.embed import components

    days_back = 14

    def get_days_ago_counts(tasks) -> list[float]:
        taskcounts = (
            tasks.annotate(queueday=Trunc("timestamp", "day")).values("queueday").annotate(taskcount=Count("id"))
        )
        dictcounts = {(today - task["queueday"]).total_seconds() // 86400: task["taskcount"] for task in taskcounts}

        return [dictcounts.get(d, 0.0) for d in reversed(range(days_back))]

    today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    waitingtasks = Task.objects.filter(
        timestamp__gt=today - datetime.timedelta(days=days_back), finishtimestamp__isnull=True
    )

    waitingtasks_api = waitingtasks.filter(from_api=True)
    waitingtasks_nonapi = waitingtasks.filter(from_api=False)

    daywaitingcounts_api = get_days_ago_counts(waitingtasks_api)
    daywaitingcounts_nonapi = get_days_ago_counts(waitingtasks_nonapi)

    finishedtasks = Task.objects.filter(
        timestamp__gt=today - datetime.timedelta(days=days_back), finishtimestamp__isnull=False
    )

    dayfinished_web_counts = get_days_ago_counts(finishedtasks.filter(from_api=False, request_type="FP"))
    dayfinished_api_counts = get_days_ago_counts(finishedtasks.filter(from_api=True, request_type="FP"))
    dayfinished_img_counts = get_days_ago_counts(finishedtasks.filter(request_type="IMGZIP"))

    data = {
        "queueday": [(today - datetime.timedelta(days=d)).strftime("%b %d") for d in reversed(range(days_back))],
        "waitingtaskcount_api": daywaitingcounts_api,
        "waitingtaskcount_nonapi": daywaitingcounts_nonapi,
        "dayfinished_web_counts": dayfinished_web_counts,
        "dayfinished_api_counts": dayfinished_api_counts,
        "dayfinished_img_counts": dayfinished_img_counts,
    }

    datasource = bokeh.plotting.ColumnDataSource(data=data)

    fig_api = bokeh.plotting.figure(
        x_range=bokeh.models.FactorRange(factors=[*data["queueday"], " ", "  ", "   "]),
        y_range=bokeh.models.DataRange1d(start=0.0),
        tools=[
            bokeh.models.HoverTool(
                tooltips=[
                    ("Finished (API)", "@dayfinished_api_counts"),
                    ("Waiting (API)", "@waitingtaskcount_api"),
                ],
            )
        ],
        toolbar_location=None,
        aspect_ratio=5,
        title="API",
        sizing_mode="stretch_both",
        output_backend="svg",
        y_axis_label="API tasks per day",
    )

    fig_api.grid.visible = False

    fig_api.vbar_stack(
        ["waitingtaskcount_api", "dayfinished_api_counts"],
        x="queueday",
        source=datasource,
        color=["red", "lightgrey"],
        legend_label=["Waiting (API)", "Finished (API)"],
        line_width=0.0,
        width=0.3,
    )

    fig_nonapi = bokeh.plotting.figure(
        x_range=bokeh.models.FactorRange(factors=[*data["queueday"], " ", "  ", "   "]),
        y_range=bokeh.models.DataRange1d(start=0.0),
        tools=[
            bokeh.models.HoverTool(
                tooltips=[
                    ("Finished (web images)", "@dayfinished_img_counts"),
                    ("Finished (web FP)", "@dayfinished_web_counts"),
                    ("Waiting (web)", "@waitingtaskcount_nonapi"),
                ],
            )
        ],
        toolbar_location=None,
        aspect_ratio=5,
        title="Web",
        sizing_mode="stretch_both",
        output_backend="svg",
        y_axis_label="Web tasks per day",
    )

    fig_nonapi.vbar_stack(
        ["waitingtaskcount_nonapi", "dayfinished_web_counts", "dayfinished_img_counts"],
        x="queueday",
        source=datasource,
        color=["red", "green", "blue"],
        legend_label=["Waiting (web)", "Finished (web FP)", "Finished (web images)"],
        line_width=0.0,
        width=0.3,
    )

    fig_nonapi.legend[0].border_line_width = 0

    fig_nonapi.grid.visible = False

    plot = bokeh.layouts.column(
        [fig_api, fig_nonapi],
        sizing_mode="stretch_both",
        aspect_ratio=2.5,
    )

    script, strhtml = components(plot)

    return JsonResponse({"script": script, "div": strhtml})


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

    dictparams["sevendaympctasks"] = int(sevendaytasks.filter(mpc_name__isnull=False).count())
    dictparams["sevendayimgtasks"] = int(sevendaytasks.filter(request_type="IMGZIP").count())

    sevendaytasks_finished = sevendaytasks.filter(finishtimestamp__isnull=False)

    def mean_seconds_str(values: list[float]) -> str:
        """Format the mean of a list of second counts, ignoring NaNs and empty lists."""
        finitevalues = [value for value in values if math.isfinite(value)]
        return f"{statistics.fmean(finitevalues):.1f}s" if finitevalues else "-"

    if sevendaytasks_finished.count() > 0:
        dictparams["sevendayavgwaittime"] = mean_seconds_str([tsk.waittime() for tsk in sevendaytasks_finished])

        sevendaytasks_finished_firstusertasks = sevendaytasks_finished.filter(userqueuedtasks_on_submit=0)
        dictparams["sevendayavgwaittimeuserfirst"] = mean_seconds_str(
            [tsk.waittime() for tsk in sevendaytasks_finished_firstusertasks]
        )

        sevenday_runtimes = [tsk.runtime() for tsk in sevendaytasks_finished]
        sevenday_finite_runtimes = [runtime for runtime in sevenday_runtimes if math.isfinite(runtime)]
        dictparams["sevendayavgruntime"] = mean_seconds_str(sevenday_runtimes)
        num_job_processors = 16
        sevenday_mean_runtime = statistics.fmean(sevenday_finite_runtimes) if sevenday_finite_runtimes else 0.0
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
    positions = Task.objects.filter(
        user_id=request.user.pk, finishtimestamp__isnull=True, is_archived=False, queuepos_relative__isnull=False
    ).values_list("id", "queuepos_relative")

    return JsonResponse(
        {
            "queuepositions": {str(taskid): queuepos - queueoffset for taskid, queuepos in positions},
            "queueoffset": queueoffset,
        }
    )


def taskrunnerstatus(request):
    """Report whether the task runner is alive and what it is doing.

    Reads the snapshot the runner writes every few seconds. `stale` is the field to alert on: it
    means the runner has not refreshed the file recently, which is what a crashed or wedged runner
    looks like from outside. The runner writes into its own log directory, which the web server
    process can read but does not serve, so the file is not reachable as a static asset.

    A status file that cannot be interpreted is reported the same way as a missing one. This
    endpoint exists to say that something is wrong with the runner, so it must not be the thing that
    raises: a 500 here tells the caller nothing and mails the admins as well.
    """
    try:
        status = json.loads(runnerstatus.STATUS_PATH.read_text())
        # this doubles as the check that the payload is an object at all: subscripting a JSON list,
        # string or number raises TypeError, which is caught below along with everything else
        written = datetime.datetime.fromisoformat(status["written"])
        age_seconds = (datetime.datetime.now(datetime.UTC) - written).total_seconds()
    except (OSError, KeyError, TypeError, ValueError) as ex:
        # never written, unreadable, half-written despite the atomic rename, or written by a
        # version that recorded something else. A naive `written` lands here as a TypeError.
        return JsonResponse(
            {"running": False, "stale": True, "detail": f"no usable status file ({type(ex).__name__})"}, status=503
        )

    # several missed writes rather than one, so that a slow write does not raise a false alarm
    stale = age_seconds > (runnerstatus.STATUS_WRITE_SECONDS * 4)

    return JsonResponse(
        {**status, "running": not stale, "stale": stale, "status_age_seconds": round(age_seconds, 1)},
        status=503 if stale else 200,
    )


def stats(request):
    dictparams = {
        "name": "Usage Statistics",
        "queuedtaskcount": Task.objects.filter(finishtimestamp__isnull=True).count(),
    }

    lastfinishedtask = Task.objects.filter(finishtimestamp__isnull=False).order_by("finishtimestamp").last()
    if lastfinishedtask is not None:
        dictparams["lastfinishtime"] = f"{lastfinishedtask.finishtimestamp:%Y-%m-%d %H:%M:%S %Z}"
    else:
        dictparams["lastfinishtime"] = "N/A"

    return render(request, "stats.html", dictparams)


def register(request):
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get("username")
            raw_password = form.cleaned_data.get("password1")
            user = authenticate(username=username, password=raw_password)
            login(request, user)
            return redirect(reverse("task-list"))
    else:
        form = RegistrationForm()

    # "name" is what the base template builds the page <title> from, the same way the pages
    # declared with extra_context in urls.py do
    return render(request, "registration/register.html", {"form": form, "name": "Register"})


@login_required
def change_email(request):
    success = False
    if request.method == "POST":
        form = EmailChangeForm(request.user, request.POST)
        if form.is_valid():
            form.save()
            success = True
            form = EmailChangeForm(request.user)
    else:
        form = EmailChangeForm(request.user)

    return render(
        request,
        "registration/email_change_form.html",
        {"form": form, "success": success, "name": "Email address change"},
    )


_plotscript_cache: dict[Path, tuple[float, int, str]] = {}


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
        task = Task.objects.get(id=taskid)
    except ObjectDoesNotExist:
        return HttpResponseNotFound("Page not found")
    if not task.finishtimestamp:
        return HttpResponseNotFound("Page not found")

    # disable etag for debugging
    etag = None if settings.DEBUG else datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")

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
            import pandas as pd

            try:
                dfforcedphot = pd.read_csv(
                    resultfilepath,
                    sep=r"\s+",
                    escapechar="#",
                    dtype=float,
                    converters={"F": str, "Obs": str, "uJy": int, "duJy": int},
                )
            except ValueError:
                # an empty, truncated or ragged result file: EmptyDataError and ParserError are
                # both ValueError subclasses, and a non-numeric token in a well-formed row (e.g.
                # a diagnostic line mixed into the output) fails the dtype=float conversion with
                # a plain ValueError
                dfforcedphot = None
            else:
                # df.rename(columns={'#MJD': 'MJD'})

                ujy_min = int(-1e10)
                ujy_max = int(1e10)
                dfforcedphot = dfforcedphot[(dfforcedphot["uJy"] > ujy_min) & (dfforcedphot["uJy"] < ujy_max)]

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

            for color, filter in [(11, "c"), (12, "o"), (8, "w")]:
                dffilter = dfforcedphot.query("F == @filter", inplace=False)

                jsout.extend(
                    (
                        '\njslabels.push({"color": '
                        + str(color)
                        + ', "display": false, "label": "'
                        + filter
                        + '"});\n',
                        "jslcdata.push(["
                        + ", ".join(
                            [
                                f"[{mjd},{uJy},{duJy}]"
                                for _, (mjd, uJy, duJy) in dffilter[["#MJD", "uJy", "duJy"]].iterrows()
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

        # with jsplotfile.open("w") as f:
        #     f.writelines(jsout)

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
    headers = {"ETag": etag} if etag is not None else {}

    return HttpResponse(strjs, content_type="text/javascript", headers=headers)

    # if jsplotfile.exists():
    #     return FileResponse(open(jsplotfile, "rb"), headers={"ETag": etag})

    # return HttpResponseNotFound("ERROR: Could not create javascript file.")


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
        item = Task.objects.get(id=taskid)
    except ObjectDoesNotExist:
        return HttpResponseNotFound("Page not found")
    if resultfile := item.localresultfile():
        resultfilepath = Path(settings.STATIC_ROOT, resultfile)
        pdfpath = resultfilepath.with_suffix(".pdf")

        # to force a refresh of all plots
        # if os.path.exists(pdfpath):
        #     os.remove(pdfpath)

        if not pdfpath.is_file():
            # One render per task at a time. Each one forks matplotlib, so without this a task
            # whose PDF does not exist yet forks a process per concurrent request -- and the
            # queue page links the PDF for every finished task, so that is a normal thing for a
            # crawler or an impatient reload to cause. add() rather than get()/set(): it is the
            # only atomic operation the cache API offers, so two racing requests cannot both win.
            #
            # The lock expires a little after the render's own deadline so that a worker killed
            # mid-render (a deploy, an OOM) cannot wedge the task's plot until the cache is cleared.
            lockkey = f"pdfplot-lock-{taskid}"
            if not caches["default"].add(lockkey, True, timeout=PDF_PLOT_TIMEOUT_SECONDS + 30):
                return _pdfplot_unavailable()

            try:
                completed = make_pdf_plot(
                    taskid=taskid, localresultfile=resultfilepath, taskcomment=item.comment, separate_process=True
                )
            finally:
                caches["default"].delete(lockkey)

            if not completed:
                logger.warning("PDF plot generation for task %d exceeded its time limit and was killed", taskid)
                return _pdfplot_unavailable()

        if pdfpath.is_file():
            return FileResponse(pdfpath.open("rb"))

    return HttpResponseNotFound("ERROR: Could not generate PDF plot (perhaps a lack of data points?)")


# Results are public on purpose, like the task detail itself — the reasoning lives on
# ForcePhotPermission.has_object_permission. The file views below (and taskpdfplot and
# resultplotdatajs above) therefore take a task id and no credentials; note the serializer also
# links results at their STATIC_URL path, which Apache serves without consulting Django at all.
#
# no @cache_page on the result file views: Django's cache middleware skips streaming responses, so
# decorating a view that returns a FileResponse only adds a lookup that can never hit
def _taskresultfile_response(
    taskid: int, getfile: Callable[[Task], str | Path | None]
) -> FileResponse | HttpResponseNotFound:
    """Serve one of a task's result files, or 404 when the task or the file does not exist."""
    task = Task.objects.filter(id=taskid).first() if taskid else None
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
