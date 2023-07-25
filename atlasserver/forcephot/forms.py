from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils.translation import gettext_lazy as _


class RegistrationForm(UserCreationForm):
    """A form that creates a user, with no privileges, from the given username and password."""

    error_messages = {
        "password_mismatch": _("The two password fields didn't match."),
    }
    email = forms.EmailField(max_length=254, help_text="Required. Give a valid email address.")

    class Meta:
        model = get_user_model()
        fields = (
            "username",
            "email",
            "password1",
            "password2",
        )
