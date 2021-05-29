import datetime
# import os

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from pathlib import Path
from forcephot.misc import date_to_mjd, country_code_to_name


def get_mjd_min_default():
    date_min = datetime.date.today() - datetime.timedelta(days=30)
    return date_to_mjd(date_min.year, date_min.month, date_min.day)


class Task(models.Model):
    class RequestType(models.TextChoices):
        FP = 'FP', "Forced Photometry Data"
        IMGZIP = 'IMGZIP', "Image Zip"

    timestamp = models.DateTimeField(default=timezone.now)
    starttimestamp = models.DateTimeField(null=True, blank=True, default=None)
    finishtimestamp = models.DateTimeField(null=True, blank=True, default=None)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    # the task must specify either Minor Planet Center object name (overrides RA and Dec)
    # or RA and Dec in floating-point degrees
    mpc_name = models.CharField(null=True, blank=True, default=None, max_length=300,
                                verbose_name="Minor Planet Center object name (overrides RA/Dec)")

    ra = models.FloatField(null=True, blank=True, default=None, verbose_name='Right Ascension (degrees)')
    dec = models.FloatField(null=True, blank=True, default=None, verbose_name='Declination (degrees)')

    mjd_min = models.FloatField(null=True, blank=True, default=get_mjd_min_default, verbose_name='MJD min')
    mjd_max = models.FloatField(null=True, blank=True, default=None, verbose_name='MJD max')
    comment = models.CharField(default=None, null=True, blank=True, max_length=300)
    use_reduced = models.BooleanField("Use reduced images instead of difference images", default=False)
    send_email = models.BooleanField("Email me when completed", default=True)
    from_api = models.BooleanField(default=False)
    country_code = models.CharField(default=None, null=True, blank=True, max_length=2)
    region = models.CharField(default=None, null=True, blank=True, max_length=256)
    city = models.CharField(default=None, null=True, blank=True, max_length=256)
    error_msg = models.CharField(null=True, blank=True, default=None, max_length=200,
                                 verbose_name="Error messages during execution")
    is_archived = models.BooleanField(default=False)
    radec_epoch_year = models.DecimalField(null=True, blank=True, max_digits=7, decimal_places=1,
                                           verbose_name='Epoch year')
    propermotion_ra = models.FloatField(null=True, blank=True, verbose_name='Proper motion RA (mas/yr)')
    propermotion_dec = models.FloatField(null=True, blank=True, verbose_name='Proper motion Dec (mas/yr)')

    parent_task = models.ForeignKey('self',
                                    related_name='imagerequest',
                                    # on_delete=models.SET_NULL,
                                    on_delete=models.CASCADE,
                                    null=True, default=None,
                                    limit_choices_to={'request_type': 'FP'})

    request_type = models.CharField(
        max_length=6,
        choices=RequestType.choices,
        default=RequestType.FP
    )

    def localresultfileprefix(self, use_parent=False):
        """
            return the relative path prefix for the job (no file extension)
        """
        if use_parent and self.parent_task:
            int_id = int(self.parent_task.id)
        else:
            int_id = int(self.id)
        return f'results/job{int_id:05d}'

    def localresultfile(self):
        """
            return the relative path to the FP data file if the job is finished
        """
        if self.finishtimestamp:
            return self.localresultfileprefix() + '.txt'

        return None

    @property
    def localresultpreviewimagefile(self):
        """
            return the full local path to the image file if it exists, otherwise None
        """
        if self.finishtimestamp:
            imagefile = self.localresultfileprefix(use_parent=True) + '.jpg'
            if Path(settings.STATIC_ROOT, imagefile).exists():
                return imagefile

        return None

    @property
    def localresultpdfplotfile(self):
        """
            return the full local path to the PDF plot file if the job is finished
        """
        if self.finishtimestamp:
            return self.localresultfileprefix() + '.pdf'

        return None

    def localresultjsplotfile(self):
        """
            return the full local path to the plotly javascript file if the FP data file exists, otherwise None
        """
        if self.localresultfile():
            return Path(settings.STATIC_ROOT, self.localresultfile()).with_suffix('.js')

        return None

    @property
    def localresultimagezipfile(self):
        """
            return the full local path to the image zip file if it exists, otherwise None
        """
        imagezipfile = Path(self.localresultfileprefix(use_parent=True) + '.zip')
        if Path(settings.STATIC_ROOT, imagezipfile).exists():
            return imagezipfile

        return None

    @property
    def imagerequest_taskid(self):
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

    def queuepos(self):
        if self.finishtimestamp:
            return -1

        # this can probably be done more efficiently. The goal is to figure out the task queue position
        # given that a round-robin queue is used

        posallqueue = 0.

        # task position in the owner's queue
        posownerqueue = Task.objects.filter(id__lt=self.id, finishtimestamp__isnull=True, user=self.user).count()

        for tmpuser in User.objects.all():
            tmpusertasks = Task.objects.filter(
                id__lt=self.id, finishtimestamp__isnull=True, user=tmpuser).order_by('id')

            if tmpusertasks.count() > posownerqueue:
                # add the number of tasks from earlier full passes
                posallqueue += posownerqueue

                # add one if this tmpuser's task preceeds it in the final pass when the task runs
                if tmpusertasks[posownerqueue].id < self.id:
                    posallqueue += 1
            else:
                # add the number of tasks from earlier full passes
                posallqueue += tmpusertasks.count()

        # queuepos = sum[
        # x = Task.objects.filter(timestamp__lt=self.timestamp, finishtimestamp__isnull=True, user=self.user).count()
        return int(posallqueue)

    def finished(self):
        return True if self.finishtimestamp else False

    def waittime(self):
        if self.starttimestamp and self.timestamp:
            timediff = self.starttimestamp - self.timestamp
            return timediff.total_seconds()

        return float('NaN')

    def runtime(self):
        if self.finishtimestamp and self.starttimestamp:
            timediff = self.finishtimestamp - self.starttimestamp
            return timediff.total_seconds()

        return float('NaN')

    def __str__(self):
        user = User.objects.get(id=self.user_id)
        if self.mpc_name:
            targetstr = " MPC[" + self.mpc_name + "]"
        else:
            targetstr = f" RA Dec: {self.ra:09.4f} {self.dec:09.4f}"

        if self.finishtimestamp:
            status = 'finished'
        elif self.starttimestamp:
            status = 'running'
        else:
            status = 'queued'

        strtask = (
            f"Task {self.id:d}: " +
            f"{self.timestamp:%Y-%m-%d %H:%M:%S %Z} " +
            f"{user.username} ({user.email})" +
            (f" '{country_code_to_name(self.country_code)}'" if self.country_code else "") +
            f"{' API' if self.from_api else ''}" +
            f" {self.request_type}" +
            targetstr +
            f" {'redimg' if self.use_reduced else 'diffimg'}" +
            f" {status} " +
            f"{' archived' if self.is_archived else ''}"
        )

        if self.starttimestamp:
            strtask += f" waittime: {self.waittime():.0f}s"
        if self.finishtimestamp:
            strtask += f" runtime: {self.runtime():.0f}s"

        return strtask

    def delete(self):
        # cleanup associated files when removing a task object from the database
        if self.request_type == 'IMGZIP':
            zipfile = self.localresultimagezipfile
            if zipfile:
                try:
                    Path(settings.STATIC_ROOT, zipfile).unlink()
                except FileNotFoundError:
                    pass
        else:
            # for localfile in Path(settings.STATIC_ROOT).glob(pattern=self.localresultfileprefix() + '.*'):
            for ext in ['.txt', '.pdf', '.js', '.jpg']:
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
