"""atlasserver URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

import rest_framework.authtoken.views
from django.conf.urls import url
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
# from django.contrib.auth.models import User
# from django.contrib.auth.decorators import login_required
from rest_framework import routers, serializers, viewsets

from forcephot import views

# Routers provide an easy way of automatically determining the URL conf.
router = routers.DefaultRouter()
router.register(r'queue', views.ForcePhotTaskViewSet)

admin.site.site_url = settings.PATHPREFIX + '/'
admin.site.site_header = "ATLAS Forced Photometry Admin"
admin.site.site_title = "ATLAS Forced Photometry"

# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
urlpatterns = [
    path('', views.index, name="index"),
    path('', include(router.urls)),
    path('queue/delete/<str:pk>/', views.deleteTask, name="delete"),
    url(r'^register/$', views.register, name='register'),
    path('resultdesc/', views.resultdesc, name="resultdesc"),

    path('taskboxhtml/', views.taskboxhtml),
    path('taskboxhtml/<int:taskid>/', views.taskboxhtml, name="taskboxhtml"),
    path('taskboxhtml/newsince/<int:taskid>/', views.taskboxhtml, {"type": "maxid"}, name="taskboxhtml_newsince_taskid"),
    path('resultdatajs/<int:taskid>/', views.resultdatajs, name='resultdatajs'),

    path('apiguide/', views.apiguide, name="apiguide"),
    path('api-token-auth/', rest_framework.authtoken.views.obtain_auth_token),
]
