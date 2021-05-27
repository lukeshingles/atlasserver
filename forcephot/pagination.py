from base64 import b64decode, b64encode
from collections import OrderedDict, namedtuple
from urllib import parse

from django.core.paginator import InvalidPage
from django.core.paginator import Paginator as DjangoPaginator
from django.template import loader
from django.utils.encoding import force_str
from django.utils.translation import gettext_lazy as _
from rest_framework.pagination import CursorPagination, _positive_int, Cursor, _reverse_ordering, replace_query_param
from rest_framework.compat import coreapi, coreschema
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.settings import api_settings


class TaskPagination(CursorPagination):
    cursor_query_param = 'cursor'
    ordering = ['-id']
    template = 'rest_framework/pagination/older_and_newer.html'
    querysetcount = 0

    def paginate_queryset(self, queryset, request, view=None):
        self.querysetcount = queryset.count()
        return super().paginate_queryset(queryset=queryset, request=request, view=view)

    def get_paginated_response(self, data):
        return Response(OrderedDict([
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('taskcount', self.querysetcount),
            ('results', data)
        ]))
