# from django.contrib.auth.models import Group, User
from rest_framework import serializers
from rest_framework.reverse import reverse
from django.conf import settings

from forcephot.models import Task


class ForcePhotTaskSerializer(serializers.ModelSerializer):

    def get_result_url(self, obj):
        if obj.localresultfile() and not obj.error_msg:
            request = self.context.get('request')
            # return request.build_absolute_uri(settings.STATIC_URL + obj.localresultfile())
            return request.build_absolute_uri(reverse('taskresultdata', args=[obj.id]))

        return None

    def get_parent_task_url(self, obj):
        if obj.parent_task_id:
            request = self.context.get('request')
            return request.build_absolute_uri(reverse('task-detail', args=[obj.parent_task_id]))

        return None

    def get_pdfplot_url(self, obj):
        if obj.localresultfile() and not obj.error_msg:
            request = self.context.get('request')
            return request.build_absolute_uri(reverse('taskpdfplot', args=[obj.id]))

        return None

    def get_previewimage_url(self, obj):
        if obj.localresultfile():
            request = self.context.get('request')
            return request.build_absolute_uri(settings.STATIC_URL + obj.localresultpreviewimagefile)
            # return request.build_absolute_uri(reverse('taskpreviewimage', args=[obj.id]))

        return None

    result_url = serializers.SerializerMethodField('get_result_url')
    parent_task_url = serializers.SerializerMethodField('get_parent_task_url')
    pdfplot_url = serializers.SerializerMethodField('get_pdfplot_url')
    previewimage_url = serializers.SerializerMethodField('get_previewimage_url')

    def validate(self, attrs):
        # print(attrs)
        # raise serializers.ValidationError('This field must be an even number.')
        if attrs.get('mpc_name', False):
            mpc_name = attrs['mpc_name']
            # okchars = "0123456789 abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            # if any([c not in dict.fromkeys(okchars) for c in mpc_name]):
            #     raise serializers.ValidationError({'mpc_name': f'Invalid an mpc_name. May contain only: 0-9a-z[space]'})
            badchars = "'\";"
            if any([c in dict.fromkeys(badchars) for c in mpc_name]):
                raise serializers.ValidationError(
                    {'mpc_name': 'Invalid an mpc_name. May not contain quotes or seimicolons'})

            if attrs.get('ra', False) or attrs.get('dec', False):
                raise serializers.ValidationError({'mpc_name': 'mpc_name was given but RA and Dec were not empty.'})
        else:
            if not attrs.get('ra', False) and not attrs.get('dec', False):
                raise serializers.ValidationError('Either an mpc_name or (ra, dec) must be specified')
            elif not attrs.get('dec', False):
                raise serializers.ValidationError({'dec': 'RA given but Dec is missing'})
            elif not attrs.get('ra', False):
                raise serializers.ValidationError({'ra': 'Dec given but RA is missing'})
        return attrs

    class Meta:
        model = Task

        fields = [
            'url', 'id', 'user_id', 'timestamp', 'mpc_name', 'ra', 'dec', 'mjd_min', 'mjd_max',
            'radec_epoch_year', 'propermotion_ra', 'propermotion_dec', 'use_reduced',
            'finished', 'result_url', 'comment', 'send_email', 'starttimestamp',
            'finishtimestamp', 'error_msg', 'previewimage_url', 'parent_task_id', 'parent_task_url', 'request_type',
            'pdfplot_url', 'queuepos', 'imagerequest_taskid', 'imagerequest_finished']

        read_only_fields = [
            'user_id', 'timestamp', 'finished', 'result_url', 'starttimestamp', 'finishtimestamp', 'error_msg',
            'parent_task_url', 'previewimage_url', 'pdfplot_url', 'queuepos', 'imagerequest_taskid', 'imagerequest_finished']
