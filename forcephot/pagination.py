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
from rest_framework.utils.urls import remove_query_param, replace_query_param


class TaskPagination(CursorPagination):
    cursor_query_param = 'cursor'
    ordering = ['id']
    template = 'rest_framework/pagination/older_and_newer.html'
    querysetcount = None
    pagefirsttaskposition = None  # with ordered tasks, the position of the first page time
    previous_is_top = False

    def paginate_queryset(self, queryset, request, view=None):
        queryset_full = queryset
        self.querysetcount = queryset.count()

        self.page_size = self.get_page_size(request)
        if not self.page_size:
            return None

        self.base_url = request.build_absolute_uri()
        self.ordering = self.get_ordering(request, queryset, view)

        self.cursor = self.decode_cursor(request)
        if self.cursor is None:
            (offset, reverse, current_position) = (0, False, None)
        else:
            (offset, reverse, current_position) = self.cursor

        # Cursor pagination always enforces an ordering.
        if reverse:
            queryset = queryset.order_by(*_reverse_ordering(self.ordering))
        else:
            queryset = queryset.order_by(*self.ordering)

        self.previous_is_top = False
        self.pagefirsttaskposition = 0
        # If we have a cursor with a fixed position then filter by that.
        if current_position is not None:
            order = self.ordering[0]
            is_reversed = order.startswith('-')
            order_attr = order.lstrip('-')

            # Test for: (cursor reversed) XOR (queryset reversed)
            if self.cursor.reverse != is_reversed:
                kwargs = {order_attr + '__lt': current_position}
            else:
                kwargs = {order_attr + '__gt': current_position}

            queryset = queryset.filter(**kwargs)

            if reverse:
                prev_records = queryset_full.filter(id__gt=int(current_position)).count() - self.page_size
                # print(prior_records, queryset[self.page_size:].count())
            else:
                prev_records = queryset_full.filter(id__gt=int(current_position) - 1).count()

            self.pagefirsttaskposition = prev_records
            if (prev_records <= self.page_size):
                self.previous_is_top = True

        # If we have an offset cursor then offset the entire page by that amount.
        # We also always fetch an extra item in order to determine if there is a
        # page following on from this one.
        results = list(queryset[offset:offset + self.page_size + 1])
        self.page = list(results[:self.page_size])

        # Determine the position of the final item following the page.
        if len(results) > len(self.page):
            has_following_position = True
            following_position = self._get_position_from_instance(results[-1], self.ordering)
        else:
            has_following_position = False
            following_position = None

        if reverse:
            # If we have a reverse queryset, then the query ordering was in reverse
            # so we need to reverse the items again before returning them to the user.
            self.page = list(reversed(self.page))

            # Determine next and previous positions for reverse cursors.
            self.has_next = (current_position is not None) or (offset > 0)
            self.has_previous = has_following_position

            if self.has_next:
                self.next_position = current_position
            if self.has_previous:
                self.previous_position = following_position
        else:
            # Determine next and previous positions for forward cursors.
            self.has_next = has_following_position
            self.has_previous = (current_position is not None) or (offset > 0)

            if self.has_next:
                self.next_position = following_position
            if self.has_previous:
                self.previous_position = current_position

        # Display page controls in the browsable API if there is more
        # than one page.
        if (self.has_previous or self.has_next) and self.template is not None:
            self.display_page_controls = True

        return self.page

    def get_previous_link(self):
        if self.previous_is_top:
            return remove_query_param(self.base_url, 'cursor')
        else:
            return super().get_previous_link()

    def get_paginated_response(self, data, headers=None):
        return Response(OrderedDict([
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('pagefirsttaskposition', self.pagefirsttaskposition),
            ('taskcount', self.querysetcount),
            ('results', data)
        ]), headers=headers)
