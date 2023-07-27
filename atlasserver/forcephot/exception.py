from django.http import HttpResponseRedirect
from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils.html import escape
from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None or response.status_code not in [401, 403]:
        return response

    try:
        # login_url = request.build_absolute_uri(reverse('rest_framework:login'))
        login_url = reverse("rest_framework:login")
        request = context.get("request")
        nexturl = escape(request.path)
        redirect_url = f"{login_url}?next={nexturl}"

    except NoReverseMatch:
        redirect_url = "/"

    return HttpResponseRedirect(redirect_url)
