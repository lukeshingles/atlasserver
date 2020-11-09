import astrocalc.coords.unit_conversion
import django.contrib.auth.forms
import fundamentals.logs
from django import forms
from django.contrib.auth import (authenticate, get_user_model,
                                 password_validation)
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from .models import *


class TaskForm(forms.ModelForm):

    radeclist = forms.CharField(label="RA Dec list", required=True,
                                widget=forms.Textarea(attrs={"rows": 3, "cols": ""}))

    class Meta:
        model = Task
        fields = '__all__'

    # def clean_radeclist(self):
    #     data = self.cleaned_data.get("radeclist")
    #     if True:
    #         raise ValidationError(
    #             'problem',
    #             code='password_mismatch',
    #         )
    #     return data

    def clean(self):
        cleaned_data = super().clean()
        # self.add_error('radeclist', 'test')

        data = cleaned_data
        if 'radeclist' in data and data['radeclist']:
            # multi-add functionality with a list of RA,DEC coords
            converter = astrocalc.coords.unit_conversion(log=fundamentals.logs.emptyLogger())

            for index, line in enumerate(data['radeclist'].split('\n'), 1):
                if ',' in line:
                    row = line.split(',')
                else:
                    row = line.split()

                if row and len(row) < 2:
                    self.add_error('radeclist', f'Error on line {index}: Could not find two columns. '
                                   'Separate RA and Dec by a comma or a space.')
                elif row:
                    try:
                        converter.ra_sexegesimal_to_decimal(ra=row[0])
                        converter.dec_sexegesimal_to_decimal(dec=row[1])
                    except (IndexError, OSError) as err:
                        self.add_error('radeclist', f'Error on line {index}: {err}')
                        pass

        return cleaned_data


class RegistrationForm(UserCreationForm):
    """
    A form that creates a user, with no privileges, from the given username and
    password.
    """
    error_messages = {
        'password_mismatch': _('The two password fields didn’t match.'),
    }
    email = forms.EmailField(max_length=254, help_text='Required. Inform a valid email address.')

    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2', )
