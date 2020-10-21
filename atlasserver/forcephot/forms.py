from django import forms
from django.forms import ModelForm

from .models import *


class TaskForm(forms.ModelForm):

    radeclist = forms.CharField(label="RA DEC list", required=True,
                                widget=forms.Textarea(attrs={"rows": 2, "cols": ""}))

    class Meta:
        model = Task
        fields = '__all__'
