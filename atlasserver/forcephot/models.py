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
