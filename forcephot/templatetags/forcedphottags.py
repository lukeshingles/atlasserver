from django import template
from django.contrib.humanize.templatetags import humanize
from rest_framework.utils.urls import remove_query_param, replace_query_param
from django.utils.html import mark_safe
from django.urls import reverse
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


@register.simple_tag()
def get_or_request_imagezip(task):
    strhtml = ''
    if task.request_type == 'IMGZIP':
        if task.localresultimagezipfile():
            # direct link to zip file
            url = reverse('taskimagezip', args=(task.parent_task_id,))
            strhtml = f'<a class="results btn btn-info" href="{url}">Download images (ZIP)</a>'
    else:
        imgreq_taskid = task.imagerequesttaskid()
        if imgreq_taskid:
            url = reverse('task-detail', args=(imgreq_taskid,))
            if task.localresultimagezipfile():
                strhtml = f'<a class="btn btn-primary" href="{url}">Images ready</a>'
            else:
                strhtml = f'<a class="btn btn-warning" href="{url}">Images requested</a>'
        else:
            url = reverse('requestimages', args=(task.id,))
            strhtml = f'<a class="btn btn-info" href="{url}" title="Download FITS and JPEG images for up to the first 500 observations.">Request {"reduced" if task.use_reduced else "difference"} images</a>'

    return mark_safe(strhtml)
