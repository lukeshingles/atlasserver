from django import forms
from django.forms import ModelForm
from django.utils.translation import gettext, gettext_lazy as _
from django.contrib.auth import (
    authenticate, get_user_model, password_validation,
)
from django.core.exceptions import ValidationError
import django.contrib.auth.forms
from .models import *

import astrocalc.coords.unit_conversion
import fundamentals.logs


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


class AtlasServerUserCreationForm(forms.ModelForm):
    """
    A form that creates a user, with no privileges, from the given username and
    password.
    """
    error_messages = {
        'password_mismatch': _('The two password fields didn’t match.'),
    }
    email = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(attrs={'autocomplete': 'email'})
    )
    password1 = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text=password_validation.password_validators_help_text_html(),
    )
    password2 = forms.CharField(
        label=_("Password confirmation"),
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        strip=False,
        help_text=_("Enter the same password as before, for verification."),
    )

    class Meta:
        model = User
        fields = ("username", "email")
        field_classes = {
            'username': django.contrib.auth.forms.UsernameField,
            'email': forms.EmailField,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self._meta.model.USERNAME_FIELD in self.fields:
            self.fields[self._meta.model.USERNAME_FIELD].widget.attrs['autofocus'] = True

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise ValidationError(
                self.error_messages['password_mismatch'],
                code='password_mismatch',
            )
        return password2

    def _post_clean(self):
        super()._post_clean()
        # Validate the password after self.instance is updated with form data
        # by super().
        password = self.cleaned_data.get('password2')
        if password:
            try:
                password_validation.validate_password(password, self.instance)
            except ValidationError as error:
                self.add_error('password2', error)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user
