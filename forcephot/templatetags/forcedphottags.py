from django import template
from django.contrib.humanize.templatetags import humanize
from rest_framework.utils.urls import remove_query_param, replace_query_param
from django.utils.html import mark_safe
import datetime

register = template.Library()


@register.filter
def tasktimesince(value):
    seconds = (datetime.datetime.now(datetime.timezone.utc) - value).total_seconds()
    if seconds < 350:
        return f'{seconds:.0f} seconds ago'

    return humanize.naturaltime(value)


# the JavaScript will send a request with htmltaskframeonly=True, but the pagination links shouldn't keep this param
@register.filter
def removetaskboxqueryparam(value):
    return remove_query_param(value, 'htmltaskframeonly')


@register.filter
def addtaskboxqueryparam(value):
    print(replace_query_param(value, 'htmltaskframeonly', 'true'))
    return replace_query_param(value, 'htmltaskframeonly', 'true')


@register.simple_tag()
def filterbuttons(request):
    fullpath = remove_query_param(request.get_full_path(), 'cursor')
    strhtml = '<ul id="taskfilters">'
    links = [
        (remove_query_param(fullpath, 'started'), 'All tasks'),
        (replace_query_param(fullpath, 'started', 'true'), 'Running/Finished</a></li>'),
    ]
    for href, label in links:
        strclass = 'btn-primary' if fullpath == href else 'btn-link'
        strhtml += f'<li><a href="{href}" class="btn {strclass}">{label}</a></li>'
    strhtml += '</ul>'

    return mark_safe(strhtml)
