import datetime
import math

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, models
from django.utils import timezone

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
    finished = models.BooleanField(default=False)
    finishtimestamp = models.DateTimeField(null=True, default=None)
    starttimestamp = models.DateTimeField(null=True, default=None)

    def get_localresultfile(self):
        if self.finished:
            return f'results/job{int(self.id):05d}.txt'

        return None

    def get_queuepos(self):
        if self.finished:
            return -1
        else:
            return Task.objects.filter(timestamp__lt=self.timestamp, finished=False).count()

    def __str__(self):
        email = User.objects.get(id=self.user_id).email
        return f"RA: {self.ra} DEC: {self.dec} {email}"


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
        return f"RA: {self.ra} DEC: {self.declination} MJD {self.mjd} m {self.m}"
