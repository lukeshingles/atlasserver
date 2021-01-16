import datetime
import os

from django.contrib.auth import authenticate, login
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import HttpResponse, FileResponse
# from django.http.response import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django_filters.rest_framework import DjangoFilterBackend
from django.http import HttpResponseNotFound
from rest_framework import filters, permissions, status, viewsets
# from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.reverse import reverse
from django.conf import settings as settings
from pathlib import Path

from forcephot.filters import TaskFilter
from forcephot.forms import TaskForm, RegistrationForm
from forcephot.misc import splitradeclist, date_to_mjd, make_pdf_plot
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
        # if not kwargs['form'].is_valid():
        #     return self.list(request, *args, **kwargs)
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
        timestampnow = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        serializer.save(user=self.request.user, timestamp=timestampnow)

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

    def perform_destroy(self, instance):
        if instance.localresultfile():
            localresultfullpath = os.path.join(settings.STATIC_ROOT, instance.localresultfile())
            if os.path.exists(localresultfullpath):
                os.remove(localresultfullpath)
            pdfpath = Path(localresultfullpath).with_suffix('.pdf')
            if os.path.exists(pdfpath):
                os.remove(pdfpath)
        instance.delete()

    def list(self, request, *args, **kwargs):
        listqueryset = self.filter_queryset(self.get_queryset().filter(user_id=request.user))

        page = self.paginate_queryset(listqueryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
        else:
            serializer = self.get_serializer(listqueryset, many=True)

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
                'form': form, 'name': 'Job Queue', 'htmltaskframeonly': htmltaskframeonly, 'singletaskdetail': False,
                'paginator': self.paginator, 'usertaskcount': len(listqueryset)})

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
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
        if item.localresultfile():
            localresultfullpath = os.path.join(settings.STATIC_ROOT, item.localresultfile())
            if os.path.exists(localresultfullpath):
                os.remove(localresultfullpath)
            pdfpath = Path(localresultfullpath).with_suffix('.pdf')
            if os.path.exists(pdfpath):
                os.remove(pdfpath)

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


def resultdatajs(request, taskid):
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
