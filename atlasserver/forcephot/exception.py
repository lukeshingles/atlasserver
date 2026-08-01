import urllib.parse

from django.http import HttpResponseRedirect
from django.urls import NoReverseMatch
from django.urls import reverse
from rest_framework.views import exception_handler

# renderers that produce a page for a person to look at, rather than a body for a program to parse.
# TemplateHTMLRenderer.format is "html" and BrowsableAPIRenderer.format is "api".
HTML_FORMATS = frozenset({"html", "api"})


def custom_exception_handler(exc, context):
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

    try:
        login_url = reverse("rest_framework:login")
    except NoReverseMatch:
        return HttpResponseRedirect("/")

    # quote, not escape: this is a URL, and HTML-escaping an "&" in the path into "&amp;" would send
    # the user somewhere other than where they asked to go
    nexturl = urllib.parse.quote(request.get_full_path())

    return HttpResponseRedirect(f"{login_url}?next={nexturl}")
