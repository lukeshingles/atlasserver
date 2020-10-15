import os

from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend

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
    queryset = Task.objects.all()
    serializer_class = ForcePhotTaskSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    throttle_scope = 'forcephottasks'
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    ordering_fields = ['timestamp', 'id']
    ordering = ['-timestamp']
    filterset_fields = ['user', 'finished']

    def perform_create(self, serializer):
        if self.request.user and self.request.user.is_authenticated:
            usertasks = Task.objects.filter(user_id=self.request.user, finished=False)
            usertaskcount = usertasks.count()
            if (usertaskcount > 10):
                raise ValidationError(f'You have too many queued tasks ({usertaskcount}).')
            serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance):
        localresultfile = os.path.join('atlasserver', 'forcephot', instance.get_localresultfile())
        if localresultfile and os.path.exists(localresultfile):
            os.remove(localresultfile)
        instance.delete()


class index(APIView):
    queryset = Task.objects.all().order_by('-timestamp')
    serializer_class = ForcePhotTaskSerializer
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'tasklist.html'

    # def get(self, request, pk):
    #     profile = get_object_or_404(Task, pk=pk)
    #     serializer = ForcePhotTaskSerializer(profile)
    #     return Response({'serializer': serializer, 'profile': profile})
    #
    # def post(self, request, pk):
    #     profile = get_object_or_404(Task, pk=pk)
    #     serializer = ForcePhotTaskSerializer(profile, data=request.data)
    #     if not serializer.is_valid():
    #         return Response({'serializer': serializer, 'profile': profile})
    #     serializer.save()
    #     return redirect('profile-list')

    def get(self, request, format=None):
        tasks = Task.objects.all().order_by('-timestamp')
        serializer = ForcePhotTaskSerializer(tasks, context={'request': request})
        form = TaskForm()
        return Response({'serializer': serializer, 'tasks': tasks, 'form': form})

    def post(self, request, format=None):
        serializer = ForcePhotTaskSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(user=self.request.user)
            # return Response(request.data, status=status.HTTP_201_CREATED)
            return redirect('/', status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def deleteTask(request, pk):
    item = Task.objects.get(id=pk)

    item.delete()
    return redirect('/')

    # if request.method == 'POST':
    #     item.delete()
    #     return redirect('/')

    # context = {'item': item}
    # return render(request, 'tasks/delete.html', context)
