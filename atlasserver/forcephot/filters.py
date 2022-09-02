from django_filters import rest_framework as filters

from atlasserver.forcephot.models import Task


class TaskFilter(filters.FilterSet):
    started = filters.BooleanFilter(field_name="starttimestamp", lookup_expr="isnull", exclude=True, label="Started")

    class Meta:
        model = Task
        fields = ("started",)
