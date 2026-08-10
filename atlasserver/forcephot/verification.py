"""Email address verification for new registrations.

Registration used to log the new account straight in without proving the address belonged to
whoever typed it, so the address that receives job completion mail (and password resets) was
unverified. Anyone could sign up as someone else's address, and the owner's only sign would be
unexpected mail from a service they had never used.

Django ships no verification flow, but it ships the pieces: the same signed-token machinery the
password reset views use. Reusing it means there is no new model, no new column and nothing to
expire by hand -- the token carries its own timestamp, and PASSWORD_RESET_TIMEOUT bounds it.
"""

import typing as t

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import EmailMessage
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode
from django.utils.http import urlsafe_base64_encode


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """Tokens for confirming an email address.

    Its own generator rather than default_token_generator, so that a verification link cannot be
    replayed as a password reset link or the other way round: the two have different consequences,
    and the key salt is what keeps them apart.

    is_active is in the hash, so a token stops working the moment it is used -- activation flips
    the flag, which changes the hash. That makes the link single-use without storing anything.
    """

    key_salt = "atlasserver.forcephot.verification.EmailVerificationTokenGenerator"

    # user is Any because the type checkers disagree: django-stubs declares these hooks against
    # the concrete User, while ty reads Django's own source, where they are AbstractBaseUser.
    def _make_hash_value(self, user: t.Any, timestamp: int) -> str:
        # email as well, so that changing the address invalidates any outstanding link for the old
        # one. The base implementation covers pk, password and last_login.
        return f"{super()._make_hash_value(user, timestamp)}{user.is_active}{user.email}"


token_generator = EmailVerificationTokenGenerator()


def verification_url(request: HttpRequest, user: t.Any) -> str:
    """Return the absolute URL that verifies this user's current email address."""
    from django.urls import reverse

    path = reverse(
        "verify_email",
        kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": token_generator.make_token(user)},
    )

    # built from the request rather than from a configured site name, like the password reset mail
    # already is. ALLOWED_HOSTS is pinned (not "*") precisely so that a forged Host header cannot
    # turn this into a link to somebody else's server.
    return request.build_absolute_uri(path)


def send_verification_email(request: HttpRequest, user: t.Any) -> None:
    """Mail the user a link that confirms their address."""
    body = render_to_string(
        "registration/verification_email.txt",
        {"user": user, "verification_url": verification_url(request, user), "site_name": request.get_host()},
    )

    EmailMessage(
        subject=f"Verify your email address for {request.get_host()}",
        body=body,
        to=[user.email],
    ).send(fail_silently=False)


# Salt for the email-change tokens below. Distinct from the activation generator's, so neither
# kind of link can be presented as the other.
EMAIL_CHANGE_SALT: t.Final = "atlasserver.forcephot.verification.email_change"


def send_email_change_confirmation(request: HttpRequest, user: t.Any, new_email: str) -> None:
    """Mail a confirmation link to the address a user wants to move to.

    The pending address travels inside a signed token rather than being written to the user row or
    to a new table: nothing is changed until the link is followed, so an unconfirmed request leaves
    no state to expire, and a user who never follows it keeps the address they already had.

    Confirming at the new address is what stops the verification added at registration from being
    trivially undone -- otherwise an account could verify a real address and then move to someone
    else's unchallenged.
    """
    from django.core import signing
    from django.urls import reverse

    token = signing.dumps({"user_pk": user.pk, "email": new_email}, salt=EMAIL_CHANGE_SALT)
    url = request.build_absolute_uri(reverse("email_change_confirm", kwargs={"token": token}))

    body = render_to_string(
        "registration/email_change_email.txt",
        {"user": user, "confirmation_url": url, "new_email": new_email, "site_name": request.get_host()},
    )

    EmailMessage(
        subject=f"Confirm your new email address for {request.get_host()}",
        body=body,
        to=[new_email],
    ).send(fail_silently=False)


def load_email_change_token(token: str, max_age: int) -> tuple[t.Any, str] | None:
    """Return (user, new_email) for a valid email-change token, or None."""
    from django.contrib.auth import get_user_model
    from django.core import signing

    try:
        payload = signing.loads(token, salt=EMAIL_CHANGE_SALT, max_age=max_age)
    except signing.BadSignature:
        return None

    user = get_user_model().objects.filter(pk=payload.get("user_pk")).first()
    email = payload.get("email")
    if user is None or not email:
        return None

    return user, email


def user_from_uidb64(uidb64: str) -> t.Any:
    """Return the user a verification link refers to, or None if the id is unusable."""
    from django.contrib.auth import get_user_model

    try:
        pk = urlsafe_base64_decode(uidb64).decode()
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None

    return get_user_model().objects.filter(pk=pk).first()
