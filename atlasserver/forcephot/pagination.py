from collections import OrderedDict

from rest_framework.pagination import _reverse_ordering
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.utils.urls import remove_query_param


class TaskPagination(CursorPagination):
    cursor_query_param = "cursor"
    ordering = ["id"]
    template = "rest_framework/pagination/older_and_newer.html"
    page_size_query_param = "pagesize"
    # without a bound, ?pagesize=100000 serialises a user's entire task history in one response
    max_page_size = 100
    querysetcount: int | None = None
    pagefirsttaskposition: int | None = None  # with ordered tasks, the position of the first page time
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

        order = self.ordering[0]
        is_reversed = order.startswith("-")
        order_attr = order.lstrip("-")

        # If we have a cursor with a fixed position then filter by that.
        if current_position is not None:
            # Test for: (cursor reversed) XOR (queryset reversed)
            if self.cursor.reverse != is_reversed:
                kwargs = {f"{order_attr}__lt": current_position}
            else:
                kwargs = {f"{order_attr}__gt": current_position}

            queryset = queryset.filter(**kwargs)

        # If we have an offset cursor then offset the entire page by that amount.
        # We also always fetch an extra item in order to determine if there is a
        # page following on from this one.
        results = list(queryset[offset : offset + self.page_size + 1])
        self.page = list(results[: self.page_size])

        # Count the tasks displayed before this page. The comparison follows the ordering in use
        # (which is not necessarily by id, see the view's ordering_fields), and current_position
        # is passed through as the string that decode_cursor produced. The cursor offset is how
        # DRF pages through rows that share an ordering value (every task from one radeclist
        # submission has the same timestamp), so those rows precede this page as well.
        prev_records = offset
        if current_position is not None:
            if is_reversed:  # e.g. "-id": later values are shown first
                strictlybefore = {f"{order_attr}__gt": current_position}
                beforeorat = {f"{order_attr}__gte": current_position}
            else:
                strictlybefore = {f"{order_attr}__lt": current_position}
                beforeorat = {f"{order_attr}__lte": current_position}

            if reverse:
                # a reverse cursor page ends just before current_position, so the rows on this
                # page are part of the "before" count and have to be subtracted again
                prev_records += max(0, queryset_full.filter(**strictlybefore).count() - len(self.page))
            else:
                prev_records += queryset_full.filter(**beforeorat).count()

        self.pagefirsttaskposition = prev_records
        if current_position is not None and prev_records <= self.page_size:
            self.previous_is_top = True

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
        # has_previous must be checked too, otherwise the first page can advertise a link to itself
        if self.previous_is_top and self.has_previous:
            return remove_query_param(self.base_url, "cursor")
        return super().get_previous_link()

    def get_paginated_response(self, data, headers=None):
        return Response(
            OrderedDict(
                [
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("pagefirsttaskposition", self.pagefirsttaskposition),
                    ("taskcount", self.querysetcount),
                    ("results", data),
                ]
            ),
            headers=headers,
        )
