import django.contrib.auth.forms
from django import forms
from django.contrib.auth import (authenticate, get_user_model,
                                 password_validation)
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from forcephot.misc import splitradeclist
from forcephot.models import Task


class TaskForm(forms.ModelForm):
    radeclist = forms.CharField(label="RA Dec list", required=True,
                                widget=forms.Textarea(attrs={"rows": 3, "cols": ""}))

    class Meta:
        model = Task
        # fields = '__all__'
        fields = ('mpc_name', 'radeclist', 'mjd_min', 'mjd_max', 'radec_epoch_year',
                  'propermotion_ra', 'propermotion_dec', 'comment', 'use_reduced', 'send_email')

    def clean(self):
        cleaned_data = super().clean()

        if 'radeclist' in cleaned_data and cleaned_data['radeclist']:
            splitradeclist(cleaned_data, form=self)  # doesn't use the output, just checks for errors

        return cleaned_data


class RegistrationForm(UserCreationForm):
    """
    A form that creates a user, with no privileges, from the given username and
    password.
    """
    error_messages = {
        'password_mismatch': _('The two password fields didn’t match.'),
    }
    email = forms.EmailField(max_length=254, help_text='Required. Give a valid email address.')

    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2', )
