from django.contrib.auth.models import Group, User
from rest_framework import serializers
from rest_framework.reverse import reverse

import atlasserver.settings as djangosettings
from forcephot.models import Task


class ForcePhotTaskSerializer(serializers.ModelSerializer):
    result_url = serializers.SerializerMethodField('get_result_url')

    def get_result_url(self, obj):
        if obj.localresultfile():
            request = self.context.get('request')
            return request.build_absolute_uri(djangosettings.STATIC_URL + obj.localresultfile())

        return None

    class Meta:
        model = Task
        fields = ['url', 'id', 'user', 'timestamp', 'ra', 'dec', 'mjd_min', 'mjd_max',
                  'use_reduced', 'finished', 'result_url', 'comment', 'send_email', 'starttimestamp', 'finishtimestamp']
        read_only_fields = ['user', 'timestamp', 'finished', 'result_url', 'starttimestamp', 'finishtimestamp']
