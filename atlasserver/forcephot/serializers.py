import math
import typing as t
from collections.abc import Mapping
from typing import override

from django.conf import settings
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


def stack_requests_allowed(context: Mapping[str, t.Any]) -> bool:
    """Return whether the caller in this serializer context may submit an image stack request.

    The queue page reads the same rule to decide whether to offer the option, so the two cannot
    drift: an account that sees the box can submit, and one that does not see it cannot.
    """
    user = getattr(context.get("request"), "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False

    return bool(getattr(user, "is_staff", False) or user.pk in settings.TEST_USERS)


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

    # The result links below name the file's static path, which the web server answers without
    # Django. A check that a Django view makes (see views._taskresultfile_response) does not reach
    # a link that was already handed out.
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
    # Each of the two checked for the one request type that produces the file: the lookups stat the
    # results directory, and a page of a hundred light-curve tasks stated it three hundred times for
    # files that no light-curve task has. The queue page reads the links on those rows only.
    def get_result_imagezip_url(self, obj) -> str | None:
        if obj.request_type != "IMGZIP" or not obj.finishtimestamp:
            return None

        if (imagezipfile := obj.localresultimagezipfile) and (request := self.context.get("request")):
            return request.build_absolute_uri(staticfiles_storage.url(imagezipfile))

        return None

    def get_result_imagestack_url(self, obj) -> str | None:
        if obj.request_type != "SSOSTACK" or not obj.finishtimestamp:
            return None

        if (imagestackfile := obj.localresultimagestackfile) and (request := self.context.get("request")):
            return request.build_absolute_uri(staticfiles_storage.url(imagestackfile))

        return None

    @property
    def min_queuepos_relative(self) -> int:
        """Return the queue offset, computed once per request rather than once per task.

        Task.min_queuepos_relative() is a global aggregate that does not depend on the task being
        serialised. The viewset has made it already for the entity-tag, and hands it over in the
        context as "min_queuepos_relative"; a serializer built without it makes the aggregate once.
        Under many=True this serializer instance is reused for every task in the page, so the memo
        turns N aggregate queries into one.
        """
        if self._min_queuepos_cache is UNSET:
            given = self.context.get("min_queuepos_relative")
            self._min_queuepos_cache = Task.min_queuepos_relative() if given is None else given

        return self._min_queuepos_cache

    def get_queuepos(self, obj) -> int | None:
        if obj.finishtimestamp or obj.queuepos_relative is None:
            return None

        return obj.queuepos_relative - self.min_queuepos_relative

    @staticmethod
    def _rounded_seconds(value: float | None) -> float | None:
        """Round a second count for display, or pass through the None that means "not yet"."""
        return None if value is None else round(value, 1)

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
            # one submission validates this same URL for every row of a radeclist, and then again
            # for the whole list; views.create opens the scope that answers the repeats
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

    # the same finite check as ra and dec: a NaN here reached the database, where mysqlclient
    # refuses it with a 500, and the runner's command line, as `pmra=nan`
    @staticmethod
    def validate_propermotion_ra(value):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError(
                {"propermotion_ra": "propermotion_ra must be a finite floating-point number."}
            )

        return value

    @staticmethod
    def validate_propermotion_dec(value):
        if value is None or value == "":
            return value

        if not is_finite_float(value):
            raise serializers.ValidationError(
                {"propermotion_dec": "propermotion_dec must be a finite floating-point number."}
            )

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
    def to_representation(self, instance: Task) -> dict[str, t.Any]:
        """Serialise a task, keeping the submitter's callback URL to the submitter.

        Reading a task is public on purpose -- the reasoning is on
        ForcePhotPermission.has_object_permission -- and that covers the measurements a public
        survey produced. It does not cover callback_url. The callback carries no signature and no
        shared secret (see webhooks.send_task_callback), so the URL is the whole credential for
        whatever it points at, and such URLs routinely hold a secret in their path or query. Task
        ids are sequential, so without this anyone could walk them and collect every API user's.

        Emptied rather than dropped, so that the shape of the response does not depend on who is
        asking: a client that reads the field still finds it, and finds nothing in it.
        """
        data = super().to_representation(instance)

        if data.get("callback_url") and not instance.is_owned_by(getattr(self.context.get("request"), "user", None)):
            data["callback_url"] = None

        return data

    @override
    def update(self, instance: Task, validated_data: dict[str, t.Any]) -> Task:
        """Apply a change to a task, writing only the columns it touched.

        ModelSerializer.update() ends in a bare instance.save(), which writes every concrete column
        from the copy that was loaded at the start of the request. The task runner writes
        starttimestamp, finishtimestamp, error_msg and attempt_count to the same row with queryset
        updates (taskrunner.main.mark_started and mark_finished), and forcephot.queue renumbers
        queuepos_relative the same way -- so a PATCH that overlapped one of those reverted it.

        A finished task that loses its finishtimestamp re-enters Task.queued(), so the runner
        dispatches it again and mails the user a second set of results.

        task_modified_datetime is named as well because it is auto_now: Django only refreshes such
        a field when update_fields lists it, and the task list's entity tag is built from it, so
        leaving it out would hide the change from every polling browser. Task.delete() names it for
        the same reason.
        """
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save(update_fields=[*validated_data, "task_modified_datetime"])

        return instance

    @override
    def validate(self, attrs):
        mpc_name = self.submitted(attrs, "mpc_name")
        request_type = self.submitted(attrs, "request_type")

        # RA 0 and Dec 0 are valid coordinates, so test for "not given" rather than falsiness
        ra_missing = self.submitted(attrs, "ra") in (None, "")
        dec_missing = self.submitted(attrs, "dec") in (None, "")

        # Before the branch on the target below, not inside one arm of it. An IMGZIP task carries
        # the target of its parent, so these rules hold for an mpc_name request as much as for an
        # ra/dec one. A row with no parent cannot be named a result file, so the runner fails on
        # it before it records a finish time, and dispatches it again on every pass.
        # The stack request is a trial feature, offered on the queue page to the accounts the
        # settings name. The page's flag is only a hint to the browser, so the rule is applied
        # here, where a request from any token holder arrives.
        # On a creation, and on an update that turns another kind of task into one: an ordinary
        # account could otherwise submit a forced photometry task and PATCH its request_type.
        becomes_stack = request_type == "SSOSTACK" and (
            self.instance is None or self.instance.request_type != "SSOSTACK"
        )
        if becomes_stack and not stack_requests_allowed(self.context):
            msg = "Image stack requests are not enabled for this account."
            raise serializers.ValidationError(msg)

        if request_type == "IMGZIP":
            # An image request is not created here, and the message says so.
            #
            # parent_task_id is neither a model field name nor a relation name, so ModelSerializer
            # builds it as a ReadOnlyField and to_internal_value drops it. The old message asked
            # for that field, which made this a 400 the caller could never satisfy. RequestImages
            # is the path that carries what an image request needs: the parent's ownership rule,
            # the per-user cap, the client location fields and a queue position.
            if self.instance is None:
                msg = (
                    "IMGZIP tasks cannot be created here. Ask for the images of a finished"
                    " forced photometry task by POSTing to /queue/<id>/requestimages/."
                )
                raise serializers.ValidationError(msg)

            # An existing one must keep its parent: the runner names the parent's data file as the
            # list of observations to fetch images for.
            parent_task_id = self.submitted(attrs, "parent_task_id")
            if not parent_task_id:
                msg = "An IMGZIP task must keep its parent_task_id set to an FP task."
                raise serializers.ValidationError(msg)

            try:
                Task.objects.all().get(id=parent_task_id, request_type="FP")
            except (ObjectDoesNotExist, IndexError):
                msg = "An IMGZIP task must keep its parent_task_id set to an FP task id."
                raise serializers.ValidationError(msg) from None

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
            "attempt_count",
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
            "attempt_count",
        ]
