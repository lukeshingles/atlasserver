from django.db import models
from django.contrib.auth.models import User
from django.conf import settings


class RoundingDecimalField(models.DecimalField):
    def validate_precision(self, value):
        return value



class Tasks(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    ra = models.DecimalField(max_digits=8, decimal_places=5)
    dec = models.DecimalField(max_digits=8, decimal_places=5)
    mjd_min = models.FloatField(null=True, blank=True, default=None)
    mjd_max = models.FloatField(null=True, blank=True, default=None)
    use_reduced = models.BooleanField("Use reduced images instead of difference", default=False)
    finished = models.BooleanField(default=False)

    def get_localresultfile(self):
        if self.finished:
            return f'static/results/job{int(self.id):05d}.txt'

        return None
