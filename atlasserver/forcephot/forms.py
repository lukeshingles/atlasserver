import typing as t
from typing import override

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _

from atlasserver.forcephot.throttles import login_failures_exceeded
from atlasserver.forcephot.throttles import LOGIN_LIMIT_MESSAGE
from atlasserver.forcephot.throttles import note_login_failure


def email_is_taken(email: str, exclude_user=None) -> bool:
    """Return True if another account already uses this email address.

    Django does not enforce uniqueness on User.email, but duplicates make the password reset flow
    ambiguous (it emails a reset link for every matching account), so reject them here.
    """
    others = get_user_model().objects.filter(email__iexact=email)
    if exclude_user is not None and exclude_user.pk is not None:
        others = others.exclude(pk=exclude_user.pk)

    return others.exists()


class LoginFailureLimitMixin(AuthenticationForm):
    """Refuse the password check itself once an address has failed too many in the window.

    Refused before the check, not after: an over-budget address must not learn whether the
    password was right. The error is a non-field error with code "throttled", which the login view
    reads to answer 429 rather than 200.

    An AuthenticationForm subclass rather than a bare mixin, so that super().clean() resolves for
    the type checkers with the same signature. In the method resolution order of the two forms
    below, that call still reaches the admin's clean() and then AuthenticationForm's, which is
    where the password is checked.
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
        """Return whether the refusal was for the password, rather than for the account.

        Only a wrong password is a guess. The admin refuses an account without staff access after
        the password has checked out, with the same error code as a wrong password; and the default
        backend refuses an inactive account, such as one not yet verified, before the form learns
        whether the password was right. So the password is checked here against the named account.
        A colleague who tries the right password on an account that is refused must not spend the
        address's budget of guesses.
        """
        if getattr(self, "user_cache", None) is not None:
            return False

        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        if not username or not password:
            # no password was offered, so none was guessed
            return False

        user = User.objects.filter(username=username).first()
        if user is None:
            return True

        return not user.check_password(password)


class ThrottledAuthenticationForm(LoginFailureLimitMixin, AuthenticationForm):
    """The login page's form."""


class ThrottledAdminAuthenticationForm(LoginFailureLimitMixin, AdminAuthenticationForm):
    """The admin login's form; the admin reads site.login_form at request time."""


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

    def __init__(self, user, *args, **kwargs) -> None:
        """Bind the form to the account whose address is being changed."""
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.user.check_password(password):
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
        if email_is_taken(email):
            raise forms.ValidationError(self.error_messages["email_taken"], code="email_taken")
        return email

    # pyrefly: ignore [bad-override]
    class Meta:
        model = get_user_model()
        fields = (
            "username",
            "email",
            "password1",
            "password2",
        )
