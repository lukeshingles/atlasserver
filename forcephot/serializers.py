# from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse
from django.conf import settings

from forcephot.models import Task

import math


def is_finite_float(val):
    if val is None:
        return False
    try:
        f_val = float(val)
    except ValueError:
        return False

    if math.isnan(f_val):
        return False

    return True


class ForcePhotTaskSerializer(serializers.ModelSerializer):

    def get_result_url(self, obj):
        if obj.localresultfile() and not obj.error_msg:
            request = self.context.get('request')
            # return request.build_absolute_uri(settings.STATIC_URL + obj.localresultfile())
            return request.build_absolute_uri(reverse('taskresultdata', args=[obj.id]))

        return None

    def get_parent_task_url(self, obj):
        if obj.parent_task_id:
            try:
                parent = Task.objects.get(id=obj.parent_task_id)
                if parent.is_archived:
                    return None
            except ObjectDoesNotExist:
                return None
            request = self.context.get('request')
            return request.build_absolute_uri(reverse('task-detail', args=[obj.parent_task_id]))

        return None

    def get_pdfplot_url(self, obj):
        if obj.localresultfile() and not obj.error_msg:
            request = self.context.get('request')
            return request.build_absolute_uri(reverse('taskpdfplot', args=[obj.id]))

        return None

    def get_previewimage_url(self, obj):
        if obj.localresultpreviewimagefile:
            request = self.context.get('request')
            return request.build_absolute_uri(settings.STATIC_URL + obj.localresultpreviewimagefile)
            # return request.build_absolute_uri(reverse('taskpreviewimage', args=[obj.id]))

        return None

    def get_imagerequest_url(self, obj):
        if obj.imagerequest_task_id:
            request = self.context.get('request')
            return request.build_absolute_uri(reverse('task-detail', args=[obj.imagerequest_task_id]))

        return None

    def get_result_imagezip_url(self, obj):
        if obj.localresultimagezipfile:
            request = self.context.get('request')
            return request.build_absolute_uri(settings.STATIC_URL + str(obj.localresultimagezipfile))

        return None

    result_url = serializers.SerializerMethodField('get_result_url')
    parent_task_url = serializers.SerializerMethodField('get_parent_task_url')
    pdfplot_url = serializers.SerializerMethodField('get_pdfplot_url')
    previewimage_url = serializers.SerializerMethodField('get_previewimage_url')
    imagerequest_url = serializers.SerializerMethodField('get_imagerequest_url')
    result_imagezip_url = serializers.SerializerMethodField('get_result_imagezip_url')

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
            if not 'ra' in attrs and not 'dec' in attrs:
                raise serializers.ValidationError('Either an mpc_name or (ra, dec) must be specified')
            elif not 'dec' in attrs:
                raise serializers.ValidationError({'dec': 'RA given but Dec is missing'})
            elif not 'ra' in attrs:
                raise serializers.ValidationError({'ra': 'Dec given but RA is missing'})

            if not is_finite_float(attrs.get('ra', 0.)):
                raise serializers.ValidationError({'ra': 'ra must be a finite floating-point number'})

            if not is_finite_float(attrs.get('dec', 0.)):
                raise serializers.ValidationError({'dec': 'dec must be a finite floating-point number'})

        return attrs

    class Meta:
        model = Task

        fields = [
            'url', 'id', 'user_id', 'username', 'timestamp', 'mpc_name', 'ra', 'dec', 'mjd_min', 'mjd_max',
            'radec_epoch_year', 'propermotion_ra', 'propermotion_dec', 'use_reduced',
            'finished', 'result_url', 'comment', 'send_email', 'starttimestamp',
            'finishtimestamp', 'error_msg', 'previewimage_url', 'parent_task_id', 'parent_task_url', 'request_type',
            'pdfplot_url', 'queuepos',
            'imagerequest_task_id', 'imagerequest_url', 'imagerequest_finished', 'result_imagezip_url']

        read_only_fields = [
            'user_id', 'username', 'timestamp', 'finished', 'result_url', 'starttimestamp', 'finishtimestamp', 'error_msg',
            'parent_task_url', 'previewimage_url', 'pdfplot_url', 'queuepos',
            'imagerequest_task_id', 'imagerequest_url', 'imagerequest_finished', 'result_imagezip_url']
