import datetime
import typing as t
from pathlib import Path

from django.conf import settings
from django.core.cache import caches
from django.db import models
from django.db.models import Min
from django.utils import timezone

from atlasserver.forcephot.misc import country_code_to_name
from atlasserver.forcephot.misc import datetime_to_mjd
from atlasserver.forcephot.misc import resultplotdatajs_cachekey


def get_mjd_min_default():
    return round(datetime_to_mjd(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)), 5)


# marks a memoised value that has not been computed yet. None is a meaningful result for every
# value memoised below, so it cannot double as the "not computed" marker.
UNSET: t.Final = object()


class Task(models.Model):
    class RequestType(models.TextChoices):
        FP = "FP", "Forced Photometry Data"
        IMGZIP = "IMGZIP", "Image Zip"
        IMGSTACK = "SSOSTACK", "Solar System object image stack"

    timestamp = models.DateTimeField(default=timezone.now)
    starttimestamp = models.DateTimeField(null=True, blank=True, default=None)
    finishtimestamp = models.DateTimeField(null=True, blank=True, default=None)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    # the task must specify either Minor Planet Center object name (overrides RA and Dec)
    # or RA and Dec in floating-point degrees
    mpc_name = models.CharField(
        null=True,
        blank=True,
        default=None,
        max_length=300,
        verbose_name="Minor Planet Center object name (overrides RA/Dec)",
    )

    ra = models.FloatField(null=True, blank=True, default=None, verbose_name="Right Ascension (degrees)")
    dec = models.FloatField(null=True, blank=True, default=None, verbose_name="Declination (degrees)")

    mjd_min = models.FloatField(null=True, blank=True, default=get_mjd_min_default, verbose_name="MJD min")
    mjd_max = models.FloatField(null=True, blank=True, default=None, verbose_name="MJD max")
    comment = models.CharField(default=None, null=True, blank=True, max_length=300)
    use_reduced = models.BooleanField("Use reduced images instead of difference images", default=False)
    send_email = models.BooleanField("Email me when completed", default=True)
    from_api = models.BooleanField(default=False)
    country_code = models.CharField(default=None, null=True, blank=True, max_length=2)
    region = models.CharField(default=None, null=True, blank=True, max_length=256)
    # city = models.CharField(default=None, null=True, blank=True, max_length=256)
    error_msg = models.CharField(
        null=True, blank=True, default=None, max_length=512, verbose_name="Error messages during execution"
    )
    is_archived = models.BooleanField(default=False)
    # optional completion callback for API clients, which are not sent result emails and would
    # otherwise have to poll. Validated on submission and again before the request is sent, see
    # atlasserver.forcephot.webhooks.
    callback_url = models.URLField(
        null=True,
        blank=True,
        default=None,
        max_length=500,
        verbose_name="Completion callback URL (API only)",
    )
    radec_epoch_year = models.DecimalField(
        null=True, blank=True, max_digits=7, decimal_places=1, verbose_name="Epoch year"
    )
    propermotion_ra = models.FloatField(null=True, blank=True, verbose_name="Proper motion RA (mas/yr)")
    propermotion_dec = models.FloatField(null=True, blank=True, verbose_name="Proper motion Dec (mas/yr)")
    queuepos_relative = models.IntegerField(null=True, blank=True, default=None, verbose_name="Queue position")
    userqueuedtasks_on_submit = models.IntegerField(
        null=True, blank=True, default=None, verbose_name="User queued tasks when submitted", editable=False
    )

    parent_task = models.ForeignKey(
        "self",
        related_name="imagerequest",
        # on_delete=models.SET_NULL,
        on_delete=models.CASCADE,
        null=True,
        default=None,
        limit_choices_to={"request_type": "FP"},
    )

    request_type = models.CharField(max_length=8, choices=RequestType.choices, default=RequestType.FP)

    task_modified_datetime = models.DateTimeField(auto_now=True)

    id: int
    user_id: int

    # per-instance memoisation slots, declared as class attributes so that no __init__ override is
    # needed. Model instances do not outlive a request, so a stale entry is not a concern.
    _localresultfile_cache: t.Any = UNSET
    _imagerequest_cache: t.Any = UNSET

    class Meta:
        indexes = [
            # the task list: filter on (is_archived, user), order by (-timestamp, -id)
            models.Index(fields=["is_archived", "user", "-timestamp", "-id"], name="task_userlist_idx"),
            # the task runner's queue scan and the queuepos aggregate: unfinished and not archived,
            # ordered by queue position
            models.Index(fields=["finishtimestamp", "is_archived", "queuepos_relative"], name="task_queue_idx"),
            # Task._imagerequest_task(): the live image request belonging to a parent task
            models.Index(fields=["parent_task", "is_archived", "id"], name="task_imagereq_idx"),
            # the hourly maintenance sweeps, which select by type and finish time
            models.Index(fields=["request_type", "finishtimestamp"], name="task_maint_idx"),
            # the usage statistics views, which window on submission time
            models.Index(fields=["timestamp"], name="task_timestamp_idx"),
        ]

    def __str__(self) -> str:
        """Return a string representation of the task (as seen in the admin panel list of tasks)."""
        user = self.user
        if self.mpc_name:
            targetstr = f" MPC[{self.mpc_name}]"
        elif self.ra is not None and self.dec is not None:
            targetstr = f" RA Dec: {self.ra:09.4f} {self.dec:09.4f}"
        else:  # a task with no target at all can be created in the admin panel
            targetstr = " RA Dec: (none)"

        if self.finishtimestamp:
            status = "finished"
        elif self.starttimestamp:
            status = "running"
        else:
            status = "queued"

        strtask = (
            f"Task {self.id:d}: {self.timestamp:%Y-%m-%d %H:%M:%S %Z} {user.username} ({user.email})"
            + (f" '{country_code_to_name(self.country_code)}'" if self.country_code else "")
            + f"{' API' if self.from_api else ''} {self.request_type}"
            + targetstr
            + f" {'redimg' if self.use_reduced else 'diffimg'} {status} {' archived' if self.is_archived else ''}"
        )

        if self.starttimestamp:
            strtask += f" waittime: {self.waittime():.0f}s"
        if self.finishtimestamp:
            strtask += f" runtime: {self.runtime():.0f}s"

        strtask += f" queuedtasks_on_submit: {self.userqueuedtasks_on_submit}"

        return strtask

    def localresultfileprefix(self, use_parent: bool = False) -> str:
        """Return the relative path prefix for the job (no file extension)."""
        int_id = int(self.parent_task.id) if use_parent and self.parent_task else int(self.id)
        return f"results/job{int_id:05d}"

    def localresultfile(self) -> str | None:
        """Return the relative path to the FP data file if the job is finished, and the file exists.

        The answer is memoised on the instance: serialising one task asks for it twice (once for
        the result URL and once for the PDF plot URL), and each miss costs a filesystem stat.
        """
        if self._localresultfile_cache is UNSET:
            resultfile = None
            if self.finishtimestamp:
                relpath = f"{self.localresultfileprefix()}.txt"
                if Path(settings.STATIC_ROOT, relpath).exists():
                    resultfile = relpath
            self._localresultfile_cache = resultfile

        return self._localresultfile_cache

    @property
    def localresultpreviewimagefile(self) -> str | None:
        """Return the full local path to the image file if it exists, otherwise None."""
        if self.finishtimestamp:
            imagefile = f"{self.localresultfileprefix(use_parent=True)}.jpg"
            if Path(settings.STATIC_ROOT, imagefile).exists():
                return imagefile

        return None

    @property
    def localresultpdfplotfile(self) -> str | None:
        """Return the full local path to the PDF plot file if the job is finished."""
        return f"{self.localresultfileprefix()}.pdf" if self.finishtimestamp else None

    @property
    def localresultimagezipfile(self) -> Path | None:
        """Return the full local path to the image zip file if it exists, otherwise None."""
        imagezipfile = Path(f"{self.localresultfileprefix(use_parent=True)}.zip")
        return imagezipfile if Path(settings.STATIC_ROOT, imagezipfile).exists() else None

    @property
    def localresultimagestackfile(self) -> Path | None:
        """Return the full local path to the image stack FITS file if it exists, otherwise None."""
        imagstackfile = Path(f"{self.localresultfileprefix()}.fits")
        return imagstackfile if Path(settings.STATIC_ROOT, imagstackfile).exists() else None

    def _imagerequest_task(self) -> "Task | None":
        """Return the image request task associated with this forced photometry task, or None.

        A parent can end up with more than one live image request (e.g. a double-clicked
        button), so order the query to make sure that every caller agrees on which one it is.

        Serialising one task asks for this three times (imagerequest_task_id, imagerequest_finished,
        and the imagerequest URL), so the result is memoised. When the caller prefetched the
        relation (see PREFETCH_IMAGEREQUESTS), the whole page costs one query instead of one per
        task.
        """
        prefetched = getattr(self, "live_imagerequests", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None

        if self._imagerequest_cache is UNSET:
            self._imagerequest_cache = (
                Task.objects.filter(parent_task_id=self.id, is_archived=False).order_by("id").first()
            )

        return self._imagerequest_cache

    @property
    def imagerequest_task_id(self) -> int | None:
        """Return the task id of the image request task associated with this forced photometry task if it exists, otherwise None."""
        imagerequest = self._imagerequest_task()
        return imagerequest.id if imagerequest is not None else None

    @property
    def imagerequest_finished(self) -> bool | None:
        """Return whether the image request task associated with this forced photometry task has finished, otherwise None."""
        imagerequest = self._imagerequest_task()
        return bool(imagerequest.finishtimestamp) if imagerequest is not None else None

    @staticmethod
    def min_queuepos_relative() -> int:
        """Return the lowest queue position currently assigned, or 0 if the queue is empty.

        After completing a job, the next job might not have queuepos_relative=0 until a queue order
        refresh is done, so queuepos_relative=1 could have queuepos 0 (is next).

        This does not depend on any particular task, so a caller serialising many tasks should call
        it once rather than once per task (see ForcePhotTaskSerializer.min_queuepos_relative).
        """
        minqueuepos = Task.objects.filter(finishtimestamp__isnull=True, is_archived=False).aggregate(
            Min("queuepos_relative")
        )["queuepos_relative__min"]

        return 0 if minqueuepos is None else int(minqueuepos)

    @property
    def queuepos(self) -> int | None:
        if self.finishtimestamp or self.queuepos_relative is None:
            return None

        return self.queuepos_relative - Task.min_queuepos_relative()

    def finished(self) -> bool:
        return bool(self.finishtimestamp)

    def waittime(self) -> float:
        if self.starttimestamp and self.timestamp:
            timediff = self.starttimestamp - self.timestamp
            return timediff.total_seconds()

        return float("NaN")

    def runtime(self) -> float:
        if self.finishtimestamp and self.starttimestamp:
            timediff = self.finishtimestamp - self.starttimestamp
            return timediff.total_seconds()

        return float("NaN")

    @property
    def username(self) -> str:
        return self.user.username

    @staticmethod
    def prefetch_imagerequests() -> models.Prefetch:
        """Return the prefetch that lets _imagerequest_task() answer without a query per task."""
        return models.Prefetch(
            "imagerequest",
            queryset=Task.objects.filter(is_archived=False).order_by("id"),
            to_attr="live_imagerequests",
        )

    def delete_result_files(self) -> None:
        """Delete the result files belonging to this task.

        Split out of delete() so that the maintenance sweep can reclaim the files of many tasks and
        then update or remove their rows in one statement, instead of one write per task.
        """
        if self.request_type == "IMGZIP":
            if zipfile := self.localresultimagezipfile:
                Path(settings.STATIC_ROOT, zipfile).unlink(missing_ok=True)

            # the parent's .txt and .jpg are kept while a live image request needs them, so once
            # this was the last one they have to be collected here or nothing reclaims them until
            # the maintenance sweep runs months later
            parent = self.parent_task
            if parent is not None and parent.is_archived:
                siblings = Task.objects.filter(parent_task_id=parent.id, is_archived=False).exclude(id=self.id)
                if not siblings.exists():
                    for ext in (".txt", ".jpg"):
                        Path(settings.STATIC_ROOT, parent.localresultfileprefix() + ext).unlink(missing_ok=True)

        else:
            delete_extlist = [".pdf", ".fits"]
            if self.imagerequest_task_id is None:
                # image request tasks share this preview image, and need the .txt data file
                # as the input that lists the observations to fetch images for
                delete_extlist += [".jpg", ".txt"]

            for ext in delete_extlist:
                Path(settings.STATIC_ROOT, self.localresultfileprefix() + ext).unlink(missing_ok=True)

    def forget_derived_cache(self) -> None:
        """Drop the cached plot data generated from this task's result file."""
        caches["taskderived"].delete(resultplotdatajs_cachekey(self.id))

    def delete(self, using: t.Any | None = None, keep_parents: bool = False):
        # cleanup associated files when removing a task object from the database
        self.delete_result_files()

        # keep finished jobs in the database but mark them as archived and hide them from the website
        if self.finished():
            self.is_archived = True
            # only the one column changed, so there is no reason to rewrite every other one
            self.save(update_fields=["is_archived", "task_modified_datetime"])
            self.forget_derived_cache()
        else:
            super().delete(using=using, keep_parents=keep_parents)
