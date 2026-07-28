from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils.translation import gettext_lazy as _


class EmailChangeForm(forms.Form):
    """A form for a logged-in user to change their account email address after confirming their password."""

    error_messages = {
        "password_incorrect": _("Your password was entered incorrectly. Please enter it again."),
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
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.user.check_password(password):
            raise forms.ValidationError(self.error_messages["password_incorrect"], code="password_incorrect")
        return password

    def save(self):
        self.user.email = self.cleaned_data["new_email"]
        self.user.save(update_fields=["email"])
        return self.user


class RegistrationForm(UserCreationForm):
    """A form that creates a user, with no privileges, from the given username and password."""

    error_messages = {
        "password_mismatch": _("The two password fields didn't match."),
    }
    email = forms.EmailField(max_length=254, help_text="Required. Give a valid email address.")

    # pyrefly: ignore [bad-override]
    class Meta:
        model = get_user_model()
        fields = (
            "username",
            "email",
            "password1",
            "password2",
        )
