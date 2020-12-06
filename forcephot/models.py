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
    finishtimestamp = models.DateTimeField(null=True, default=None)
    starttimestamp = models.DateTimeField(null=True, default=None)

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
        else:
            return Task.objects.filter(timestamp__lt=self.timestamp, finishtimestamp__isnull=True).count()

    def finished(self):
        return True if self.finishtimestamp else False

    def __str__(self):
        user = User.objects.get(id=self.user_id)
        userstr = f"{user.username} ({user.email})"
        return (f"{self.timestamp:%Y-%m-%d %H:%M %Z} {userstr} RA: {self.ra:09.4f} DEC: {self.dec:09.4f}"
                f" {'finished' if self.finishtimestamp else ''} " +
                f" {'img_reduced' if self.use_reduced else 'img_difference'}")


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
