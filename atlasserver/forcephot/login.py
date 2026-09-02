"""The doors of the site that take a password: the login page, the admin's, and the token endpoint.

Each applies the failed-login budget of throttles, which counts wrong passwords per client address
and refuses the check itself once the address is over budget. A Basic header on any API view is
metered there too, by ThrottledBasicAuthentication.
"""

import typing as t
from typing import override

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.http import HttpResponse
from rest_framework import serializers
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.exceptions import Throttled
from rest_framework.request import Request
from rest_framework.response import Response

from atlasserver.forcephot.throttles import ForcedPhotRateThrottle
from atlasserver.forcephot.throttles import LOGIN_FAILURE_WINDOW_SECONDS
from atlasserver.forcephot.throttles import login_failures_exceeded
from atlasserver.forcephot.throttles import LOGIN_LIMIT_MESSAGE
from atlasserver.forcephot.throttles import note_login_failure
from atlasserver.forcephot.throttles import password_was_wrong


class LoginFailureLimitMixin(AuthenticationForm):
    """Refuse the password check once an address is over budget, and count each wrong password.

    Refused before the check: an over-budget address must not learn whether the password was
    right. The error carries the code "throttled", which ThrottledLoginView turns into a 429.

    An AuthenticationForm subclass rather than a bare mixin, so that super().clean() resolves for
    the type checkers; in the two forms below it still reaches the admin's clean() first.
    """

    @override
    def clean(self) -> dict[str, t.Any]:
        if self.request is not None and login_failures_exceeded(self.request):
            raise forms.ValidationError(LOGIN_LIMIT_MESSAGE, code="throttled")

        try:
            return super().clean()
        except forms.ValidationError:
            if self.request is not None and self.password_was_wrong():
                note_login_failure(self.request)
            raise

    def password_was_wrong(self) -> bool:
        """Return whether the refusal was for the password; see throttles.password_was_wrong."""
        if getattr(self, "user_cache", None) is not None:
            # the password checked out, and the account was refused after that
            return False

        return password_was_wrong(self.cleaned_data.get("username"), self.cleaned_data.get("password"))


class ThrottledAuthenticationForm(LoginFailureLimitMixin, AuthenticationForm):
    """The login page's form."""


class ThrottledAdminAuthenticationForm(LoginFailureLimitMixin, AdminAuthenticationForm):
    """The admin login's form; the admin reads site.login_form at request time."""


class ThrottledLoginView(LoginView):
    """Django's login page, answering a refused check with 429 and a Retry-After like every other limit."""

    authentication_form = ThrottledAuthenticationForm

    @override
    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        refused = any(error.code == "throttled" for error in form.non_field_errors().as_data())
        response = self.render_to_response(self.get_context_data(form=form), status=429 if refused else 200)
        if refused:
            response["Retry-After"] = str(int(LOGIN_FAILURE_WINDOW_SECONDS))

        return response


class ObtainAuthTokenThrottled(ObtainAuthToken):
    """DRF's token endpoint, with the rate limit its base class removes and the failed-login budget.

    ObtainAuthToken sets throttle_classes = (), so the one endpoint that returns a token that does
    not expire was the one unmetered endpoint. No authenticators, because APIView.initial()
    authenticates before it throttles: a wrong password in an Authorization header was answered
    401 before the throttle ran, so one extra header turned the limit off. Nothing here reads
    request.user, and dropping SessionAuthentication drops only a CSRF check that a view acting on
    the credentials in its own body does not need.
    """

    authentication_classes = ()
    throttle_classes = [ForcedPhotRateThrottle]
    throttle_scope = "forcephotlogin"

    @override
    def post(self, request: Request, *args: t.Any, **kwargs: t.Any) -> Response:
        if login_failures_exceeded(request):
            raise Throttled(detail=LOGIN_LIMIT_MESSAGE)

        try:
            return super().post(request, *args, **kwargs)
        except serializers.ValidationError as ex:
            # only a wrong password counts. The serializer marks a refused login with the code
            # "authorization", for a wrong password and a refused account alike; a body with no
            # password fails earlier, with another code.
            codes = ex.get_codes()
            fieldcodes = codes.get("non_field_errors") if isinstance(codes, dict) else None
            body: dict[str, t.Any] = request.data if isinstance(request.data, dict) else {}
            if (
                isinstance(fieldcodes, list)
                and "authorization" in fieldcodes
                and password_was_wrong(body.get("username"), body.get("password"))
            ):
                note_login_failure(request)
            raise
