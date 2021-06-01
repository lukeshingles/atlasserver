from django import template
from rest_framework.utils.urls import remove_query_param, replace_query_param
from django.utils.html import mark_safe

register = template.Library()


# the JavaScript will send a request with htmltaskframeonly=True, but the pagination links shouldn't keep this param
@register.filter
def removenewidqueryparam(value):
    return remove_query_param(value, 'newids')


@register.simple_tag()
def filterbuttons(request):
    fullpath = remove_query_param(remove_query_param(request.get_full_path(), 'cursor'), 'newids')
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
