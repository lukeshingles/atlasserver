import datetime
import os

import numpy as np
import geoip2.errors

from django.conf import settings as settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Count
from django.http import HttpResponse, FileResponse
# from django.http.response import HttpResponseRedirect
from django.http import HttpResponseNotFound
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
from pathlib import Path

from forcephot.filters import TaskFilter
from forcephot.forms import TaskForm, RegistrationForm
from forcephot.misc import country_code_to_name, country_region_to_name, splitradeclist, date_to_mjd, make_pdf_plot
from forcephot.models import Task
from forcephot.serializers import ForcePhotTaskSerializer


class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    Object-level permission to only allow owners of an object to edit it.
    Assumes the model instance has an `user` attribute.
    """

    message = 'You must be the owner of this object.'

    # def has_permission(self, request, view):
    #     return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed to any request,
        # so we'll always allow GET, HEAD or OPTIONS requests.
        if request.method in permissions.SAFE_METHODS:
            return True

        if request.method in ['PUT', 'PATCH']:  # and obj.started
            return False

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
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrReadOnly]
    throttle_scope = 'forcephottasks'
    ordering_fields = ['timestamp', 'id']
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    filterset_class = TaskFilter
    ordering = '-id'
    # filterset_fields = ['finishtimestamp']
    template_name = 'tasklist.html'

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
                return redirect(reverse('task-list'), status=status.HTTP_201_CREATED, headers=kwargs['headers'])

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
        extra_fields['timestamp'] = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

        extra_fields['country_code'] = self.request.geo_data.country_code
        extra_fields['region'] = self.request.geo_data.region
        extra_fields['city'] = self.request.geo_data.city

        extra_fields['from_api'] = (self.request.accepted_renderer.format != 'html')

        serializer.save(**extra_fields)

    def perform_update(self, serializer):
        serializer.save(user=self.request.user)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        # print(reverse('task-list', request=request))
        # if request.accepted_renderer.format == 'html':
        return Response(status=status.HTTP_303_SEE_OTHER, headers={
                'Location': reverse('task-list', request=request)})
        # return Response(status=status.HTTP_204_NO_CONTENT)

    # def perform_destroy(self, instance):
    #     instance.delete()

    def list(self, request, *args, **kwargs):
        listqueryset = self.filter_queryset(self.get_queryset().filter(is_archived=False, user_id=request.user))

        page = self.paginate_queryset(listqueryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
        else:
            serializer = self.get_serializer(listqueryset, many=True)

        # taskframeonly is for javascript updates (no header/menubar)
        htmltaskframeonly = 'htmltaskframeonly' in request.GET

        if request.accepted_renderer.format == 'html' or htmltaskframeonly:
            if not page and listqueryset:
                return redirect(reverse('task-list'), request=request)

            if 'form' in kwargs:
                form = kwargs['form']
            else:
                form = TaskForm()

            template = 'tasklist-frame.html' if htmltaskframeonly else self.template_name

            return Response(template_name=template, data={
                'serializer': serializer, 'data': serializer.data, 'tasks': page,
                'form': form, 'name': 'Task Queue', 'htmltaskframeonly': htmltaskframeonly, 'singletaskdetail': False,
                'paginator': self.paginator, 'usertaskcount': len(listqueryset)})

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_archived:
            return HttpResponseNotFound("Page not found")
        serializer = self.get_serializer(instance)

        if request.accepted_renderer.format == 'html':
            # return redirect('/')
            # queryset = self.filter_queryset(self.get_queryset())
            # serializer = self.get_serializer(queryset, many=True)

            htmltaskframeonly = 'htmltaskframeonly' in request.GET
            tasks = [instance]
            form = TaskForm()
            return Response({'serializer': serializer, 'data': serializer.data, 'tasks': tasks, 'form': form,
                             'name': f'Task {self.get_object().id}', 'htmltaskframeonly': htmltaskframeonly,
                             'singletaskdetail': True})

        return Response(serializer.data)


def deleteTask(request, pk):
    try:
        item = Task.objects.get(id=pk)
        item.delete()
    except ObjectDoesNotExist:
        pass

    redirurl = request.META.get('HTTP_REFERER', reverse('task-list'))
    return redirect(redirurl, request=request)


def index(request):
    template_name = 'index.html'
    # return redirect('/queue')
    return render(request, template_name)


def resultdesc(request):
    template_name = 'resultdesc.html'
    return render(request, template_name, {'name': 'Output Description'})


def apiguide(request):
    template_name = 'apiguide.html'
    return render(request, template_name, {'name': 'API Guide'})


def faq(request):
    template_name = 'faq.html'
    return render(request, template_name, {'name': 'FAQ'})


@cache_page(60 * 60 * 4)
def statscoordchart(request):

    # now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    # tasks = Task.objects.filter(timestamp__gt=now - datetime.timedelta(days=7))

    tasks = Task.objects.all().order_by('-timestamp')[:10000]

    # from bokeh.io import output_file, show
    # from bokeh.transform import linear_cmap
    # from bokeh.util.hex import hexbin
    from bokeh.embed import components
    from bokeh.models import HoverTool
    from bokeh.plotting import figure
    from bokeh.plotting import ColumnDataSource
    # from bokeh.resources import CDN

    dictsource = {
            'ra': np.array([tsk.ra for tsk in tasks]),
            'dec':  np.array([tsk.dec for tsk in tasks]),
            'taskid':  np.array([tsk.id for tsk in tasks]),
            # 'username': np.array([User.objects.get(id=tsk.user_id).username for tsk in tasks]),
    }
    source = ColumnDataSource(dictsource)

    plot = figure(tools="pan,wheel_zoom,box_zoom,reset",
                  # match_aspect=True,
                  aspect_ratio=2,
                  background_fill_color='#340154',
                  active_scroll="wheel_zoom",
                  title="Recently requested coordinates",
                  x_axis_label="Right ascension (deg)",
                  y_axis_label="Declination (deg)",
                  x_range=(0, 360), y_range=(-90, 90),
                  # frame_width=600,
                  sizing_mode='stretch_both')
    plot.grid.visible = False

    # bins = hexbin(arr_ra, arr_dec, 1.)
    # plot.hex_tile(q="q", r="r", size=0.1, line_color=None, source=bins,
    #               fill_color=linear_cmap('counts', 'Viridis256', 0, max(bins.counts)))
    # r, bins = plot.hexbin(arr_ra, arr_dec, size=.5, hover_color="pink", hover_alpha=0.8)

    r = plot.circle('ra', 'dec', source=source, color="white", size=7.,
                    hover_color="orange", alpha=0.7, hover_alpha=1.0)

    plot.add_tools(HoverTool(
        tooltips=[
            ("RA Dec", "@ra @dec"),
            ("Task", "@taskid"),
            # ("User", "@username"),
        ],
        mode="mouse", point_policy="follow_mouse", renderers=[r]))

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
        return thirtydaytasks.filter(country_code=country_code).values_list('user_id').annotate(
            task_count=Count('user_id')).count()

    dictparams['countrylist'] = [
        (country_code_to_name(code), task_count, country_activeusers(code)) for code, task_count in countrylist]

    regionlist = thirtydaytasks.filter(country_code__isnull=False).exclude(
        country_code='XX').values_list('country_code', 'region').annotate(
        task_count=Count('country_code')).order_by('-task_count', 'country_code', 'region')[:15]

    dictparams['regionlist'] = [
        (country_region_to_name(country_code, region), task_count) for country_code, region, task_count in regionlist]

    dictparams['thirtyddayusers'] = thirtydaytasks.values_list('user_id').annotate(task_count=Count('user_id')).count()

    return render(request, 'statslongterm.html', dictparams)


@cache_page(60 * 15)
def statsshortterm(request):
    dictparams = {}
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    sevendaytasks = Task.objects.filter(timestamp__gt=now - datetime.timedelta(days=7))
    sevendaytaskcount = int(sevendaytasks.count())

    dictparams['sevendaytasks'] = sevendaytaskcount
    dictparams['sevendayusers'] = sevendaytasks.values_list('user_id').annotate(task_count=Count('user_id')).count()
    dictparams['sevendaytaskrate'] = '{:.1f}/day'.format(dictparams['sevendaytasks'] / 7.)
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


def resultplotdatajs(request, taskid):
    import pandas as pd

    if taskid:
        try:
            item = Task.objects.get(id=taskid)
        except ObjectDoesNotExist:
            return HttpResponseNotFound("Page not found")
    else:
        return HttpResponseNotFound("Page not found")

    strjs = ''
    resultfile = item.localresultfile()
    if resultfile:
        df = pd.read_csv(os.path.join(settings.STATIC_ROOT, resultfile), delim_whitespace=True, escapechar='#')
        # df.rename(columns={'#MJD': 'MJD'})
        df.query("uJy > -1e10 and uJy < 1e10", inplace=True)

        strjs += "var jslcdata = new Array();\n"
        strjs += "var jslabels = new Array();\n"

        divid = f'plotforcedflux-task-{taskid}'

        for color, filter in [(11, 'c'), (12, 'o')]:
            dffilter = df.query('F == @filter', inplace=False)

            strjs += '\njslabels.push({"color": ' + str(color) + ', "display": false, "label": "' + filter + '"});\n'

            strjs += "jslcdata.push([" + (", ".join([
                f"[{mjd},{uJy},{duJy}]" for _, (mjd, uJy, duJy) in
                dffilter[["#MJD", "uJy", "duJy"]].iterrows()])) + "]);\n"

        today = datetime.date.today()
        mjd_today = date_to_mjd(today.year, today.month, today.day)
        xmin = df['#MJD'].min()
        xmax = df['#MJD'].max()
        ymin = max(-200, df.uJy.min())
        ymax = min(40000, df.uJy.max())

        strjs += 'var jslclimits = {'
        strjs += f'"xmin": {xmin}, "xmax": {xmax}, "ymin": {ymin}, "ymax": {ymax},'
        strjs += f'"discoveryDate": {xmin},'
        strjs += f'"today": {mjd_today},'
        strjs += '};\n'

        strjs += f'jslimitsglobal["#{divid}"] = jslclimits;\n'
        strjs += f'jslcdataglobal["#{divid}"] = jslcdata;\n'
        strjs += f'jslabelsglobal["#{divid}"] = jslabels;\n'

        strjs += f'var lcdivname = "#{divid}", lcplotheight = 300, markersize = 15, errorbarsize = 4, arrowsize = 7;\n'

        strjs += (
            "$.ajax({url: '" + settings.STATIC_URL + "js/lightcurveplotly.js', cache: true, dataType: 'script'});")

    return HttpResponse(strjs, content_type="text/javascript")


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

    return HttpResponseNotFound("ERROR: Could not generate PDF plot (perhaps no data points?)")


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
