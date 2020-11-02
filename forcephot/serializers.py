from django.contrib.auth.models import User, Group
from rest_framework import serializers
from rest_framework.reverse import reverse
import atlasserver.settings as djangosettings
from .models import Task


# class UserSerializer(serializers.HyperlinkedModelSerializer):
#
#     class Meta:
#         model = User
#         fields = ['url', 'username', 'email', 'groups']
#
#
# class GroupSerializer(serializers.HyperlinkedModelSerializer):
#     class Meta:
#         model = Group
#         fields = ['url', 'name']


class ForcePhotTaskSerializer(serializers.ModelSerializer):
    result_url = serializers.SerializerMethodField('get_result_url')

    def get_result_url(self, obj):
        if obj.get_localresultfile():
            request = self.context.get('request')
            return request.build_absolute_uri(djangosettings.STATIC_URL + obj.get_localresultfile())

        return None

    class Meta:
        model = Task
        fields = ['url', 'id', 'user', 'timestamp', 'ra', 'dec', 'mjd_min', 'mjd_max',
                  'use_reduced', 'finished', 'result_url', 'comment', 'finishtimestamp']
        read_only_fields = ['user', 'timestamp', 'finished', 'result_url', 'finishtimestamp']
