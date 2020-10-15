from django import forms
from django.forms import ModelForm

from .models import *


class TaskForm(forms.ModelForm):
    ra = forms.DecimalField(label="RA", max_digits=8, decimal_places=5)
    dec = forms.DecimalField(label="DEC", max_digits=8, decimal_places=5)
    use_reduced = forms.BooleanField(
        label="Use reduced instead of difference images", required=False)
    # widget=forms.CheckboxInput(attrs={'class': 'cbox'})

    class Meta:
        model = Task
        fields = '__all__'
