from django import forms
from django.forms import ModelForm

from .models import *


class TaskForm(forms.ModelForm):

    # ra = forms.FloatField(label="RA", required=False)

    # dec = forms.FloatField(label="DEC", required=False)

    use_reduced = forms.BooleanField(
        label="Use reduced instead of difference images", required=False)

    radeclist = forms.CharField(label="RA DEC list", required=False,
                                widget=forms.Textarea(attrs={"rows": 2, "cols": ""}))

    class Meta:
        model = Task
        fields = '__all__'
