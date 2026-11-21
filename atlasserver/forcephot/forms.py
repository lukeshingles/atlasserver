from typing import override

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _

from atlasserver.forcephot.throttles import ACCOUNT_GUESS_LIMIT_MESSAGE
from atlasserver.forcephot.throttles import account_password_failures_exceeded
from atlasserver.forcephot.throttles import note_account_password_failure


def unverified_accounts_at(email: str):
    """Return the accounts at this address that wait for the confirmation of the address.

    The queryset form of views.awaiting_verification: inactive, and with a PendingEmailVerification
    row.
    """
    return get_user_model().objects.filter(email__iexact=email, is_active=False, pending_verification__isnull=False)


def email_is_taken(email: str, exclude_user=None, *, ignore_unverified: bool = False) -> bool:
    """Return True if another account already uses this email address.

    Django does not enforce uniqueness on User.email, but duplicates make the password reset flow
    ambiguous (it emails a reset link for every matching account), so reject them here.

    With `ignore_unverified`, an account that waits for the confirmation of the address does not
    count; see RegistrationForm.clean_email.
    """
    others = get_user_model().objects.filter(email__iexact=email)
    if exclude_user is not None and exclude_user.pk is not None:
        others = others.exclude(pk=exclude_user.pk)
    if ignore_unverified:
        others = others.exclude(pk__in=unverified_accounts_at(email).values("pk"))

    return others.exists()


class EmailChangeForm(forms.Form):
    """A form for a logged-in user to change their account email address after confirming their password."""

    error_messages = {
        "password_incorrect": _("Your password was entered incorrectly. Please enter it again."),
        "email_taken": _("An account with that email address already exists."),
    }
    password = forms.CharField(
        label=_("Current password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text=_("For security, please enter your current password."),
    )
    new_email = forms.EmailField(
        label=_("New email address"), max_length=254, help_text=_("Give a valid email address.")
    )

    def __init__(self, user: User, *args, **kwargs) -> None:
        """Bind the form to the account whose address is being changed."""
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        password = self.cleaned_data["password"]

        # The person who guesses here has a session for this account, so the budget is for the
        # account; see throttles.account_password_failures_exceeded. This code refuses the check
        # before it reads the password. Thus a guess over the budget gets no answer.
        if account_password_failures_exceeded(self.user.pk):
            raise forms.ValidationError(ACCOUNT_GUESS_LIMIT_MESSAGE, code="throttled")

        if not self.user.check_password(password):
            note_account_password_failure(self.user.pk)
            raise forms.ValidationError(self.error_messages["password_incorrect"], code="password_incorrect")
        return password

    def clean_new_email(self):
        new_email = self.cleaned_data["new_email"]

        # Only once the password has been proved. Django runs every clean_<field> independently and
        # carries on past a failed one, so this used to answer "is this address registered here?"
        # for anyone with any account and no password at all -- an enumeration oracle over the
        # whole user table, rendered right next to the field.
        #
        # password is declared above new_email, so it has already been cleaned by now; its absence
        # from cleaned_data is exactly the case where clean_password rejected it.
        if "password" not in self.cleaned_data:
            return new_email

        if email_is_taken(new_email, exclude_user=self.user):
            raise forms.ValidationError(self.error_messages["email_taken"], code="email_taken")
        return new_email


class ResendVerificationForm(forms.Form):
    """Asks only for the address, so that someone locked out of an unverified account can recover.

    No password field: the point is to reach a user who cannot log in, and the link goes to the
    address itself, so knowing the address is not enough to take anything over.
    """

    email = forms.EmailField(
        label=_("Email address"),
        max_length=254,
        help_text=_("The address you registered with. We'll send a fresh verification link."),
    )


class RegistrationForm(UserCreationForm[User]):
    """A form that creates a user, with no privileges, from the given username and password."""

    error_messages = {
        "password_mismatch": _("The two password fields didn't match."),
        "email_taken": _("An account with that email address already exists."),
    }
    email = forms.EmailField(max_length=254, help_text="Required. Give a valid email address.")

    def clean_email(self):
        email = self.cleaned_data["email"]

        # An account that waits for the confirmation of this address does not block a new
        # registration, and views.register replaces that account. Nobody proved the address for
        # either account. Without this rule, a person who registers first with the address of
        # someone else holds that address, and a resend every few days keeps the account.
        if email_is_taken(email, ignore_unverified=True):
            raise forms.ValidationError(self.error_messages["email_taken"], code="email_taken")
        return email

    # pyrefly: ignore [bad-override]
    class Meta:  # ty: ignore[incompatible-attribute-override]
        model = get_user_model()
        fields = (
            "username",
            "email",
            "password1",
            "password2",
        )


class ThrottledPasswordChangeForm(PasswordChangeForm):
    """Django's form to change the password, with the failed-password budget of the account.

    The person who guesses the old password here has a session for this account; see
    throttles.account_password_failures_exceeded.
    """

    @override
    def clean_old_password(self):
        if account_password_failures_exceeded(self.user.pk):
            raise forms.ValidationError(ACCOUNT_GUESS_LIMIT_MESSAGE, code="throttled")

        try:
            return super().clean_old_password()
        except forms.ValidationError:
            note_account_password_failure(self.user.pk)
            raise
