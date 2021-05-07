from rest_framework.views import exception_handler
from django.http import HttpResponseRedirect
from django.utils.html import escape
from django.urls import NoReverseMatch, reverse


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    # Now add the HTTP status code to the response.

    if response is not None and response.status_code in [401, 403]:
        try:
            # login_url = request.build_absolute_uri(reverse('rest_framework:login'))
            login_url = reverse('rest_framework:login')
            request = context.get('request')
            next = escape(request.path)
            redirect_url = f"{login_url}?next={next}"

        except NoReverseMatch:
            redirect_url = '/'

        return HttpResponseRedirect(redirect_url)
    else:
        return response