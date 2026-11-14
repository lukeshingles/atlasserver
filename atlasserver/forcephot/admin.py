import typing as t
from typing import override

from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import AutocompleteSelectMultiple
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.models import Group
from django.contrib.auth.models import User
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


# pyrefly: ignore [implicit-any-type-argument]
# ModelForm is generic only in django-stubs, as ModelAdmin is above.
class GroupMembersForm(forms.ModelForm):
    """The form of a group, with a field for the users who are its members.

    The stock form of a group has no such field: the members are the reverse side of User.groups.
    The widget gets the users from the search of the user admin, and thus the page holds only the
    current members and not the full table of users. The source field of the widget is the foreign
    key to the user in the table that joins users and groups.
    """

    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=AutocompleteSelectMultiple(
            User.groups.through._meta.get_field("user"),  # noqa: SLF001
            admin.site,
        ),
        help_text="Search by username, name or email address. A change has an effect on the queue in a maximum of "
        "30 seconds.",
    )

    class Meta:
        model = Group
        fields = "__all__"  # noqa: DJ007

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Show the current members of a group that is in the database."""
        super().__init__(*args, **kwargs)
        if self.instance.pk is not None:
            self.fields["members"].initial = self.instance.user_set.all()


class GroupWithMembersAdmin(GroupAdmin):
    form = GroupMembersForm

    @override
    def save_related(self, request: HttpRequest, form: t.Any, formsets: t.Any, change: bool) -> None:
        super().save_related(request, form, formsets, change)
        form.instance.user_set.set(form.cleaned_data["members"])


admin.site.unregister(Group)
admin.site.register(Group, GroupWithMembersAdmin)
