import datetime
import os

import numpy as np
# import geoip2.errors

from django.conf import settings as settings
from django.contrib.auth import authenticate, login
# from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
# from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Min, Max
from django.forms import model_to_dict
# from django.http import HttpResponse
from django.http import FileResponse
# from django.http.response import HttpResponseRedirect
from django.http import HttpResponseNotFound
from django.http import HttpResponseNotModified
from django.http import JsonResponse
from django.views.decorators.cache import cache_page
# from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, status, viewsets
# from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.utils.urls import remove_query_param, replace_query_param

from pathlib import Path

from forcephot.filters import TaskFilter
from forcephot.forms import TaskForm, RegistrationForm
from forcephot.misc import country_code_to_name, country_region_to_name, splitradeclist, date_to_mjd, make_pdf_plot
from forcephot.models import Task
from forcephot.serializers import ForcePhotTaskSerializer


def calculate_queue_positions():
    # the last started task user id can be passed in, in case the associated task
    # got cancelled
    # the user of the last completed task (to get position in current pass)
    laststartedtask = Task.objects.filter(finishtimestamp__isnull=False).order_by('-starttimestamp').first()

    queuedtasks = Task.objects.filter(finishtimestamp__isnull=True, is_archived=False).order_by('user_id', 'timestamp')
    queuedtaskcount = queuedtasks.count()
    queuedtasks.update(queuepos_relative=None)

    queuepos = 0
    passnum = 0
    while queuepos < queuedtaskcount:
        useridsassigned_currentpass = set()
        if passnum == 0 and not laststartedtask.finishtimestamp:
            # currently running task will be assigned position 0
            laststartedtask.queuepos_relative = 0
            laststartedtask.save()
            useridsassigned_currentpass.add(laststartedtask.user_id)
            queuepos = 1

        unassigned_tasks = queuedtasks.filter(queuepos_relative__isnull=True)
        if unassigned_tasks.count() == 0:
            break

        for task in unassigned_tasks:

            if (task.user_id not in useridsassigned_currentpass and
                    (passnum != 0 or task.user_id > laststartedtask.user_id)):
                # print(queuepos, task)
                task.queuepos_relative = queuepos
                task.save()
                useridsassigned_currentpass.add(task.user_id)
                queuepos += 1

        passnum += 1


def get_tasklist_etag(request, queryset):
    if settings.DEBUG:
        todaydate = datetime.datetime.utcnow()
    else:
        todaydate = datetime.datetime.utcnow().strftime("%Y%m%d %H:%M")
    last_queued = Task.objects.filter().aggregate(Max('timestamp'))['timestamp__max']
    last_started = Task.objects.filter().aggregate(Max('starttimestamp'))['starttimestamp__max']
    last_finished = Task.objects.filter().aggregate(Max('finishtimestamp'))['finishtimestamp__max']
    taskid_list = '-'.join([str(row.id) for row in queryset])
    etag = (f'{todaydate}.{request.accepted_renderer.format}.user{request.user.id}.'
            f'lastqueue{last_queued}.laststart{last_started}.lastfinish{last_finished}.tasks{taskid_list}')

    return etag


class ForcePhotPermission(permissions.BasePermission):
    message = 'You must be the owner of this object.'

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True

        if request.method in ['PUT', 'PATCH'] and not request.user.is_authenticated:
            return False

        return True
    #     return request.user and request.user.is_authenticated

    """
    Object-level permission to only allow owners of an object to edit it.
    Assumes the model instance has an `user` attribute.
    """
    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed to any request,
        # so we'll always allow GET, HEAD or OPTIONS requests.
        if request.method in permissions.SAFE_METHODS:
            return True

        if not request.user or not request.user.is_authenticated:
            return False

        if request.user.is_staff:
            return True

        # Instance owner must match current user
        return obj.user.id == request.user.id


class ForcePhotTaskViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows force.sh tasks to be created and deleted.
    """
    queryset = Task.objects.all().order_by('-timestamp', '-id').select_related('user')
    serializer_class = ForcePhotTaskSerializer
    # permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    permission_classes = [ForcePhotPermission]
    throttle_scope = 'forcephottasks'
    ordering_fields = ['timestamp', 'id']
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    filterset_class = TaskFilter
    ordering = '-id'
    # filterset_fields = ['finishtimestamp']
    template_name = 'tasklist-react.html'

    def create(self, request, *args, **kwargs):
        if request.accepted_renderer.format == 'html':
            form = TaskForm(request.POST)
            success = False
            if form.is_valid():
                datalist = splitradeclist(request.data)
                if datalist:
                    serializer = self.get_serializer(data=datalist, many=True)
                    success = serializer.is_valid(raise_exception=True)
                    self.perform_create(serializer)
                    kwargs['headers'] = self.get_success_headers(serializer.data)

                    # this single post request may have actually contained multiple tasks,
                    # so manually increment the throttles as if we made extra requests
                    for throttle in self.get_throttles():
                        for _ in range(len(datalist) - 1):
                            throttle.allow_request(request=request, view=self)
                else:
                    success = False

            if success:
                strnewids = ','.join([str(item['id']) for item in serializer.data])
                redirurl = request.META['HTTP_REFERER'] if 'HTTP_REFERER' in request.META else reverse('task-list')
                redirurl = replace_query_param(redirurl, 'newids', strnewids)
                return redirect(redirurl, status=status.HTTP_201_CREATED, headers=kwargs['headers'])

            kwargs['form'] = form
            return self.list(request, *args, **kwargs)
        else:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        # if self.request.user and self.request.user.is_authenticated:
        #     usertasks = Task.objects.filter(user_id=self.request.user, finished=False)
        #     usertaskcount = usertasks.count()
        #     if (usertaskcount > 10):
        #         raise ValidationError(f'You have too many queued tasks ({usertaskcount}).')
        #     serializer.save(user=self.request.user)
        extra_fields = {}  # add computed field values (not user specified)

        extra_fields['user'] = self.request.user
        extra_fields['timestamp'] = datetime.datetime.utcnow().replace(
            tzinfo=datetime.timezone.utc, microsecond=0).isoformat()

        extra_fields['country_code'] = self.request.geo_data.country_code
        extra_fields['region'] = self.request.geo_data.region
        extra_fields['city'] = self.request.geo_data.city

        extra_fields['from_api'] = (self.request.accepted_renderer.format != 'html')

        serializer.save(**extra_fields)
        calculate_queue_positions()

    def perform_update(self, serializer):
        serializer.save(user=self.request.user)

    # def destroy(self, request, *args, **kwargs):
    #     instance = self.get_object()
    #     self.perform_destroy(instance)
    #     # print(reverse('task-list', request=request))
    #     # if request.accepted_renderer.format == 'html':
    #     # return Response(status=status.HTTP_303_SEE_OTHER, headers={
    #     #         'Location': reverse('task-list', request=request)})
    #     return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance):
        instance.delete()
        calculate_queue_positions()

    def list(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            listqueryset = self.filter_queryset(self.get_queryset().filter(is_archived=False, user_id=request.user))
        else:
            listqueryset = Task.objects.none()
            raise PermissionDenied()

        page = self.paginate_queryset(listqueryset)
        if page is not None:
            # if request.GET.get('cursor') and page[0].id == listqueryset[0].id:
            #     return redirect(remove_query_param(request.get_full_path(), 'cursor'))
            serializer = self.get_serializer(page, many=True)
        else:
            serializer = self.get_serializer(listqueryset, many=True)

        # taskframeonly is for javascript updates (no header/menubar)
        htmltaskframeonly = request.GET.get('htmltaskframeonly', False)

        if request.accepted_renderer.format == 'html' or htmltaskframeonly:
            if not page and listqueryset:  # empty page, redirect to top of list
                return redirect(reverse('task-list'), request=request)

            if 'form' in kwargs:
                form = kwargs['form']
            else:
                form = TaskForm()

            # template = 'tasklist-frame.html' if htmltaskframeonly else self.template_name
            # if 'usereact' in request.GET:
            #     template = 'tasklist-react.html'

            template = 'tasklist-frame.html' if htmltaskframeonly else 'tasklist-react.html'

            return Response(template_name=template, data={
                'serializer': serializer, 'data': serializer.data, 'tasks': page,
                'form': form, 'name': 'Task Queue', 'singletaskdetail': False,
                'paginator': self.paginator, 'usertaskcount': listqueryset.count(),
                'api_url_base': request.build_absolute_uri(reverse('index')),
            })

        if page is not None:
            etag = get_tasklist_etag(request, page)
            if 'HTTP_IF_NONE_MATCH' in request.META and etag == request.META['HTTP_IF_NONE_MATCH']:
                return HttpResponseNotModified()

            serializer = self.get_serializer(page, many=True)
            # return self.get_paginated_response(serializer.data)
            return self.paginator.get_paginated_response(serializer.data, headers={'ETag': etag})

        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_archived:
            return HttpResponseNotFound("Page not found")
        serializer = self.get_serializer(instance)

        htmltaskframeonly = request.GET.get('htmltaskframeonly', False)

        if request.accepted_renderer.format == 'html' or htmltaskframeonly:
            # return redirect('/')
            # queryset = self.filter_queryset(self.get_queryset())
            # serializer = self.get_serializer(queryset, many=True)

            template = 'tasklist-frame.html' if htmltaskframeonly else 'tasklist-react.html'

            tasks = [instance]
            form = TaskForm()
            return Response(template_name=template, data={
                'serializer': serializer, 'data': serializer.data, 'tasks': tasks, 'form': form,
                'name': f'Task {self.get_object().id}', 'singletaskdetail': True,
                'api_url_base': request.build_absolute_uri(reverse('index')),
            })

        etag = get_tasklist_etag(request, [instance])
        if 'HTTP_IF_NONE_MATCH' in request.META and etag == request.META['HTTP_IF_NONE_MATCH']:
            return HttpResponseNotModified()

        return Response(serializer.data, headers={'ETag': etag})


def deletetask(request, pk):
    """ deprecated! remove when sure that this is not used anymore """
    if not request.user.is_authenticated:
        raise PermissionDenied()

    try:
        item = Task.objects.get(id=pk)

        if item.user.id != request.user.id and not request.user.is_staff:
            raise PermissionDenied()

        item.delete()

    except ObjectDoesNotExist:
        pass

    calculate_queue_positions()

    redirurl = request.META.get('HTTP_REFERER', reverse('task-list'))
    if f'/{pk}/' in redirurl:  # if referrer was the single-task view, it will not exist anymore
        redirurl = reverse('task-list')

    return redirect(redirurl, request=request)


def requestimages(request, pk):
    if not request.user.is_authenticated:
        raise PermissionDenied()

    try:
        parent_task = Task.objects.get(id=pk)

        if parent_task.user.id != request.user.id and not request.user.is_staff:
            raise PermissionDenied()

    except ObjectDoesNotExist:
        return HttpResponseNotFound("Page not found")

    redirurl = reverse('task-list')

    if not parent_task.error_msg and parent_task.finishtimestamp:
        data = model_to_dict(parent_task, exclude=['id'])
        data['parent_task_id'] = parent_task.id
        data['request_type'] = Task.RequestType.IMGZIP
        data['user'] = request.user
        data['timestamp'] = datetime.datetime.utcnow().replace(
            tzinfo=datetime.timezone.utc, microsecond=0).isoformat()
        data['starttimestamp'] = None
        data['finishtimestamp'] = None

        data['country_code'] = request.geo_data.country_code
        data['region'] = request.geo_data.region
        data['city'] = request.geo_data.city

        data['from_api'] = False
        data['send_email'] = False

        newtask = Task(**data)
        newtask.save()
        calculate_queue_positions()

        redirurl = replace_query_param(reverse('task-list'), 'newids', str(newtask.id))

    return redirect(redirurl, request=request)


@cache_page(60 * 60 * 24)
def statscoordchart(request):
    # now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    # tasks = Task.objects.filter(timestamp__gt=now - datetime.timedelta(days=7))

    tasks = Task.objects.all().order_by('-timestamp')[:30000].select_related('user')

    # from bokeh.io import output_file, show
    # from bokeh.transform import linear_cmap
    # from bokeh.util.hex import hexbin
    from bokeh.embed import components
    from bokeh.models import HoverTool
    from bokeh.models import Range1d
    from bokeh.plotting import figure
    from bokeh.plotting import ColumnDataSource
    # from bokeh.resources import CDN

    dictsource = {
            'ra': [tsk.ra for tsk in tasks],
            'dec':  [tsk.dec for tsk in tasks],
            'taskid': [tsk.id for tsk in tasks],
            'username': [tsk.user.username for tsk in tasks],
    }
    source = ColumnDataSource(dictsource)

    plot = figure(
        tools="pan,wheel_zoom,box_zoom,reset",
        # match_aspect=True,
        aspect_ratio=2,
        background_fill_color='#040154',
        active_scroll="wheel_zoom",
        title="Recently requested coordinates",
        x_axis_label="Right ascension (deg)",
        y_axis_label="Declination (deg)",
        x_range=Range1d(0, 360, bounds="auto"),
        y_range=Range1d(-90., 90., bounds="auto"),
        # frame_width=600,
        sizing_mode='stretch_both',
        output_backend="webgl")

    plot.grid.visible = False

    r = plot.circle('ra', 'dec', source=source, color="white", radius=0.05,
                    hover_color="orange", alpha=0.7, hover_alpha=1.0)

    plot.add_tools(HoverTool(
        tooltips="Task @taskid, RA Dec: @ra @dec, user: @username",
        mode="mouse", point_policy="follow_mouse", renderers=[r]))

    script, strhtml = components(plot)

    return JsonResponse({"script": script, "div": strhtml})


@cache_page(30)
def statsusagechart(request):
    from django.db.models.functions import Trunc
    # from bokeh.io import output_file, show
    # from bokeh.transform import linear_cmap
    # from bokeh.util.hex import hexbin
    from bokeh.embed import components
    from bokeh.models import HoverTool
    # from bokeh.models import SingleIntervalTicker, LinearAxis
    # from bokeh.models import Range1d
    from bokeh.models import DataRange1d
    from bokeh.plotting import figure
    from bokeh.plotting import ColumnDataSource
    from bokeh.models import FactorRange, Legend
    # from bokeh.transform import dodge, factor_cmap

    # from bokeh.resources import CDN

    today = datetime.datetime.utcnow().replace(
        tzinfo=datetime.timezone.utc, hour=0, minute=0, second=0, microsecond=0)

    waitingtasks = Task.objects.filter(
        timestamp__gt=today - datetime.timedelta(days=14), finishtimestamp__isnull=True)\
        .annotate(queueday=Trunc('timestamp', 'day')) \
        .values('queueday') \
        .annotate(taskcount=Count('id'))

    daywaitingcounts = {
        (today - task['queueday']).total_seconds() // 86400: task['taskcount'] for task in waitingtasks}

    for d in range(14):
        if d not in daywaitingcounts:
            daywaitingcounts[d] = 0.

    arr_queueday = sorted(daywaitingcounts.keys(), reverse=True)

    finishedtasks = Task.objects.filter(
        timestamp__gt=today - datetime.timedelta(days=14), finishtimestamp__isnull=False) \
        .annotate(queueday=Trunc('timestamp', 'day')) \
        .values('queueday') \
        .annotate(taskcount=Count('id'))

    dayfinishedcounts = {
        (today - task['queueday']).total_seconds() // 86400: task['taskcount'] for task in finishedtasks}

    data = {
        'queueday': [(today - datetime.timedelta(days=d)).strftime('%b %d') for d in arr_queueday],
        'waitingtaskcount': [daywaitingcounts.get(d, 0.) for d in arr_queueday],
        'finishedtaskcount': [dayfinishedcounts.get(d, 0.) for d in arr_queueday],
    }

    source = ColumnDataSource(data=data)

    plot = figure(
        x_range=FactorRange(*data['queueday']),
        y_range=DataRange1d(start=0.),
        tools="",
        aspect_ratio=5,
        # title="Waiting and finished tasks",
        sizing_mode='stretch_both',
        output_backend="svg",
        y_axis_label="Tasks per day",
    )

    plot.grid.visible = False

    r = plot.vbar_stack(['waitingtaskcount', 'finishedtaskcount'], x='queueday', source=source, color=['red', 'grey'],
                        line_width=0., width=0.3)

    # plot.legend.orientation = "horizontal"

    legend = Legend(items=[
        ("Finished", [r[1]]),
        ("Waiting", [r[0]]),
    ], location="top", border_line_width=0)

    plot.add_layout(legend, 'right')

    plot.add_tools(HoverTool(
        tooltips=[
            ("Day", "@queueday"),
            ("Finished", "@finishedtaskcount"),
            ("Waiting", "@waitingtaskcount"),
        ], mode="mouse", point_policy="follow_mouse", renderers=r))

    script, strhtml = components(plot)

    return JsonResponse({"script": script, "div": strhtml})


@cache_page(60 * 60 * 4)
def statslongterm(request):
    dictparams = {}
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    thirtydaytasks = Task.objects.filter(timestamp__gt=now - datetime.timedelta(days=30))

    dictparams['thirtydaytasks'] = thirtydaytasks.count()
    dictparams['thirtydaytaskrate'] = '{:.1f}/day'.format(dictparams['thirtydaytasks'] / 30.)

    countrylist = thirtydaytasks.filter(country_code__isnull=False).exclude(country_code='XX').values_list(
        'country_code').annotate(task_count=Count('country_code')).order_by('-task_count', 'country_code')[:15]

    def country_activeusers(country_code):
        return thirtydaytasks.filter(country_code=country_code).values_list('user_id').distinct().count()

    dictparams['countrylist'] = [
        (country_code_to_name(code), task_count, country_activeusers(code)) for code, task_count in countrylist]

    regionlist = thirtydaytasks.filter(country_code__isnull=False).exclude(
        country_code='XX').values_list('country_code', 'region').annotate(
        task_count=Count('country_code')).order_by('-task_count', 'country_code', 'region')[:15]

    dictparams['regionlist'] = [
        (country_region_to_name(country_code, region), task_count) for country_code, region, task_count in regionlist]

    dictparams['thirtyddayusers'] = thirtydaytasks.values_list('user_id').distinct().count()

    return render(request, 'statslongterm.html', dictparams)


@cache_page(60 * 15)
def statsshortterm(request):
    dictparams = {}
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    sevendaytasks = Task.objects.filter(timestamp__gt=now - datetime.timedelta(days=7))
    sevendaytaskcount = int(sevendaytasks.count())

    dictparams['sevendaytasks'] = sevendaytaskcount
    dictparams['sevendayusers'] = sevendaytasks.values_list('user_id').distinct().count()
    dictparams['sevendaytaskrate'] = '{:.1f}/day'.format(dictparams['sevendaytasks'] / 7.)

    dictparams['sevendaympctasks'] = int(sevendaytasks.filter(mpc_name__isnull=False).count())
    dictparams['sevendayimgtasks'] = int(sevendaytasks.filter(request_type='IMGZIP').count())

    sevendaytasks_finished = sevendaytasks.filter(finishtimestamp__isnull=False)

    if sevendaytasks_finished.count() > 0:
        dictparams['sevendayavgwaittime'] = '{:.1f}s'.format(
            np.nanmean(np.array([tsk.waittime() for tsk in sevendaytasks_finished])))

        sevenday_runtimes = np.array([tsk.runtime() for tsk in sevendaytasks_finished])
        sevenday_mean_runtime = np.nanmean(sevenday_runtimes)
        dictparams['sevendayavgruntime'] = '{:.1f}s'.format(sevenday_mean_runtime)
        dictparams['sevendayloadpercent'] = '{:.1f}%'.format(
            100. * sevendaytaskcount * sevenday_mean_runtime / (7 * 24. * 60 * 60))
    else:
        dictparams.update({
            'sevendayavgwaittime': '-',
            'sevendayavgruntime': '-',
            'sevendayloadpercent': '0%',
        })

    return render(request, 'statsshortterm.html', dictparams)


# @cache_page(60 * 5)
def stats(request):
    dictparams = {'name': 'Usage Statistics'}

    dictparams['queuedtaskcount'] = Task.objects.filter(finishtimestamp__isnull=True).count()
    try:
        lastfinishtime = (Task.objects.filter(finishtimestamp__isnull=False)
                          .order_by('finishtimestamp').last().finishtimestamp)
        dictparams['lastfinishtime'] = f"{lastfinishtime:%Y-%m-%d %H:%M:%S %Z}"
    except AttributeError:
        dictparams['lastfinishtime'] = 'N/A'

    return render(request, 'stats.html', dictparams)


def register(request):
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('username')
            raw_password = form.cleaned_data.get('password1')
            user = authenticate(username=username, password=raw_password)
            login(request, user)
            return redirect(reverse('task-list'))
    else:
        form = RegistrationForm()

    return render(request, 'registration/register.html', {'form': form})


@cache_page(60 * 10)
def resultplotdatajs(request, taskid):
    import pandas as pd

    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")
    else:
        return HttpResponseNotFound("Page not found")

    jsplotfile = item.localresultjsplotfile()
    if not jsplotfile:
        return HttpResponseNotFound("Page not found")

    # force occasional refresh
    etag = datetime.datetime.utcnow().strftime("%Y%m%d")
    if 'HTTP_IF_NONE_MATCH' in request.META and etag == request.META['HTTP_IF_NONE_MATCH']:
        return HttpResponseNotModified()

    if not Path(jsplotfile).exists():
        jsout = []
        resultfile = item.localresultfile()
        if resultfile:
            resultfilepath = os.path.join(settings.STATIC_ROOT, resultfile)
            if not os.path.exists(resultfilepath):
                return HttpResponseNotFound("Page not found")

            df = pd.read_csv(resultfilepath, delim_whitespace=True, escapechar='#')
            # df.rename(columns={'#MJD': 'MJD'})
            if df.empty:
                return HttpResponseNotFound("Page not found")

            df.query("uJy > -1e10 and uJy < 1e10", inplace=True)

            jsout.append("var jslcdata = new Array();\n")
            jsout.append("var jslabels = new Array();\n")

            divid = f'plotforcedflux-task-{taskid}'

            for color, filter in [(11, 'c'), (12, 'o')]:
                dffilter = df.query('F == @filter', inplace=False)

                jsout.append(
                    '\njslabels.push({"color": ' + str(color) + ', "display": false, "label": "' + filter + '"});\n')

                jsout.append("jslcdata.push([" + (", ".join([
                    f"[{mjd},{uJy},{duJy}]" for _, (mjd, uJy, duJy) in
                    dffilter[["#MJD", "uJy", "duJy"]].iterrows()])) + "]);\n")

            today = datetime.date.today()
            mjd_today = date_to_mjd(today.year, today.month, today.day)
            xmin = df['#MJD'].min()
            xmax = df['#MJD'].max()
            ymin = max(-200, df.uJy.min())
            ymax = min(40000, df.uJy.max())

            jsout.append('var jslclimits = {')
            jsout.append(f'"xmin": {xmin}, "xmax": {xmax}, "ymin": {ymin}, "ymax": {ymax},')
            jsout.append(f'"discoveryDate": {xmin},')
            jsout.append(f'"today": {mjd_today},')
            jsout.append('};\n')

            jsout.append(f'jslimitsglobal["#{divid}"] = jslclimits;\n')
            jsout.append(f'jslcdataglobal["#{divid}"] = jslcdata;\n')
            jsout.append(f'jslabelsglobal["#{divid}"] = jslabels;\n')

            jsout.append(
                f'var lcdivname = "#{divid}", lcplotheight = 300, markersize = 15, errorbarsize = 4, arrowsize = 7;\n')

            jsout.append(''.join(Path(settings.STATIC_ROOT, 'js/lightcurveplotly.js').open('rt').readlines()))
            # jsout.append((
            #     "$.ajax({url: '" + settings.STATIC_URL + "js/lightcurveplotly.js', "
            #     "cache: true, dataType: 'script'});"))

        # strjs = ''.join(jsout)
        # return HttpResponse(strjs, content_type="text/javascript")

        with jsplotfile.open('w') as f:
            f.writelines(jsout)

    if os.path.exists(jsplotfile):
        return FileResponse(open(jsplotfile, 'rb'), headers={'ETag': etag})

    return HttpResponseNotFound("ERROR: Could not create javascript file.")


def taskpdfplot(request, taskid):
    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")
    else:
        return HttpResponseNotFound("Page not found")

    resultfile = item.localresultfile()
    if resultfile:
        resultfilepath = Path(os.path.join(settings.STATIC_ROOT, resultfile))
        pdfpath = resultfilepath.with_suffix('.pdf')

        # to force a refresh of all plots
        # if os.path.exists(pdfpath):
        #     os.remove(pdfpath)

        if not os.path.exists(pdfpath):
            # matplotlib needs to run in its own process or it will crash
            make_pdf_plot(
                taskid=taskid, localresultfile=resultfilepath, taskcomment=item.comment, separate_process=True)

        if os.path.exists(pdfpath):
            return FileResponse(open(pdfpath, 'rb'))

    return HttpResponseNotFound("ERROR: Could not generate PDF plot (perhaps a lack of data points?)")


def taskresultdata(request, taskid):
    item = None
    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")

    if item:
        resultfile = item.localresultfile()
        if resultfile:
            resultfilepath = Path(os.path.join(settings.STATIC_ROOT, resultfile))

            if os.path.exists(resultfilepath):
                return FileResponse(open(resultfilepath, 'rb'))

    return HttpResponseNotFound("Page not found")


def taskimagezip(request, taskid):
    item = None
    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")

    if item:
        resultfile = item.localresultimagezipfile
        if resultfile:
            resultfilepath = Path(os.path.join(settings.STATIC_ROOT, resultfile))

            if os.path.exists(resultfilepath):
                return FileResponse(open(resultfilepath, 'rb'))

    return HttpResponseNotFound("Page not found")
