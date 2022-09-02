from django.contrib import admin

import atlasserver.forcephot.models

# Register your models here.


# admin.site.register(forcephot.models.Task)


class CustomAdmin(admin.ModelAdmin):
    # readonly_fields = ('parent_task',)

    # make all fields read-only
    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(atlasserver.forcephot.models.Task, CustomAdmin)
