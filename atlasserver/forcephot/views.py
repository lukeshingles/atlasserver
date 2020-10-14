import os

from django.shortcuts import render

from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework import permissions
from rest_framework import filters
from .serializers import *
from .models import *


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
