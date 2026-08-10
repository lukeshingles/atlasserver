"""atlasserver URL Configuration.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/

Examples
--------
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
from django.conf import settings
from django.contrib import admin
from django.urls import include
from django.urls import path
from django.urls import re_path
from django.views.generic.base import TemplateView
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework import routers

from atlasserver.forcephot import views

# Routers provide an easy way of automatically determining the URL conf.
router = routers.DefaultRouter()
router.register(r"queue", views.ForcePhotTaskViewSet)

admin.site.site_url = f"{settings.PATHPREFIX}/"
admin.site.site_header = "ATLAS Forced Photometry Admin"
admin.site.site_title = "ATLAS Forced Photometry"

# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
urlpatterns = [
    path("", TemplateView.as_view(template_name="index.html"), name="index"),
    path("", include(router.urls)),
    path("queue/<int:pk>/requestimages/", views.RequestImages.as_view(), name="requestimages"),
    re_path(r"^register/$", views.register, name="register"),
    path("emailchange/", views.change_email, name="email_change"),
    path("apitoken/", views.api_token, name="apitoken"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path("resendverification/", views.resend_verification, name="resend_verification"),
    path("emailchange/confirm/<token>/", views.confirm_email_change, name="email_change_confirm"),
    path("faq/", TemplateView.as_view(template_name="faq.html", extra_context={"name": "FAQ"}), name="faq"),
    path(
        "resultdesc/",
        TemplateView.as_view(template_name="resultdesc.html", extra_context={"name": "Output Description"}),
        name="resultdesc",
    ),
    path("queuepositions.json", views.queuepositions, name="queuepositions"),
    path("taskrunnerstatus.json", views.taskrunnerstatus, name="taskrunnerstatus"),
    path("stats/", views.stats, name="stats"),
    path("stats/shortterm.html", views.statsshortterm, name="statsshortterm"),
    path("stats/longterm.html", views.statslongterm, name="statslongterm"),
    path("stats/coordchart.json", views.statscoordchart, name="statscoordchart"),
    path("stats/usagechart.json", views.statsusagechart, name="statsusagechart"),
    path("queue/<int:taskid>/preview.jpg", views.taskpreviewimage, name="taskpreviewimage"),
    path("queue/<int:taskid>/data.txt", views.taskresultdata, name="taskresultdata"),
    path("queue/<int:taskid>/resultplotdata.js", views.resultplotdatajs, name="resultplotdatajs"),
    path("queue/<int:taskid>/plot.pdf", views.taskpdfplot, name="taskpdfplot"),
    path("queue/<int:taskid>/images.zip", views.taskimagezip, name="taskimagezip"),
    path(
        "apiguide/",
        TemplateView.as_view(template_name="apiguide.html", extra_context={"name": "API Guide"}),
        name="apiguide",
    ),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api-token-auth/", rest_framework.authtoken.views.obtain_auth_token),
    path("robots.txt", TemplateView.as_view(template_name="robots.txt", content_type="text/plain")),
]
