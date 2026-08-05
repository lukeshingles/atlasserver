from django.contrib import admin

import atlasserver.forcephot.models

# Register your models here.


class CustomAdmin(admin.ModelAdmin):
    # the changelist renders Task.__str__, which reads task.user — a lazy load the admin's own
    # foreign-key detection cannot see, costing one User query per row without this
    list_select_related = ["user"]

    # readonly_fields = ('parent_task',)

    # make all fields read-only
    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(atlasserver.forcephot.models.Task, CustomAdmin)
