"""The root URL configuration: the admin, the login of the browsable API, the app, and the auth views."""

from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.contrib.staticfiles.storage import staticfiles_storage
from django.urls import include
from django.urls import path
from django.views.generic.base import RedirectView

from atlasserver.forcephot.login import ThrottledLoginView

# The browsable API's login and logout, under the names rest_framework.urls gives them, because
# DRF's browsable API reverses "rest_framework:login". Not that module itself: it serves the stock
# LoginView with the stock AuthenticationForm, which was one password door with no failed-login
# budget while every other door had one.
browsable_api_auth = [
    path("login/", ThrottledLoginView.as_view(template_name="rest_framework/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api-auth/", include((browsable_api_auth, "rest_framework"), namespace="rest_framework")),
    path("", include("atlasserver.forcephot.urls")),
    path("", include("django.contrib.auth.urls")),
    path("favicon.ico", RedirectView.as_view(url=staticfiles_storage.url("images/logos/atlas_logo.svg"))),
]
