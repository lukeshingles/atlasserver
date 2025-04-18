"""Django views for the forcephot app."""

import contextlib
import datetime
from pathlib import Path
from typing import Any

import bokeh.layouts
import bokeh.models
import bokeh.plotting
import numpy as np
from bokeh.embed import components
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import login
from django.contrib.gis.geoip2 import GeoIP2
from django.core.cache import caches
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied

# from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count
from django.db.models import Max
from django.db.models.functions import Trunc
from django.forms import model_to_dict
from django.http import FileResponse
from django.http import HttpResponse
from django.http import HttpResponseNotFound
from django.http import HttpResponseNotModified
from django.http import JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.cache import cache_page
from django_filters.rest_framework import DjangoFilterBackend
from geoip2.errors import AddressNotFoundError
from rest_framework import filters
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.utils.urls import replace_query_param
from rest_framework.views import APIView

from atlasserver.forcephot.filters import TaskFilter
from atlasserver.forcephot.forms import RegistrationForm
from atlasserver.forcephot.misc import country_code_to_name
from atlasserver.forcephot.misc import country_region_to_name
from atlasserver.forcephot.misc import datetime_to_mjd
from atlasserver.forcephot.misc import make_pdf_plot
from atlasserver.forcephot.misc import splitradeclist
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.serializers import ForcePhotTaskSerializer

# from django.http import HttpResponse
# from django.http.response import HttpResponseRedirect
# from django.shortcuts import get_object_or_404
# from rest_framework.utils.urls import remove_query_param
maximgziptasks = 5


def calculate_queue_positions() -> None:
    """Calculate and assign the queue positions (determining the order of execution in the task runner) for all queued tasks."""
    with transaction.atomic():
        queuedtasks = (
            Task.objects.all().filter(finishtimestamp__isnull=True, is_archived=False).order_by("user_id", "timestamp")
        )

        # to get position in current pass, check if job currently running
        query_currentlyrunningtask = queuedtasks.filter(starttimestamp__isnull=False).order_by("-starttimestamp")

        runningtaskid = None
        runningtask_userid = None

        try:
            if query_currentlyrunningtask.exists():
                runningtask = query_currentlyrunningtask.first()
                if runningtask is not None:
                    runningtaskid = runningtask.id
                    runningtask_userid = runningtask.user_id

        except AttributeError:
            runningtaskid = None
            runningtask_userid = None

        queuedtaskcount = queuedtasks.count()

        unassigned_taskids = [t.id for t in queuedtasks]
        unassigned_task_userids = [t.user_id for t in queuedtasks]

        # work through passes (max one task per user in each pass) assigning queue positions from 0 (next) upwards
        queuepos: int = 0
        passnum: int = 0
        # queuepos_updates = []  # for bulk_update()
        while unassigned_taskids:
            useridsassigned_currentpass = set()

            if passnum == 0 and runningtaskid is not None:
                # currently running task will be assigned position 0

                # method 1
                Task.objects.filter(id=runningtaskid).update(queuepos_relative=0)

                # method 2
                # queuepos_updates.append((currentlyrunningtaskid, 0))

                try:
                    index = unassigned_taskids.index(runningtaskid)
                    unassigned_taskids.pop(index)
                    unassigned_task_userids.pop(index)
                    useridsassigned_currentpass.add(runningtask_userid)
                    queuepos = 1
                except ValueError:  # the task disappeared between the two queries?
                    runningtaskid = None

            for i, (taskid, task_userid) in enumerate(zip(unassigned_taskids, unassigned_task_userids, strict=True)):
                if task_userid not in useridsassigned_currentpass and (
                    passnum != 0 or runningtask_userid is None or (task_userid > runningtask_userid)
                ):
                    # method 1
                    Task.objects.filter(id=taskid).update(queuepos_relative=queuepos)

                    # method 2
                    # queuepos_updates.append((task.id, queuepos))

                    unassigned_taskids.pop(i)
                    unassigned_task_userids.pop(i)
                    useridsassigned_currentpass.add(task_userid)
                    queuepos += 1

            assert passnum < (2 * queuedtaskcount + 1)  # prevent infinite loop if we're failing to assign anything

            passnum += 1

        # method 2
        # Task.objects.bulk_update([Task(id=k, queuepos_relative=v) for k, v in queuepos_updates], ["queuepos_relative"])


def get_tasklist_etag(request, queryset) -> str:
    """Return an etag that will change when the task list changes."""
    if settings.DEBUG:
        todaydate = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d %H:%M:%S")
    else:
        todaydate = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d %H:%M")

    last_queued = Task.objects.filter().aggregate(Max("timestamp"))["timestamp__max"]
    last_started = Task.objects.filter().aggregate(Max("starttimestamp"))["starttimestamp__max"]
    last_finished = Task.objects.filter().aggregate(Max("finishtimestamp"))["finishtimestamp__max"]
    taskids = "-".join([str(row.id) for row in queryset])
    return f"{todaydate}.{request.accepted_renderer.format}.user{request.user.id}.lastqueue{last_queued}.laststart{last_started}.lastfinish{last_finished}.tasks{taskids}"


class ForcePhotPermission(permissions.BasePermission):
    """Custom permission to only allow owners of an object to edit it."""

    message = "You must be the owner of this object."

    def has_permission(self, request, view) -> bool:
        return bool(request.method in permissions.SAFE_METHODS or request.user.is_authenticated)

    #     return request.user and request.user.is_authenticated

    """
    Object-level permission to only allow owners of an object to edit it.
    Assumes the model instance has an `user` attribute.
    """

    def has_object_permission(self, request, view, obj) -> bool:
        # Read permissions are allowed to any request,
        # so we'll always allow GET, HEAD or OPTIONS requests.
        if request.method in permissions.SAFE_METHODS:
            return True

        if not request.user or not request.user.is_authenticated:
            return False

        # staff and instance owner have all permissions
        return True if request.user.is_staff else obj.user.id == request.user.id


class ForcePhotTaskViewSet(viewsets.ModelViewSet):
    """API endpoint that allows force.sh tasks to be created and deleted."""

    queryset = Task.objects.all().order_by("-timestamp", "-id").select_related("user")
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
        if not request.user.is_authenticated:
            raise PermissionDenied

        if "radeclist" in request.data:
            datalist = splitradeclist(request.data)
            serializer = self.get_serializer(data=datalist, many=True)
        else:
            serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer) -> None:
        """Create new task(s)."""
        extra_fields: dict[str, Any] = {
            "user": self.request.user,
            "timestamp": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        }

        if x_forwarded_for := self.request.META.get("HTTP_X_FORWARDED_FOR"):
            ip = x_forwarded_for.split(",")[0]
        else:
            ip = self.request.META.get("REMOTE_ADDR")

        if ip.startswith(("192.168.", "127.0.", "10.")):
            ip = "qub.ac.uk"

        geoip = GeoIP2()
        with contextlib.suppress(AddressNotFoundError):
            location = geoip.city(ip)
            extra_fields["country_code"] = location["country_code"]
            extra_fields["region"] = location["region_code"]

        extra_fields["from_api"] = "HTTP_REFERER" not in self.request.META

        serializer.save(**extra_fields)
        calculate_queue_positions()

    def perform_update(self, serializer) -> None:
        """Update a task."""
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance) -> None:
        """Delete a task, and if the task is queued (not finished), then update queue positions."""
        update_queue_positions = not instance.finishtimestamp
        instance.delete()
        if update_queue_positions:
            calculate_queue_positions()

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
                    "api_url_base": request.build_absolute_uri(reverse("task-list")),
                },
            )

        page = self.paginate_queryset(listqueryset)
        if page is not None:
            # if request.GET.get('cursor') and page[0].id == listqueryset[0].id:
            #     return redirect(remove_query_param(request.get_full_path(), 'cursor'))
            serializer = self.get_serializer(page, many=True)
        else:
            serializer = self.get_serializer(listqueryset, many=True)

        if page is not None:
            etag = get_tasklist_etag(request, page)
            if "HTTP_IF_NONE_MATCH" in request.META and etag == request.META["HTTP_IF_NONE_MATCH"]:
                return HttpResponseNotModified()

            serializer = self.get_serializer(page, many=True)
            # return self.get_paginated_response(serializer.data)
            return self.paginator.get_paginated_response(serializer.data, headers={"ETag": etag})

        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_archived:
            return HttpResponseNotFound("Page not found")
        serializer = self.get_serializer(instance)

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
                    "name": f"Task {self.get_object().id}",
                    "singletaskdetail": True,
                    "debug": settings.DEBUG,
                    "api_url_base": request.build_absolute_uri(reverse("task-list")),
                },
            )

        etag = get_tasklist_etag(request, [instance])
        if "HTTP_IF_NONE_MATCH" in request.META and etag == request.META["HTTP_IF_NONE_MATCH"]:
            return HttpResponseNotModified()

        return Response(serializer.data, headers={"ETag": etag})


def deletetask(request, pk):
    """Delete a task. deprecated! remove when sure that this is not used anymore."""
    if not request.user.is_authenticated:
        raise PermissionDenied

    with contextlib.suppress(ObjectDoesNotExist):
        item = Task.objects.get(id=pk)

        if item.user.id != request.user.id and not request.user.is_staff:
            raise PermissionDenied

        item.delete()

    calculate_queue_positions()

    redirurl = request.META.get("HTTP_REFERER", reverse("task-list"))
    if f"/{pk}/" in redirurl:  # if referrer was the single-task view, it will not exist anymore
        redirurl = reverse("task-list")

    return redirect(redirurl, request=request)


class RequestImages(APIView):
    # permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    permission_classes = [ForcePhotPermission]

    def get(self, request, pk):
        if not request.user.is_authenticated:
            raise PermissionDenied

        try:
            parent_task = Task.objects.get(id=pk)

            if parent_task.user.id != request.user.id and not request.user.is_staff:
                raise PermissionDenied

        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")

        redirurl = reverse("task-list")

        if self.request.user and self.request.user.is_authenticated:
            userimziptaskcount = Task.objects.filter(
                user_id=self.request.user.pk, request_type="IMGZIP", is_archived=False
            ).count()
            if userimziptaskcount >= maximgziptasks:
                msg = f"You have too many IMGZIP tasks ({userimziptaskcount} >= {maximgziptasks}). Delete some before making new requests."
                return JsonResponse({"error": msg}, status=429)

        if not parent_task.error_msg and parent_task.finishtimestamp:
            data = model_to_dict(parent_task, exclude=["id"])
            data["parent_task_id"] = parent_task.id
            data["request_type"] = Task.RequestType.IMGZIP
            data["user"] = request.user
            data["timestamp"] = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()
            data["starttimestamp"] = None
            data["finishtimestamp"] = None

            if x_forwarded_for := self.request.META.get("HTTP_X_FORWARDED_FOR"):
                ip = x_forwarded_for.split(",")[0]
            else:
                ip = self.request.META.get("REMOTE_ADDR")

            if ip.startswith(("192.168.", "127.0.", "10.")):
                ip = "qub.ac.uk"

            geoip = GeoIP2()
            with contextlib.suppress(AddressNotFoundError):
                location = geoip.city(ip)
                data["country_code"] = location["country_code"]
                data["region"] = location["region_code"]

            data["from_api"] = False
            data["send_email"] = False

            newtask = Task(**data)
            newtask.save()
            calculate_queue_positions()

            redirurl = replace_query_param(reverse("task-list"), "newids", str(newtask.id))

        return redirect(redirurl, request=request)


@cache_page(60 * 60 * 24, cache="usagestats")
def statscoordchart(request):
    tasks = Task.objects.all().order_by("-timestamp")[:20000].select_related("user")

    dictsource = {
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
        x_range=bokeh.models.Range1d(0, 360, bounds="auto"),
        y_range=bokeh.models.Range1d(-90.0, 90.0, bounds="auto"),
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


@cache_page(30, cache="usagestats")
def statsusagechart(request):
    days_back = 14

    def get_days_ago_counts(tasks) -> list[float]:
        taskcounts = (
            tasks.annotate(queueday=Trunc("timestamp", "day")).values("queueday").annotate(taskcount=Count("id"))
        )
        dictcounts = {(today - task["queueday"]).total_seconds() // 86400: task["taskcount"] for task in taskcounts}

        return [dictcounts.get(d, 0.0) for d in reversed(range(days_back))]

    today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    waitingtasks = Task.objects.filter(timestamp__gt=today - datetime.timedelta(days=14), finishtimestamp__isnull=True)

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
        x_range=bokeh.models.FactorRange(*data["queueday"], " ", "  ", "   "),
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
        x_range=bokeh.models.FactorRange(*data["queueday"], " ", "  ", "   "),
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
        .annotate(task_count=Count("country_code"))
        .order_by("-task_count", "country_code")[:15]
    )

    def country_activeusers(country_code: str) -> int:
        return thirtydaytasks.filter(country_code=country_code).values_list("user_id").distinct().count()

    dictparams["countrylist"] = [
        (country_code_to_name(code), task_count, country_activeusers(code)) for code, task_count in countrylist
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
    sevendaytaskcount = int(sevendaytasks.count())

    dictparams = {
        "sevendaytasks": sevendaytaskcount,
        "sevendayusers": sevendaytasks.values_list("user_id").distinct().count(),
    }
    dictparams["sevendaytaskrate"] = "{:.1f}/day".format(dictparams["sevendaytasks"] / 7.0)

    dictparams["sevendaympctasks"] = int(sevendaytasks.filter(mpc_name__isnull=False).count())
    dictparams["sevendayimgtasks"] = int(sevendaytasks.filter(request_type="IMGZIP").count())

    sevendaytasks_finished = sevendaytasks.filter(finishtimestamp__isnull=False)

    if sevendaytasks_finished.count() > 0:
        dictparams["sevendayavgwaittime"] = (
            f"{np.nanmean(np.array([tsk.waittime() for tsk in sevendaytasks_finished])):.1f}s"
        )

        sevenday_runtimes = np.array([tsk.runtime() for tsk in sevendaytasks_finished])
        sevenday_mean_runtime = np.nanmean(sevenday_runtimes)
        dictparams["sevendayavgruntime"] = f"{sevenday_mean_runtime:.1f}s"
        num_job_processors = 8
        dictparams["sevendayloadpercent"] = (
            f"{100.0 * sevendaytaskcount * sevenday_mean_runtime / (7 * 24.0 * 60 * 60) / num_job_processors:.1f}%"
        )
    else:
        dictparams |= {
            "sevendayavgwaittime": "-",
            "sevendayavgruntime": "-",
            "sevendayloadpercent": "0%",
        }

    return render(request, "statsshortterm.html", dictparams)


def stats(request):
    dictparams = {
        "name": "Usage Statistics",
        "queuedtaskcount": Task.objects.filter(finishtimestamp__isnull=True).count(),
    }

    try:
        lastfinishtime = (
            Task.objects.filter(finishtimestamp__isnull=False).order_by("finishtimestamp").last().finishtimestamp
        )
        dictparams["lastfinishtime"] = f"{lastfinishtime:%Y-%m-%d %H:%M:%S %Z}"
    except AttributeError:
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

    return render(request, "registration/register.html", {"form": form})


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

    strjs = caches["taskderived"].get(f"task{taskid}_resultplotdatajs", default=None)

    if strjs is None:
        jsout = ['"use strict";\n']

        resultfilepath = None
        if task.localresultfile() is not None:
            resultfilepath = Path(settings.STATIC_ROOT, task.localresultfile())
            if not resultfilepath.is_file():
                resultfilepath = None

        if resultfilepath is not None:
            import pandas as pd

            dfforcedphot = pd.read_csv(
                resultfilepath,
                delim_whitespace=True,
                escapechar="#",
                dtype=float,
                converters={"F": str, "Obs": str, "uJy": int, "duJy": int},
            )
            # df.rename(columns={'#MJD': 'MJD'})

            ujy_min = int(-1e10)
            ujy_max = int(1e10)
            dfforcedphot = dfforcedphot[(dfforcedphot["uJy"] > ujy_min) & (dfforcedphot["uJy"] < ujy_max)]

            jsout.extend(
                (
                    "var jslcdata = new Array();\n",
                    "var jslabels = new Array();\n",
                )
            )
            divid = f"plotforcedflux-task-{taskid}"

            for color, filter in [(11, "c"), (12, "o")]:
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
            if settings.DEBUG:
                jsout.append(Path(settings.STATIC_ROOT, "js/queuepage/src/lightcurveplotly.js").open("rt").read())
            else:
                jsout.append(Path(settings.STATIC_ROOT, "js/lightcurveplotly.min.js").open("rt").read())

        strjs = "".join(jsout)

        # with jsplotfile.open("w") as f:
        #     f.writelines(jsout)

        caches["taskderived"].set(f"task{taskid}_resultplotdatajs", strjs)

    return HttpResponse(strjs, content_type="text/javascript", headers={"ETag": etag})

    # if jsplotfile.exists():
    #     return FileResponse(open(jsplotfile, "rb"), headers={"ETag": etag})

    # return HttpResponseNotFound("ERROR: Could not create javascript file.")


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
            # matplotlib needs to run in its own process or it will crash
            make_pdf_plot(
                taskid=taskid, localresultfile=resultfilepath, taskcomment=item.comment, separate_process=True
            )

        if pdfpath.is_file():
            return FileResponse(pdfpath.open("rb"))

    return HttpResponseNotFound("ERROR: Could not generate PDF plot (perhaps a lack of data points?)")


def taskresultdata(request, taskid):
    item = None
    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")

    if item and (resultfile := item.localresultfile()):
        resultfilepath = Path(settings.STATIC_ROOT, resultfile)

        if resultfilepath.is_file():
            return FileResponse(resultfilepath.open("rb"))

    return HttpResponseNotFound("Page not found")


@cache_page(60 * 60, cache="taskderived")
def taskpreviewimage(request, taskid: int):
    item = None
    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")

    if item and (previewimagefile := item.localresultpreviewimagefile):
        previewimagefilepath = Path(settings.STATIC_ROOT, previewimagefile)

        if previewimagefilepath.is_file():
            return FileResponse(previewimagefilepath.open("rb"))

    return HttpResponseNotFound("Page not found")


def taskimagezip(request, taskid: int):
    item = None
    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")

    if item and (resultfile := item.localresultimagezipfile):
        resultfilepath = Path(settings.STATIC_ROOT, resultfile)

        if resultfilepath.is_file():
            return FileResponse(resultfilepath.open("rb"))

    return HttpResponseNotFound("Page not found")
