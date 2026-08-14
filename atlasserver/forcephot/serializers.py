import math
import typing as t
from typing import override

from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from atlasserver.forcephot.models import get_mjd_min_default
from atlasserver.forcephot.models import MPC_NAME_WHITESPACE
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.models import UNSET
from atlasserver.forcephot.webhooks import CallbackUrlError
from atlasserver.forcephot.webhooks import validate_callback_url


def is_finite_float(val):
    if val is None:
        return False
    try:
        f_val = float(val)
    except (TypeError, ValueError):
        # TypeError as well as ValueError: a JSON body can put a list or an object where a number
        # belongs, and float() raises TypeError for those, which would surface as a 500
        return False

    return bool(math.isfinite(f_val))


class ForcePhotTaskSerializer(serializers.ModelSerializer[Task]):
    # memoised queue offset. 0 is a normal value, so it cannot double as "not computed yet".
    _min_queuepos_cache: t.Any = UNSET

    def get_result_url(self, obj) -> str | None:
        localresultfile = obj.localresultfile()
        if localresultfile and not obj.error_msg and (request := self.context.get("request")):
            return request.build_absolute_uri(staticfiles_storage.url(localresultfile))

        return None

    def get_parent_task_url(self, obj) -> str | None:
        if obj.parent_task_id and (request := self.context.get("request")):
            try:
                # select_related("parent_task") on the viewset queryset means this is already
                # loaded; the attribute access only falls back to a query for unprefetched callers
                parent = obj.parent_task
            except ObjectDoesNotExist:
                return None
            if parent is None or parent.is_archived:
                return None
            return request.build_absolute_uri(reverse("task-detail", args=[obj.parent_task_id]))

        return None

    def get_pdfplot_url(self, obj) -> str | None:
        if obj.localresultfile() and not obj.error_msg and (request := self.context.get("request")):
            return request.build_absolute_uri(reverse("taskpdfplot", args=[obj.id]))

        return None

    def get_previewimage_url(self, obj) -> str | None:
        if obj.localresultpreviewimagefile and (request := self.context.get("request")):
            return request.build_absolute_uri(staticfiles_storage.url(obj.localresultpreviewimagefile))

        return None

    def get_imagerequest_url(self, obj) -> str | None:
        if obj.imagerequest_task_id and (request := self.context.get("request")):
            return request.build_absolute_uri(reverse("task-detail", args=[obj.imagerequest_task_id]))

        return None

    # the finishtimestamp checks below are not redundant with the file existence checks: an
    # unfinished task cannot have produced a result yet, so testing the timestamp first skips a
    # filesystem stat for every queued task in the page
    def get_result_imagezip_url(self, obj) -> str | None:
        if obj.finishtimestamp and obj.localresultimagezipfile and (request := self.context.get("request")):
            return request.build_absolute_uri(staticfiles_storage.url(obj.localresultimagezipfile))

        return None

    def get_result_imagestack_url(self, obj) -> str | None:
        if obj.finishtimestamp and obj.localresultimagestackfile and (request := self.context.get("request")):
            return request.build_absolute_uri(staticfiles_storage.url(obj.localresultimagestackfile))

        return None

    @property
    def min_queuepos_relative(self) -> int:
        """Return the queue offset, computed once per serializer rather than once per task.

        Task.min_queuepos_relative() is a global aggregate that does not depend on the task being
        serialised. Under many=True this serializer instance is reused for every task in the page,
        so memoising here turns N aggregate queries into one.
        """
        if self._min_queuepos_cache is UNSET:
            self._min_queuepos_cache = Task.min_queuepos_relative()

        return self._min_queuepos_cache

    def get_queuepos(self, obj) -> int | None:
        if obj.finishtimestamp or obj.queuepos_relative is None:
            return None

        return obj.queuepos_relative - self.min_queuepos_relative

    @staticmethod
    def _rounded_seconds(value: float | None) -> float | None:
        """Round a second count for display, or pass through the None that means "not yet".

        Floored at zero: the runner writes starttimestamp truncated to the whole second while
        timestamp keeps its microseconds, so a task dispatched the moment it arrives computes a
        wait a fraction of a second below zero. That is a clock artefact rather than a measurement,
        and it reads as one.
        """
        return None if value is None else round(max(0.0, value), 1)

    def get_waittime(self, obj) -> float | None:
        """Seconds the task spent in the queue before it started."""
        return self._rounded_seconds(obj.waittime())

    def get_runtime(self, obj) -> float | None:
        """Seconds the task took to run."""
        return self._rounded_seconds(obj.runtime())

    queuepos = serializers.SerializerMethodField("get_queuepos")
    # declared rather than left to ModelSerializer, which would resolve these two model methods to
    # a plain ReadOnlyField and render their full float precision
    waittime = serializers.SerializerMethodField("get_waittime")
    runtime = serializers.SerializerMethodField("get_runtime")
    result_url = serializers.SerializerMethodField("get_result_url")
    parent_task_url = serializers.SerializerMethodField("get_parent_task_url")
    pdfplot_url = serializers.SerializerMethodField("get_pdfplot_url")
    previewimage_url = serializers.SerializerMethodField("get_previewimage_url")
    imagerequest_url = serializers.SerializerMethodField("get_imagerequest_url")
    result_imagezip_url = serializers.SerializerMethodField("get_result_imagezip_url")
    result_imagestack_url = serializers.SerializerMethodField("get_result_imagestack_url")

    @staticmethod
    def validate_callback_url(value):
        if value in (None, ""):
            return None

        try:
            return validate_callback_url(value)
        except CallbackUrlError as ex:
            raise serializers.ValidationError({"callback_url": str(ex)}) from ex

    @staticmethod
    def validate_mpc_name(value, prefix="", field="mpc_name"):
        # rejects the characters that would break the shell command the runner builds, rather than
        # allowing a fixed alphabet: MPC designations contain punctuation this cannot predict
        #
        # mpc_name is nullable, and DRF calls validate_<field> even when the value is None
        if value is None:
            return value

        # MPC_NAME_WHITESPACE, not str.strip(): the model, both constraints and both migrations
        # use that set, and stripping more here would make the API call an em-space blank while
        # the database still called it a name. Task.save() does this too; doing it here as well is
        # what turns a whitespace-only name into a 400 rather than an unexplained 500.
        value = value.strip(MPC_NAME_WHITESPACE)
        if value == "":
            return value

        badchars = "'\";"
        if any(c in dict.fromkeys(badchars) for c in value):
            raise serializers.ValidationError(
                {field: f"{prefix}Invalid mpc_name. May not contain quotes or semicolons"}
            )

        return value

    @staticmethod
    def validate_ra(value, prefix="", field="ra"):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError({field: f"{prefix}ra must be a finite floating-point number."})

        return value

    @staticmethod
    def validate_dec(value, prefix="", field="dec"):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError({field: f"{prefix}dec must be a finite floating-point number."})

        return value

    @staticmethod
    def validate_mjd_min(value):
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

    def submitted(self, attrs, field, default=None):
        """Return the value of a field, falling back to the stored task for a partial update.

        Without the fallback, a PATCH is judged only on the fields it changes, so changing
        (say) just the comment of an existing task is rejected for having no target.
        """
        if field in attrs:
            return attrs[field]

        if self.partial and self.instance is not None:
            return getattr(self.instance, field, default)

        return default

    @override
    def validate(self, attrs):
        mpc_name = self.submitted(attrs, "mpc_name")
        request_type = self.submitted(attrs, "request_type")

        # RA 0 and Dec 0 are valid coordinates, so test for "not given" rather than falsiness
        ra_missing = self.submitted(attrs, "ra") in (None, "")
        dec_missing = self.submitted(attrs, "dec") in (None, "")

        if mpc_name:  # it's an MPC object name
            if not ra_missing or not dec_missing:
                raise serializers.ValidationError({"mpc_name": "mpc_name was given but RA and Dec were not empty."})
            if request_type == "SSOSTACK" and ("propermotion_ra" in attrs or "propermotion_dec" in attrs):
                msg = "Proper motion cannot be used for SSO image stack requests."
                raise serializers.ValidationError(msg)
        elif request_type == "SSOSTACK":
            msg = "Image stacking only works on MPC objects."
            raise serializers.ValidationError(msg)
        else:
            if request_type == "IMGZIP":
                parent_task_id = self.submitted(attrs, "parent_task_id")
                if not parent_task_id:
                    msg = "IMGZIP requests must have a parent_task_id set to an FP task."
                    raise serializers.ValidationError(msg)

                try:
                    Task.objects.all().get(id=parent_task_id, request_type="FP")
                except (ObjectDoesNotExist, IndexError):
                    msg = "IMGZIP requests must have a parent_task_id set to an FP task id."
                    raise serializers.ValidationError(msg) from None

            # The target rules, which apply to an IMGZIP task as much as to any other: an image
            # request carries the parent's own coordinates (see Task.new_imagerequest) and the
            # runner dispatches on them, and task_target_is_mpcname_or_radec requires a target of
            # every row. Checking here is what makes a request that clears ra and dec a 400 rather
            # than an IntegrityError raised by the constraint.
            if ra_missing and dec_missing:
                msg = "Either an mpc_name or (ra, dec) must be specified."
                raise serializers.ValidationError({"non_field_errors": msg})
            if dec_missing:
                raise serializers.ValidationError({"dec": "ra was set but dec is missing."})
            if ra_missing:
                raise serializers.ValidationError({"ra": "dec was set but ra is missing."})

        if "mjd_min" in attrs and attrs["mjd_min"] is not None and not is_finite_float(attrs["mjd_min"]):
            raise serializers.ValidationError(
                {"mjd_min": "mjd_min must be either None or a finite floating-point number."}
            )

        mjd_max = self.submitted(attrs, "mjd_max")
        if mjd_max not in (None, ""):
            # a zero or negative upper bound cannot select any observation, and the task runner
            # treats a falsy mjd_max as "not given", which would silently widen the request to
            # the whole archive rather than narrowing it
            if float(mjd_max) <= 0:
                raise serializers.ValidationError({"mjd_max": "mjd_max must be a positive MJD."})

            # an explicit mjd_min of None means "no lower bound" and is saved as-is, so only
            # substitute the model default (30 days ago) when mjd_min was not given at all
            mjd_min = self.submitted(attrs, "mjd_min", default=get_mjd_min_default())
            if mjd_min not in (None, "") and not float(mjd_max) > float(mjd_min):
                raise serializers.ValidationError(
                    {"mjd_max": f"mjd_max must be greater than mjd_min ({mjd_min} was applied)."}
                )

        return attrs

    # pyrefly: ignore [bad-override]
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
            "callback_url",
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
            "result_imagestack_url",
            "result_imagezip_url",
            "userqueuedtasks_on_submit",
            "waittime",
            "runtime",
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
            "result_imagestack_url",
            "result_imagezip_url",
            "userqueuedtasks_on_submit",
            "waittime",
            "runtime",
        ]
