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

# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
urlpatterns = [
    path("admin/", admin.site.urls),
    path("api-auth/", include((browsable_api_auth, "rest_framework"), namespace="rest_framework")),
    path("", include("atlasserver.forcephot.urls")),
    path("", include("django.contrib.auth.urls")),
    path("favicon.ico", RedirectView.as_view(url=staticfiles_storage.url("images/logos/atlas_logo.svg"))),
]
