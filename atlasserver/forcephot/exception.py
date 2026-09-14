import typing as t

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseRedirect
from rest_framework.response import Response
from rest_framework.views import exception_handler

# renderers that produce a page for a person to look at, rather than a body for a program to parse.
# TemplateHTMLRenderer.format is "html" and BrowsableAPIRenderer.format is "api".
HTML_FORMATS = frozenset({"html", "api"})


def custom_exception_handler(exc: Exception, context: dict[str, t.Any]) -> Response | HttpResponseRedirect | None:
    """Send an unauthenticated browser to the login page, and leave every other response alone.

    Rewriting *every* 401/403 into a redirect made the API lie to its clients. An authenticated user
    acting on a task that is not theirs received a 302 to the login page rather than a 403, and
    jQuery follows redirects: the queue page then saw the login page with HTTP 200, ran its success
    handler, and reported a deletion that had not happened.
    """
    response = exception_handler(exc, context)
    if response is None or response.status_code not in {401, 403}:
        return response

    request = context.get("request")
    if request is None:
        return response

    # content negotiation runs before the permission checks, but not before every failure that can
    # reach here, so treat an unnegotiated request as a non-browser one
    wants_html = getattr(getattr(request, "accepted_renderer", None), "format", None) in HTML_FORMATS
    user = getattr(request, "user", None)
    if not wants_html or (user is not None and user.is_authenticated):
        # an authenticated user who is not allowed to do this needs to be told so, not offered a
        # login form for the account they are already using
        return response

    # the same helper login_required uses, so this sends the browser to settings.LOGIN_URL: the
    # site's login page, with its failed-login budget. It quotes the next URL rather than
    # HTML-escaping it, so an "&" in the query string survives the round trip.
    return redirect_to_login(request.get_full_path())
