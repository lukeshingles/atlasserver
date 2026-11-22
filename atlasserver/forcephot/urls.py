"""The URL configuration of the forcephot app: the task API, the account pages, and the stats and result views."""

from django.conf import settings
from django.contrib import admin
from django.urls import include
from django.urls import path
from django.urls import re_path
from django.views.generic.base import TemplateView
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework import routers

from atlasserver.forcephot import queue as taskqueue
from atlasserver.forcephot import views
from atlasserver.forcephot.login import ObtainAuthTokenThrottled
from atlasserver.forcephot.login import ThrottledAdminAuthenticationForm
from atlasserver.forcephot.login import ThrottledLoginView
from atlasserver.forcephot.login import ThrottledPasswordChangeView
from atlasserver.taskrunner import status as runnerstatus

router = routers.DefaultRouter()
router.register(r"queue", views.ForcePhotTaskViewSet)

admin.site.site_url = f"{settings.PATHPREFIX}/"
# read at request time by AdminSite.login, so the admin's password check shares the budget
admin.site.login_form = ThrottledAdminAuthenticationForm
admin.site.site_header = "ATLAS Forced Photometry Admin"
admin.site.site_title = "ATLAS Forced Photometry"

# The numbers in the text about the queue on the FAQ page and in the API guide, from the constants
# that the queue uses.
QUEUE_CONTEXT = {
    "numslots": runnerstatus.NUMSLOTS,
    "max_user_slots": runnerstatus.MAX_USER_SLOTS,
    "usage_half_life_days": taskqueue.USAGE_HALF_LIFE_DAYS,
    "wait_credit_minutes": round(taskqueue.WAIT_CREDIT * 60),
    "new_account_days": taskqueue.NEW_ACCOUNT_DAYS,
}

urlpatterns = [
    path("", TemplateView.as_view(template_name="index.html"), name="index"),
    path("", include(router.urls)),
    path("queue/<int:pk>/requestimages/", views.RequestImages.as_view(), name="requestimages"),
    re_path(r"^register/$", views.register, name="register"),
    # Before django.contrib.auth.urls, which atlasserver.urls includes after this module: the
    # stock view builds the mailed link from the Host header. See SiteOriginPasswordResetView.
    path("password_reset/", views.SiteOriginPasswordResetView.as_view(), name="password_reset"),
    # likewise before django.contrib.auth.urls: the stock view has no limit on password guesses
    path("login/", ThrottledLoginView.as_view(), name="login"),
    path("password_change/", ThrottledPasswordChangeView.as_view(), name="password_change"),
    path("emailchange/", views.change_email, name="email_change"),
    path("apitoken/", views.api_token, name="apitoken"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path("resendverification/", views.resend_verification, name="resend_verification"),
    path("emailchange/confirm/<token>/", views.confirm_email_change, name="email_change_confirm"),
    path(
        "faq/",
        TemplateView.as_view(template_name="faq.html", extra_context={"name": "FAQ", **QUEUE_CONTEXT}),
        name="faq",
    ),
    path(
        "resultdesc/",
        TemplateView.as_view(template_name="resultdesc.html", extra_context={"name": "Output Description"}),
        name="resultdesc",
    ),
    path("queuepositions.json", views.QueuePositions.as_view(), name="queuepositions"),
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
        TemplateView.as_view(template_name="apiguide.html", extra_context={"name": "API Guide", **QUEUE_CONTEXT}),
        name="apiguide",
    ),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api-token-auth/", ObtainAuthTokenThrottled.as_view(), name="api-token-auth"),
    path("robots.txt", TemplateView.as_view(template_name="robots.txt", content_type="text/plain")),
]
