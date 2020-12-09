from django import template
from django.contrib.humanize.templatetags import humanize
import datetime

register = template.Library()

@register.filter
def tasktimesince(value):
    seconds = (datetime.datetime.now(datetime.timezone.utc) - value).total_seconds()
    if seconds < 3001:
        return f'{seconds:.0f} seconds ago'

    return humanize.naturaltime(value)
