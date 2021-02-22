import datetime
import math
import os

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, models
from django.utils import timezone
from pathlib import Path
from forcephot.misc import date_to_mjd


def get_mjd_min_default():
    date_min = datetime.date.today() - datetime.timedelta(days=30)
    return date_to_mjd(date_min.year, date_min.month, date_min.day)


class Task(models.Model):
    timestamp = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    ra = models.FloatField(null=False, blank=False, default=None)
    dec = models.FloatField(null=False, blank=False, default=None)
    mjd_min = models.FloatField(null=True, blank=True, default=get_mjd_min_default, verbose_name='MJD min')
    mjd_max = models.FloatField(null=True, blank=True, default=None, verbose_name='MJD max')
    comment = models.CharField(default=None, null=True, blank=True, max_length=300)
    use_reduced = models.BooleanField("Use reduced images instead of difference images", default=False)
    send_email = models.BooleanField("Email me when completed", default=True)
    finishtimestamp = models.DateTimeField(null=True, blank=True, default=None)
    starttimestamp = models.DateTimeField(null=True, blank=True, default=None)
    from_api = models.BooleanField(null=True, blank=True, default=None)
    country_code = models.CharField(default=None, null=True, blank=True, max_length=4)

    def localresultfile(self):
        if self.finishtimestamp:
            return f'results/job{int(self.id):05d}.txt'

        return None

    def localresultpdfplotfile(self):
        if self.localresultfile():
            pdfplotfile = Path(self.localresultfile()).with_suffix('.pdf')
            if os.path.exists(Path(settings.STATIC_ROOT, pdfplotfile)):
                return pdfplotfile

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
            tmpusertasks = Task.objects.filter(id__lt=self.id, finishtimestamp__isnull=True, user=tmpuser).order_by('id')

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
            # now = datetime.datetime.utcnow().replace(tzinfo=utc)
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
        userstr = f"{user.username} ({user.email})"
        strtask = (
            f"{self.timestamp:%Y-%m-%d %H:%M:%S %Z} {userstr} RA: {self.ra:09.4f} DEC: {self.dec:09.4f}"
            f" {'img_reduced' if self.use_reduced else 'img_diff'}" +
            f" {'finished' if self.finished() else 'queued'} ")
        if self.finished():
            strtask += f" waittime: {self.waittime():.1f}s"
            strtask += f" runtime: {self.runtime():.1f}s"
        return strtask

    def delete(self):
        # cleanup associated files when removing a task object from the database
        if self.localresultfile():
            localresultfullpath = os.path.join(settings.STATIC_ROOT, self.localresultfile())
            if os.path.exists(localresultfullpath):
                os.remove(localresultfullpath)

        if self.localresultpdfplotfile():
            localresultpdffullpath = os.path.join(settings.STATIC_ROOT, self.localresultpdfplotfile())
            if os.path.exists(localresultpdffullpath):
                os.remove(localresultpdffullpath)

        if self.finished():
            self.is_archived = True
            self.save()
        else:
            super().delete()


class Result(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    ra = models.FloatField()
    declination = models.FloatField()
    mjd = models.FloatField()
    m = models.FloatField()
    dm = models.FloatField()
    ujy = models.IntegerField()
    dujy = models.IntegerField()
    filter = models.CharField(max_length=1)
    err = models.FloatField()
    chi_over_n = models.FloatField()
    x = models.FloatField()
    y = models.FloatField()
    maj = models.FloatField()
    min = models.FloatField()
    phi = models.FloatField()
    sky = models.FloatField()
    apfit = models.FloatField()
    zp = models.FloatField()
    obs = models.CharField(max_length=32)

    use_reduced = models.BooleanField("Use reduced images instead of difference images", default=False)

    def __str__(self):
        return f"RA: {self.ra:10.4f} DEC: {self.declination:10.4f} MJD {self.mjd} m {self.m}"
