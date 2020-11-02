from django.db import models, connection
from django.contrib.auth.models import User
from django.conf import settings
import datetime
import math


def date_to_mjd(year, month, day):
    """
    Functions for converting dates to/from JD and MJD. Assumes dates are historical
    dates, including the transition from the Julian calendar to the Gregorian
    calendar in 1582. No support for proleptic Gregorian/Julian calendars.
    :Author: Matt Davis
    :Website: http://github.com/jiffyclub

    Convert a date to Julian Day.

    Algorithm from 'Practical Astronomy with your Calculator or Spreadsheet',
        4th ed., Duffet-Smith and Zwart, 2011.

    Parameters
    ----------
    year : int
        Year as integer. Years preceding 1 A.D. should be 0 or negative.
        The year before 1 A.D. is 0, 10 B.C. is year -9.

    month : int
        Month as integer, Jan = 1, Feb. = 2, etc.

    day : float
        Day, may contain fractional part.

    Returns
    -------
    jd : float
        Julian Day

    Examples
    --------
    Convert 6 a.m., February 17, 1985 to Julian Day

    >>> date_to_jd(1985,2,17.25)
    2446113.75

    """
    if month == 1 or month == 2:
        yearp = year - 1
        monthp = month + 12
    else:
        yearp = year
        monthp = month

    # this checks where we are in relation to October 15, 1582, the beginning
    # of the Gregorian calendar.
    if ((year < 1582) or
        (year == 1582 and month < 10) or
            (year == 1582 and month == 10 and day < 15)):
        # before start of Gregorian calendar
        B = 0
    else:
        # after start of Gregorian calendar
        A = math.trunc(yearp / 100.)
        B = 2 - A + math.trunc(A / 4.)

    if yearp < 0:
        C = math.trunc((365.25 * yearp) - 0.75)
    else:
        C = math.trunc(365.25 * yearp)

    D = math.trunc(30.6001 * (monthp + 1))

    jd = B + C + D + day + 1720994.5
    mjd = jd - 2400000.5

    return mjd


def get_mjd_min_default():
    date_min = datetime.date.today() - datetime.timedelta(days=30)
    return date_to_mjd(date_min.year, date_min.month, date_min.day)


class Task(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    ra = models.FloatField(null=False, blank=False, default=None)
    dec = models.FloatField(null=False, blank=False, default=None)
    mjd_min = models.FloatField(null=True, blank=True, default=get_mjd_min_default, verbose_name='MJD min')
    mjd_max = models.FloatField(null=True, blank=True, default=None, verbose_name='MJD max')
    comment = models.CharField(default=None, null=True, blank=True, max_length=300)
    use_reduced = models.BooleanField("Use reduced images instead of difference images", default=False)
    finished = models.BooleanField(default=False)
    finishtimestamp = models.DateTimeField(null=True, default=None)

    def get_localresultfile(self):
        if self.finished:
            return f'results/job{int(self.id):05d}.txt'

        return None

    def get_queuepos(self):
        if self.finished:
            return -1
        else:
            return Task.objects.filter(timestamp__lt=self.timestamp, finished=False).count()
            # with connection.cursor() as cursor:
            #     cursor.execute("UPDATE bar SET foo = 1 WHERE baz = %s", [self.baz])
            return 22

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
