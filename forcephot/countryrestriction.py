from django.core.mail import EmailMessage
from django.http import HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin
from django.conf import settings
from django.contrib.auth import logout


class CountryRestrictionMiddleware(MiddlewareMixin):
    """
    Restrict access to users that are not in an allowed country.
    """

    def __init__(self, *args, **kwargs):
        if MiddlewareMixin != object:
            super(CountryRestrictionMiddleware, self).__init__(*args, **kwargs)

    def process_request(self, request):
        block_message = None

        country_code = request.geo_data['country_code']
        if country_code in ['BY, 'RU']:
            block_message = f"Forbidden country: {country_code}"
            log_message = f"Forbidden country: {country_code}\n"

        if hasattr(request.user, 'email'):
            if request.user.email.endswith('.ru'):
                block_message = "Forbidden country: RU"
                log_message = "Forbidden email country blocked"
            elif request.user.email.endswith('.by'):
                block_message = "Forbidden country: BY"
                log_message = "Forbidden email country blocked"

        if block_message:
            userdata = {'user': str(request.user)}
            userprops = ['username', 'email']
            for attr in userprops:
                userdata[attr] = getattr(request.user, attr, "None")

            message = EmailMessage(
                subject='Geo blocked request',
                body=(
                    f'{log_message}\n'
                    f'Geo data: {request.geo_data}\n'
                    f'User: {userdata}\n'
                    f'Request: {request}\n'
                ),
                from_email=settings.EMAIL_HOST_USER,
                to=['luke.shingles@gmail.com'],
            )

            message.send()
            logout(request)
            return HttpResponseForbidden(block_message)

        return None
