from django.contrib.auth.models import User, Group
from rest_framework import serializers
from .models import ForcePhotTask


class UserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = ['url', 'username', 'email', 'groups']


class GroupSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Group
        fields = ['url', 'name']


class ForcePhotTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = ForcePhotTask
        fields = ['url', 'id', 'userid', 'timestamp', 'ra', 'dec', 'mjd_min', 'mjd_max', 'finished']
        read_only_fields = ['userid', 'timestamp', 'finished']

