from django.db import models
from django.contrib.auth.models import User
from django.conf import settings


class Task(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    ra = models.FloatField(null=False, blank=False, default=None)
    dec = models.FloatField(null=False, blank=False, default=None)
    mjd_min = models.FloatField(null=True, blank=True, default=None, verbose_name='MJD min')
    mjd_max = models.FloatField(null=True, blank=True, default=None, verbose_name='MJD max')
    use_reduced = models.BooleanField("Use reduced images instead of difference images", default=False)
    finished = models.BooleanField(default=False)

    def get_localresultfile(self):
        if self.finished:
            return f'static/results/job{int(self.id):05d}.txt'

        return None

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

    def get_localresultfile(self):
        if self.finished:
            return f'static/results/job{int(self.id):05d}.txt'

        return None

    def __str__(self):
        email = User.objects.get(id=self.user_id).email
        return f"RA: {self.ra} DEC: {self.dec} {email}"
