from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _


def email_is_taken(email: str, exclude_user=None) -> bool:
    """Return True if another account already uses this email address.

    Django does not enforce uniqueness on User.email, but duplicates make the password reset flow
    ambiguous (it emails a reset link for every matching account), so reject them here.
    """
    others = get_user_model().objects.filter(email__iexact=email)
    if exclude_user is not None and exclude_user.pk is not None:
        others = others.exclude(pk=exclude_user.pk)

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
