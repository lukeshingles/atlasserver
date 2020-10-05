from django.db import models
from django.contrib.auth.models import User
from django.conf import settings


class ForcePhotTask(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    ra = models.FloatField()
    dec = models.FloatField()
    mjd_min = models.FloatField(null=True, blank=True, default=None)
    mjd_max = models.FloatField(null=True, blank=True, default=None)
    finished = models.BooleanField(default=False)
