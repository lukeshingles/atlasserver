"""Email address verification for new registrations.

Registration used to log the new account straight in without proving the address belonged to
whoever typed it, so the address that receives job completion mail (and password resets) was
unverified. Anyone could sign up as someone else's address, and the owner's only sign would be
unexpected mail from a service they had never used.

Django ships no verification flow, but it ships the pieces: the same signed-token machinery the
password reset views use. Reusing it means there is no new model, no new column and nothing to
expire by hand -- the token carries its own timestamp, and PASSWORD_RESET_TIMEOUT bounds it.
"""

import hashlib
import typing as t
from typing import override

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
    @override
    def _make_hash_value(self, user: t.Any, timestamp: int) -> str:
        # email as well, so that changing the address invalidates any outstanding link for the old
        # one. The base implementation covers pk, password and last_login.
        return f"{super()._make_hash_value(user, timestamp)}{user.is_active}{user.email}"


token_generator = EmailVerificationTokenGenerator()


def absolute_url(request: HttpRequest, path: str) -> str:
    """Return `path` as an absolute URL, preferring the configured origin to the request's host.

    A link in security-sensitive mail must not be built from the Host header. Pinning ALLOWED_HOSTS
    is not enough on its own: it legitimately holds wildcard entries, so every subdomain of
    qub.ac.uk and fallingstar-data.com passes validation, and anyone who can serve one of those can
    have this send a victim a link to their own server -- disclosing the token when it is opened.

    Falls back to the request when SITE_ORIGIN is unset, which is what development and the tests
    want; production sets it.
    """
    from urllib.parse import urljoin

    from django.conf import settings

    if settings.SITE_ORIGIN:
        return urljoin(settings.SITE_ORIGIN, path)

    return request.build_absolute_uri(path)


def site_name(request: HttpRequest) -> str:
    """Return the host to name in email, from the same source the links come from."""
    from urllib.parse import urlsplit

    from django.conf import settings

    return urlsplit(settings.SITE_ORIGIN).netloc if settings.SITE_ORIGIN else request.get_host()


def verification_url(request: HttpRequest, user: t.Any) -> str:
    """Return the absolute URL that verifies this user's current email address."""
    from django.urls import reverse

    path = reverse(
        "verify_email",
        kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": token_generator.make_token(user)},
    )

    return absolute_url(request, path)


def send_verification_email(request: HttpRequest, user: t.Any) -> None:
    """Mail the user a link that confirms their address."""
    body = render_to_string(
        "registration/verification_email.txt",
        {"user": user, "verification_url": verification_url(request, user), "site_name": site_name(request)},
    )

    EmailMessage(
        subject=f"Verify your email address for {site_name(request)}",
        body=body,
        to=[user.email],
    ).send(fail_silently=False)


# Salt for the email-change tokens below. Distinct from the activation generator's, so neither
# kind of link can be presented as the other.
EMAIL_CHANGE_SALT: t.Final = "atlasserver.forcephot.verification.email_change"


def _credential_state(user: t.Any) -> str:
    """Return a value that changes whenever the account's credentials or usability do.

    The password hash covers a change, a reset and a forced logout (Django rotates the session
    hash from it); is_active covers an administrator switching the account off. Hashed rather than
    embedded, because the token is signed but not encrypted and the client can read its payload.
    """
    return hashlib.sha256(f"{user.password}:{user.is_active}".encode()).hexdigest()


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

    # from_email makes the link single-use: applying it changes the account's address, so a replay
    # no longer matches. Without it the token stayed valid for the whole timeout, and anyone still
    # holding an old link -- it was delivered to a mailbox, and scanners and forwarding rules keep
    # copies -- could undo a later change and point result mail and password resets back at
    # themselves.
    #
    # The credential state goes in for the same reason PasswordResetTokenGenerator hashes it: a
    # pending change has to die when the account is recovered. Someone with temporary access could
    # otherwise request a move to their own mailbox, wait out the owner's password reset, and
    # confirm afterwards -- taking the address, and with it every future reset link.
    token = signing.dumps(
        {"user_pk": user.pk, "email": new_email, "from_email": user.email, "state": _credential_state(user)},
        salt=EMAIL_CHANGE_SALT,
    )
    url = absolute_url(request, reverse("email_change_confirm", kwargs={"token": token}))

    body = render_to_string(
        "registration/email_change_email.txt",
        {"user": user, "confirmation_url": url, "new_email": new_email, "site_name": site_name(request)},
    )

    EmailMessage(
        subject=f"Confirm your new email address for {site_name(request)}",
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

    # annotated, for the same reason the parameters above are: the type checkers disagree about
    # whether get_user_model() yields the concrete User or AbstractBaseUser, and only one of them
    # believes it has an `email`
    user: t.Any = get_user_model().objects.filter(pk=payload.get("user_pk")).first()
    email = payload.get("email")
    if user is None or not email:
        return None

    # the address the account had when the link was issued. A link is good for one change: once
    # applied (or once the address has moved on for any other reason) this no longer matches.
    if user.email != payload.get("from_email"):
        return None

    # and the credentials it was issued under, so that a password change, a reset or an
    # administrative deactivation revokes anything still pending
    if _credential_state(user) != payload.get("state"):
        return None

    return user, email


def user_from_uidb64(uidb64: str) -> t.Any:
    """Return the user a verification link refers to, or None if the id is unusable."""
    from django.contrib.auth import get_user_model

    # int() inside the guard, not just the decode: "YWJj" is valid base64 and decodes cleanly to
    # "abc", and it is the *lookup* that then rejects it -- raising ValueError from a queryset
    # rather than from anything this had caught, so a malformed link answered 500 instead of the
    # invalid-link page.
    try:
        pk = int(urlsafe_base64_decode(uidb64).decode())
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None

    return get_user_model().objects.filter(pk=pk).first()
