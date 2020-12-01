import datetime
import os

from django.contrib.auth import authenticate, login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import HttpResponse
from django.http.response import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, status, viewsets
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

import atlasserver.settings as djangosettings
from forcephot.forms import *
from forcephot.misc import splitradeclist
from forcephot.models import *
from forcephot.serializers import *


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

        # Instance owner must match current user
        return request.user and request.user.is_authenticated and (obj.user.id == request.user.id)
        # return obj.user == request.user


class ForcePhotTaskViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows force.sh tasks to be created and deleted.
    """
    queryset = Task.objects.all().order_by('-timestamp').select_related('user')
    serializer_class = ForcePhotTaskSerializer
    # permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrReadOnly]
    throttle_scope = 'forcephottasks'
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    ordering_fields = ['timestamp', 'id']
    ordering = ['-timestamp']
    filterset_fields = ['user', 'finished']
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
        if instance.get_localresultfile():
            localresultfile = os.path.join(djangosettings.STATIC_ROOT, instance.get_localresultfile())
            if localresultfile and os.path.exists(localresultfile):
                os.remove(localresultfile)
        instance.delete()

    def list(self, request, *args, **kwargs):
        if request.accepted_renderer.format == 'html':
            listqueryset = self.get_queryset().filter(user_id=request.user)
            serializer = self.get_serializer(listqueryset, many=True)
            tasks = listqueryset
            # serializer2 = ForcePhotTaskSerializer(tasks, context={'request': request})
            if 'form' in kwargs:
                form = kwargs['form']
            else:
                form = TaskForm()
            return Response({'serializer': serializer, 'data': serializer.data, 'tasks': tasks, 'form': form})

        # listqueryset = self.filter_queryset(self.get_queryset())
        listqueryset = self.filter_queryset(self.get_queryset().filter(user_id=request.user))
        serializer = self.get_serializer(listqueryset, many=True)
        page = self.paginate_queryset(listqueryset)
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

            tasks = [instance]
            form = TaskForm()
            return Response({'serializer': serializer, 'data': serializer.data, 'tasks': tasks, 'form': form})

        return Response(serializer.data)


def deleteTask(request, pk):
    try:
        item = Task.objects.get(id=pk)
        if item.get_localresultfile():
            localresultfullpath = os.path.join(djangosettings.STATIC_ROOT, item.get_localresultfile())
            if localresultfullpath and os.path.exists(localresultfullpath):
                os.remove(localresultfullpath)

        item.delete()
    except ObjectDoesNotExist:
        pass
    return redirect(reverse('task-list'))


def index(request):
    template_name = 'index.html'
    # return redirect('/queue')
    return render(request, template_name)


def resultdesc(request):
    template_name = 'resultdesc.html'
    return render(request, template_name)


def apiguide(request):
    template_name = 'apiguide.html'
    return render(request, template_name)


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

    return render(request, 'register.html', {'form': form})

