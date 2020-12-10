from django import template
from django.contrib.humanize.templatetags import humanize
from rest_framework.utils.urls import remove_query_param, replace_query_param
import datetime

register = template.Library()


@register.filter
def tasktimesince(value):
    seconds = (datetime.datetime.now(datetime.timezone.utc) - value).total_seconds()
    if seconds < 350:
        return f'{seconds:.0f} seconds ago'

    return humanize.naturaltime(value)


@register.filter
def removetaskboxqueryparam(value):
    return remove_query_param(value, 'htmltaskframeonly')


@register.filter
def addtaskboxqueryparam(value):
    print(replace_query_param(value, 'htmltaskframeonly', 'true'))
    return replace_query_param(value, 'htmltaskframeonly', 'true')
