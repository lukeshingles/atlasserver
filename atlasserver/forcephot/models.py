import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Min
from django.utils import timezone

from atlasserver.forcephot.misc import country_code_to_name
from atlasserver.forcephot.misc import datetime_to_mjd


def get_mjd_min_default():
    return round(datetime_to_mjd(datetime.datetime.now() - datetime.timedelta(days=30)), 5)


class Task(models.Model):
    class RequestType(models.TextChoices):
        FP = "FP", "Forced Photometry Data"
        IMGZIP = "IMGZIP", "Image Zip"

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
        null=True, blank=True, default=None, max_length=200, verbose_name="Error messages during execution"
    )
    is_archived = models.BooleanField(default=False)
    radec_epoch_year = models.DecimalField(
        null=True, blank=True, max_digits=7, decimal_places=1, verbose_name="Epoch year"
    )
    propermotion_ra = models.FloatField(null=True, blank=True, verbose_name="Proper motion RA (mas/yr)")
    propermotion_dec = models.FloatField(null=True, blank=True, verbose_name="Proper motion Dec (mas/yr)")
    queuepos_relative = models.IntegerField(null=True, blank=True, default=None, verbose_name="Queue position")

    parent_task = models.ForeignKey(
        "self",
        related_name="imagerequest",
        # on_delete=models.SET_NULL,
        on_delete=models.CASCADE,
        null=True,
        default=None,
        limit_choices_to={"request_type": "FP"},
    )

    request_type = models.CharField(max_length=6, choices=RequestType.choices, default=RequestType.FP)

    task_modified_datetime = models.DateTimeField(auto_now=True)

    def localresultfileprefix(self, use_parent=False):
        """
        return the relative path prefix for the job (no file extension)
        """
        if use_parent and self.parent_task:
            int_id = int(self.parent_task.id)
        else:
            int_id = int(self.id)
        return f"results/job{int_id:05d}"

    def localresultfile(self):
        """
        return the relative path to the FP data file if the job is finished,
        and the file exists
        """
        if self.finishtimestamp:
            resultfile = self.localresultfileprefix() + ".txt"
            if Path(settings.STATIC_ROOT, resultfile).exists():
                return resultfile

        return None

    @property
    def localresultpreviewimagefile(self):
        """
        return the full local path to the image file if it exists, otherwise None
        """
        if self.finishtimestamp:
            imagefile = self.localresultfileprefix(use_parent=True) + ".jpg"
            if Path(settings.STATIC_ROOT, imagefile).exists():
                return imagefile

        return None

    @property
    def localresultpdfplotfile(self):
        """
        return the full local path to the PDF plot file if the job is finished
        """
        if self.finishtimestamp:
            return self.localresultfileprefix() + ".pdf"

        return None

    def localresultjsplotfile(self):
        """
        return the full local path to the plotly javascript file if the FP data file exists, otherwise None
        """
        if self.localresultfile():
            return Path(settings.STATIC_ROOT, self.localresultfile()).with_suffix(".js")

        return None

    @property
    def localresultimagezipfile(self):
        """
        return the full local path to the image zip file if it exists, otherwise None
        """
        imagezipfile = Path(self.localresultfileprefix(use_parent=True) + ".zip")
        if Path(settings.STATIC_ROOT, imagezipfile).exists():
            return imagezipfile

        return None

    @property
    def imagerequest_task_id(self):
        """
        return the task id of the image request task associated with this
        forced photometry task if it exists, otherwise None
        """
        associated_tasks = Task.objects.filter(parent_task_id=self.id, is_archived=False)
        if associated_tasks.count() > 0:
            return associated_tasks[0].id

        return None

    @property
    def imagerequest_finished(self):
        """
        return the task id of the image request task associated with this
        forced photometry task if it exists, otherwise None
        """
        associated_tasks = Task.objects.filter(parent_task_id=self.id, is_archived=False)
        if associated_tasks.count() > 0:
            return True if associated_tasks[0].finishtimestamp else False

        return None

    @property
    def queuepos(self):
        if self.finishtimestamp or self.queuepos_relative is None:
            return None

        # after completing a job, the next job might not have queuepos_relative=0 until a queue order refresh is done
        # so queuepos_relative=1 could have queuepos 0 (is next)
        minqueuepos = Task.objects.filter(finishtimestamp__isnull=True, is_archived=False).aggregate(
            Min("queuepos_relative")
        )["queuepos_relative__min"]

        if minqueuepos is None:
            minqueuepos = 0

        return self.queuepos_relative - int(minqueuepos)

    def finished(self):
        return True if self.finishtimestamp else False

    def waittime(self):
        if self.starttimestamp and self.timestamp:
            timediff = self.starttimestamp - self.timestamp
            return timediff.total_seconds()

        return float("NaN")

    def runtime(self):
        if self.finishtimestamp and self.starttimestamp:
            timediff = self.finishtimestamp - self.starttimestamp
            return timediff.total_seconds()

        return float("NaN")

    @property
    def username(self):
        return self.user.username

    def __str__(self):
        user = get_user_model().objects.get(id=self.user_id)
        if self.mpc_name:
            targetstr = " MPC[" + self.mpc_name + "]"
        else:
            targetstr = f" RA Dec: {self.ra:09.4f} {self.dec:09.4f}"

        if self.finishtimestamp:
            status = "finished"
        elif self.starttimestamp:
            status = "running"
        else:
            status = "queued"

        strtask = (
            f"Task {self.id:d}: "
            + f"{self.timestamp:%Y-%m-%d %H:%M:%S %Z} "
            + f"{user.username} ({user.email})"
            + (f" '{country_code_to_name(self.country_code)}'" if self.country_code else "")
            + f"{' API' if self.from_api else ''}"
            + f" {self.request_type}"
            + targetstr
            + f" {'redimg' if self.use_reduced else 'diffimg'}"
            + f" {status} "
            + f"{' archived' if self.is_archived else ''}"
        )

        if self.starttimestamp:
            strtask += f" waittime: {self.waittime():.0f}s"
        if self.finishtimestamp:
            strtask += f" runtime: {self.runtime():.0f}s"

        return strtask

    def delete(self):
        # cleanup associated files when removing a task object from the database
        if self.request_type == "IMGZIP":
            zipfile = self.localresultimagezipfile
            if zipfile:
                try:
                    Path(settings.STATIC_ROOT, zipfile).unlink()
                except FileNotFoundError:
                    pass
        else:
            # for localfile in Path(settings.STATIC_ROOT).glob(pattern=self.localresultfileprefix() + '.*'):
            delete_extlist = [".txt", ".pdf", ".js"]
            imgreqtaskid = self.imagerequest_task_id
            if imgreqtaskid is None:  # image request tasks share this same preview image
                delete_extlist.append(".jpg")

            for ext in delete_extlist:
                try:
                    Path(settings.STATIC_ROOT, self.localresultfileprefix() + ext).unlink()
                except FileNotFoundError:
                    pass

        # keep finished jobs in the database but mark them as archived and hide them from the website
        if self.finished():
            self.is_archived = True
            self.save()
        else:
            super().delete()
