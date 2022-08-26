import django.contrib.auth.forms
from django import forms
from django.contrib.auth import authenticate, get_user_model, password_validation
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _


class RegistrationForm(UserCreationForm):
    """
    A form that creates a user, with no privileges, from the given username and
    password.
    """

    error_messages = {
        "password_mismatch": _("The two password fields didn’t match."),
    }
    email = forms.EmailField(max_length=254, help_text="Required. Give a valid email address.")

    class Meta:
        model = User
        fields = (
            "username",
            "email",
            "password1",
            "password2",
        )
