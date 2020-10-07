import os

from django.shortcuts import render

from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from rest_framework import viewsets
from rest_framework import permissions
from atlasserver.forcephot.serializers import *
from atlasserver.forcephot.models import *


class UserViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows users to be viewed or edited.
    """
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]


class GroupViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows groups to be viewed or edited.
    """
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    permission_classes = [permissions.IsAuthenticated]


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

        # Instance owner must match current user
        # return request.user and request.user.is_authenticated and (obj.user == request.user)
        return obj.user == request.user


class ForcePhotTaskViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows force.sh tasks to be viewed or edited.
    """
    queryset = Tasks.objects.all()
    serializer_class = ForcePhotTaskSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    throttle_scope = 'forcephottasks'

    def perform_create(self, serializer):
        if self.request.user and self.request.user.is_authenticated:
            usertasks = Tasks.objects.filter(user_id=self.request.user, finished=False)
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

    # def get(self, request):
    #         year = now().year
    #         data['result_url'] = 'ok'
    #         # data = {
    #         #     ...
    #         #     'year-summary-url': reverse('year-summary', args=[year], request=request)
    #         # }
    #         return Response(data)