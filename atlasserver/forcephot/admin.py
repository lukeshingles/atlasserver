from django.contrib import admin

# Register your models here.

from .models import Tasks

admin.site.register(Tasks)