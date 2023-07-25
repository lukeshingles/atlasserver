# from django.contrib.auth.models import User
import math

from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from atlasserver.forcephot.models import Task


def is_finite_float(val):
    if val is None:
        return False
    try:
        f_val = float(val)
    except ValueError:
        return False

    return bool(math.isfinite(f_val))


class ForcePhotTaskSerializer(serializers.ModelSerializer):
    def get_result_url(self, obj):
        if obj.localresultfile() and not obj.error_msg:
            request = self.context.get("request")
            return request.build_absolute_uri(staticfiles_storage.url(obj.localresultfile()))
            # return request.build_absolute_uri(reverse("taskresultdata", args=[obj.id]))

        return None

    def get_parent_task_url(self, obj):
        if obj.parent_task_id:
            try:
                parent = Task.objects.get(id=obj.parent_task_id)
                if parent.is_archived:
                    return None
            except ObjectDoesNotExist:
                return None
            request = self.context.get("request")
            return request.build_absolute_uri(reverse("task-detail", args=[obj.parent_task_id]))

        return None

    def get_pdfplot_url(self, obj):
        if obj.localresultfile() and not obj.error_msg:
            request = self.context.get("request")
            return request.build_absolute_uri(reverse("taskpdfplot", args=[obj.id]))

        return None

    def get_previewimage_url(self, obj):
        if obj.localresultpreviewimagefile:
            request = self.context.get("request")
            return request.build_absolute_uri(staticfiles_storage.url(obj.localresultpreviewimagefile))
            # return request.build_absolute_uri(reverse("taskpreviewimage", args=[obj.id]))

        return None

    def get_imagerequest_url(self, obj):
        if obj.imagerequest_task_id:
            request = self.context.get("request")
            return request.build_absolute_uri(reverse("task-detail", args=[obj.imagerequest_task_id]))

        return None

    def get_result_imagezip_url(self, obj):
        if obj.localresultimagezipfile:
            request = self.context.get("request")
            return request.build_absolute_uri(staticfiles_storage.url(obj.localresultimagezipfile))

        return None

    result_url = serializers.SerializerMethodField("get_result_url")
    parent_task_url = serializers.SerializerMethodField("get_parent_task_url")
    pdfplot_url = serializers.SerializerMethodField("get_pdfplot_url")
    previewimage_url = serializers.SerializerMethodField("get_previewimage_url")
    imagerequest_url = serializers.SerializerMethodField("get_imagerequest_url")
    result_imagezip_url = serializers.SerializerMethodField("get_result_imagezip_url")

    def validate_mpc_name(self, value, prefix="", field="mpc_name"):
        # okchars = "0123456789 abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        # if any([c not in dict.fromkeys(okchars) for c in value]):
        #     raise serializers.ValidationError('Invalid an mpc_name. May contain only: 0-9a-z[space]')

        badchars = "'\";"
        if any(c in dict.fromkeys(badchars) for c in value):
            raise serializers.ValidationError(
                {field: f"{prefix}Invalid mpc_name. May not contain quotes or seimicolons"}
            )

        return value

    def validate_ra(self, value, prefix="", field="ra"):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError({field: f"{prefix}ra must be a finite floating-point number."})

        return value

    def validate_dec(self, value, prefix="", field="dec"):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError({field: f"{prefix}dec must be a finite floating-point number."})

        return value

    def validate_mjd_min(self, value):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError(
                {"mjd_min": "mjd_min must be either None or a finite floating-point number."}
            )

        return value

    def validate_mjd_max(self, value):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError(
                {"mjd_max": "mjd_max must be either None or a finite floating-point number."}
            )

        return value

    def validate(self, attrs):
        if attrs.get("mpc_name", False):
            if attrs.get("ra", False) or attrs.get("dec", False):
                raise serializers.ValidationError({"mpc_name": "mpc_name was given but RA and Dec were not empty."})
        elif "ra" not in attrs and "dec" not in attrs:
            msg = "Either an mpc_name or (ra, dec) must be specified."
            raise serializers.ValidationError(msg)
        elif "dec" not in attrs:
            raise serializers.ValidationError({"dec": "ra was set but dec is missing."})
        elif "ra" not in attrs:
            raise serializers.ValidationError({"ra": "dec was set but ra is missing."})

        if "mjd_min" in attrs and attrs["mjd_min"] is not None and not is_finite_float(attrs["mjd_min"]):
            raise serializers.ValidationError(
                {"mjd_min": "mjd_min must be either None or a finite floating-point number."}
            )

        if (
            "mjd_max" in attrs
            and attrs["mjd_max"] is not None
            and ("mjd_min" in attrs and attrs["mjd_min"] is not None and not attrs["mjd_max"] > attrs["mjd_min"])
        ):
            raise serializers.ValidationError({"mjd_max": "mjd_max must be greater than mjd_min."})

        return attrs

    class Meta:
        model = Task

        fields = [
            "url",
            "id",
            "user_id",
            "username",
            "timestamp",
            "mpc_name",
            "ra",
            "dec",
            "mjd_min",
            "mjd_max",
            "radec_epoch_year",
            "propermotion_ra",
            "propermotion_dec",
            "use_reduced",
            "finished",
            "result_url",
            "comment",
            "send_email",
            "starttimestamp",
            "finishtimestamp",
            "error_msg",
            "previewimage_url",
            "parent_task_id",
            "parent_task_url",
            "request_type",
            "pdfplot_url",
            "queuepos",
            "imagerequest_task_id",
            "imagerequest_url",
            "imagerequest_finished",
            "result_imagezip_url",
        ]

        read_only_fields = [
            "user_id",
            "username",
            "timestamp",
            "finished",
            "result_url",
            "starttimestamp",
            "finishtimestamp",
            "error_msg",
            "parent_task_url",
            "previewimage_url",
            "pdfplot_url",
            "queuepos",
            "imagerequest_task_id",
            "imagerequest_url",
            "imagerequest_finished",
            "result_imagezip_url",
        ]
