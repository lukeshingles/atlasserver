from django.contrib import admin

import atlasserver.forcephot.models

# Register your models here.


class CustomAdmin(admin.ModelAdmin):
    # readonly_fields = ('parent_task',)

    # make all fields read-only
    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(atlasserver.forcephot.models.Task, CustomAdmin)
