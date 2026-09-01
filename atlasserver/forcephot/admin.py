import typing as t
from typing import override

from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

import atlasserver.forcephot.models

# Register your models here.


# pyrefly: ignore [implicit-any-type-argument]
# ModelAdmin is generic only in django-stubs; at runtime it has no __class_getitem__, so
# ModelAdmin[Task] as a base class raises TypeError on import. django-stubs-ext exists to
# monkeypatch that, but it is not worth a runtime dependency for one class.
class CustomAdmin(admin.ModelAdmin):
    # the changelist renders Task.__str__, which reads task.user — a lazy load the admin's own
    # foreign-key detection cannot see, costing one User query per row without this
    list_select_related = ["user"]

    # make all fields read-only
    @override
    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    # Tasks come from the API and the queue page, which give them a queue position and the
    # fields the runner dispatches on. A row added here has neither, and a NULL queue position
    # sorts ahead of every task that is waiting.
    @override
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    # Django's bulk action calls queryset.delete(), which never reaches Task.delete(): the rows
    # went while their result files and cached plot data stayed, and a finished task was removed
    # where every other path archives it.
    @override
    def delete_queryset(self, request: HttpRequest, queryset: t.Any) -> None:
        for task in queryset:
            task.delete()


admin.site.register(atlasserver.forcephot.models.Task, CustomAdmin)
