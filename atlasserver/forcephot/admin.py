import datetime as dt
import typing as t
from typing import override

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.helpers import ActionForm
from django.contrib.admin.models import CHANGE
from django.contrib.admin.models import LogEntry
from django.contrib.admin.widgets import AutocompleteSelectMultiple

# this import also registers the stock admin classes of User and Group. atlasserver.forcephot
# comes before django.contrib.auth in INSTALLED_APPS, so without it the unregister calls below
# find no User and no Group.
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group
from django.contrib.auth.models import User
from django.db.models import Case
from django.db.models import Count
from django.db.models import F
from django.db.models import FloatField
from django.db.models import IntegerField
from django.db.models import Model
from django.db.models import OuterRef
from django.db.models import Subquery
from django.db.models import Sum
from django.db.models import Value
from django.db.models import When
from django.db.models.functions import Coalesce

import atlasserver.forcephot.models
from atlasserver.forcephot.models import Task
from atlasserver.forcephot.models import UsageDay
from atlasserver.forcephot.queue import HIGH_PRIORITY_GROUP
from atlasserver.forcephot.queue import RECALC_MAX_INTERVAL_SECONDS
from atlasserver.forcephot.queue import USAGE_HALF_LIFE_DAYS
from atlasserver.forcephot.queue import usage_records
from atlasserver.forcephot.queue import USAGE_RETENTION_DAYS
from atlasserver.forcephot.queue import usage_weight
from atlasserver.forcephot.queue import utcnow

if t.TYPE_CHECKING:
    from django.http import HttpRequest


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
    The widget gets the users from the search of the user admin. Thus the page holds only the
    current members, and not the full table of users. The source field of the widget is the
    foreign key to the user in the table that joins users and groups.
    """

    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=AutocompleteSelectMultiple(
            User.groups.through._meta.get_field("user"),  # noqa: SLF001
            admin.site,
        ),
        # This form is the form of each group, and only one group has an effect on the queue.
        help_text=(
            "Search by username, name or email address. "
            f"A change of the members of the group '{HIGH_PRIORITY_GROUP}' has an effect on the queue "
            f"in a maximum of {RECALC_MAX_INTERVAL_SECONDS:.0f} seconds."
        ),
    )

    class Meta:
        model = Group
        fields = "__all__"  # noqa: DJ007

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Show the current members of a group that is in the database."""
        super().__init__(*args, **kwargs)
        if self.instance.pk is not None and "members" in self.fields:
            self.fields["members"].initial = self.instance.user_set.all()


class GroupWithMembersAdmin(GroupAdmin):
    form = GroupMembersForm

    def can_change_members(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Return whether this user can change the members of a group.

        A group gives its permissions to its members. Thus a change of the members is a change of
        the users, and it needs the permission to change users as well as the permission to change
        the group. Without this rule, a staff user who can change only groups can put permissions
        in a group, join the group, and get those permissions.
        """
        user: t.Any = request.user
        return self.has_change_permission(request, obj) and bool(user.has_perm("auth.change_user"))

    @override
    def get_form(
        self, request: HttpRequest, obj: t.Any = None, change: bool = False, **kwargs: t.Any
    ) -> type[forms.ModelForm]:  # pyrefly: ignore [implicit-any-type-argument]
        form: t.Any = super().get_form(request, obj, change=change, **kwargs)
        if self.can_change_members(request, obj):
            return form
        # A declared field with the value None is not a field of the subclass. The field must be
        # absent and not only hidden: a form that has the field and gets no value for it removes
        # all the members.
        return type(form.__name__, (form,), {"members": None})

    @override
    def save_related(self, request: HttpRequest, form: t.Any, formsets: t.Any, change: bool) -> None:
        super().save_related(request, form, formsets, change)
        if "members" in form.cleaned_data:
            form.instance.user_set.set(form.cleaned_data["members"])


admin.site.unregister(Group)
admin.site.register(Group, GroupWithMembersAdmin)


class GroupActionForm(ActionForm):
    # the changelist shows this field next to the action list. The field is optional, because the
    # other actions (e.g. the delete action) do not use a group.
    group = forms.ModelChoiceField(queryset=Group.objects.all(), required=False, label="Group:")


# pyrefly: ignore [implicit-any-type-argument]
# see CustomAdmin: UserAdmin has no __class_getitem__ at runtime either
class GroupUserAdmin(UserAdmin):
    action_form = GroupActionForm
    actions = ["add_to_group"]

    # The usage and the queued tasks of each user. The usage sets the place of the tasks of the
    # user in the queue; see forcephot.queue.calculate_queue_positions. A click on the header of
    # one of these columns sorts the list by that column.
    @override
    def get_list_display(self, request: HttpRequest) -> t.Any:
        return (*super().get_list_display(request), "usage_runtime", "usage_decayed", "queued_tasks")

    @override
    def get_queryset(self, request: HttpRequest) -> t.Any:
        # The values of the three columns come with the row of the user. A query for each row of
        # the list is thus not necessary, and the database can sort the list by each column.
        today = utcnow().date()

        def of_user(rows: t.Any, total: t.Any, zero: float) -> Coalesce:
            # the subquery has no row for a user with no row in `rows`, and the value is then zero
            totals = rows.filter(user=OuterRef("pk")).values("user").annotate(total=total).values("total")
            return Coalesce(Subquery(totals), Value(zero), output_field=total.output_field)

        # The weight of a usage record is a function of its day, and the usage records are of a
        # small number of days. The weights thus come from the function that the queue order
        # uses, and the database does no date arithmetic.
        weight = Case(
            *(
                When(day=today - dt.timedelta(days=agedays), then=Value(usage_weight(agedays)))
                for agedays in range(USAGE_RETENTION_DAYS + 1)
            ),
            default=Value(0.0),
            output_field=FloatField(),
        )
        firstday = today - dt.timedelta(days=USAGE_HALF_LIFE_DAYS - 1)
        return (
            super()
            .get_queryset(request)
            .annotate(
                usage_runtime_sum=of_user(
                    UsageDay.objects.filter(day__gte=firstday), Sum("seconds", output_field=FloatField()), 0.0
                ),
                usage_decayed_sum=of_user(usage_records(), Sum(F("seconds") * weight, output_field=FloatField()), 0.0),
                queued_tasks_count=of_user(Task.queued(), Count("pk", output_field=IntegerField()), 0),
            )
        )

    @admin.display(description=f"Run time ({USAGE_HALF_LIFE_DAYS} days)", ordering="usage_runtime_sum")
    def usage_runtime(self, user: t.Any) -> str:
        return f"{user.usage_runtime_sum / 3600:.1f} h"

    # the usage that the queue order uses: older run time counts less
    @admin.display(description="Usage in the queue", ordering="usage_decayed_sum")
    def usage_decayed(self, user: t.Any) -> str:
        return f"{user.usage_decayed_sum / 3600:.1f} h"

    # The tasks of the user that wait or run. The Queue badge of the navigation bar shows the same
    # number to the user.
    @admin.display(description="Queued tasks", ordering="queued_tasks_count")
    def queued_tasks(self, user: t.Any) -> int:
        return user.queued_tasks_count

    # A group gives its permissions to its members, so the action needs the permission to change
    # users. A staff member who can only view the users does not get the action.
    @admin.action(permissions=["change"], description="Add the selected users to the group")
    def add_to_group(self, request: HttpRequest, queryset: t.Any) -> None:
        # Django validates the action form before it calls an action. Thus the value is the key of
        # a group, or it is empty.
        key = request.POST.get("group")
        group: t.Any = Group.objects.filter(pk=key).first() if key else None
        if group is None:
            self.message_user(request, "Select a group. The action changed no user.", messages.ERROR)
            return

        new_members = list(queryset.exclude(groups=group))
        group.user_set.add(*new_members)
        # the history page of each new member shows the administrator and the group
        administrator: t.Any = request.user
        LogEntry.objects.log_actions(
            administrator.pk, new_members, CHANGE, change_message=f"Added to the group '{group.name}'."
        )
        self.message_user(
            request,
            f"The group '{group.name}' contains all selected users. New members: {len(new_members)}.",
            messages.SUCCESS,
        )


admin.site.unregister(User)
admin.site.register(User, GroupUserAdmin)
