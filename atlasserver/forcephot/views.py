import os

from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from django.http.response import HttpResponseRedirect

from rest_framework import filters
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import *
from .models import *
from .forms import *


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
    queryset = Task.objects.all().order_by('-timestamp')
    serializer_class = ForcePhotTaskSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    throttle_scope = 'forcephottasks'
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    ordering_fields = ['timestamp', 'id']
    ordering = ['-timestamp']
    filterset_fields = ['user', 'finished']
    template_name = 'tasklist.html'

    def create(self, request, *args, **kwargs):

        if 'radeclist' not in request.data:
            datalist = [request.data]
        else:
            # multi-add functionality with a list of RA,DEC coords
            firstrow = request.data
            datalist = []
            if 'ra' in firstrow and firstrow['ra'] and 'dec' in firstrow and firstrow['dec']:
                datalist.append(firstrow)

            for line in firstrow['radeclist'].split('\n'):
                row = line.replace(',', ' ').split()
                if len(row) >= 2:
                    newrow = firstrow.copy()
                    newrow['ra'] = row[0]
                    newrow['dec'] = row[1]
                    newrow['radeclist'] = ['']
                    datalist.append(newrow)

        serializer = self.get_serializer(data=datalist, many=True)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        # return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
        return redirect('/', status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        # if self.request.user and self.request.user.is_authenticated:
        #     usertasks = Task.objects.filter(user_id=self.request.user, finished=False)
        #     usertaskcount = usertasks.count()
        #     if (usertaskcount > 10):
        #         raise ValidationError(f'You have too many queued tasks ({usertaskcount}).')
        #     serializer.save(user=self.request.user)
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        serializer.save(user=self.request.user)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        print(reverse('task-list', request=request))
        # if request.accepted_renderer.format == 'html':
        return Response(status=status.HTTP_303_SEE_OTHER, headers={
                'Location': reverse('task-list', request=request)})
        # return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance):
        if instance.get_localresultfile():
            localresultfile = os.path.join('atlasserver', 'forcephot', instance.get_localresultfile())
            if localresultfile and os.path.exists(localresultfile):
                os.remove(localresultfile)
        instance.delete()

    def list(self, request, *args, **kwargs):
        # listqueryset = self.filter_queryset(self.get_queryset()).filter(user_id=request.user)

        if request.accepted_renderer.format == 'html':
            listqueryset = self.get_queryset()
            serializer = self.get_serializer(listqueryset, many=True)
            tasks = listqueryset
            # serializer2 = ForcePhotTaskSerializer(tasks, context={'request': request})
            form = TaskForm()
            return Response({'serializer': serializer, 'data': serializer.data, 'tasks': tasks, 'form': form})

        listqueryset = self.filter_queryset(self.get_queryset())
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
    item = Task.objects.get(id=pk)

    item.delete()
    return redirect('/')

    # context = {'item': item}
    # return render(request, 'tasks/delete.html', context)
    #     return Response(serializer.data)
