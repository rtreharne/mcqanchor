from django import forms

from .models import PilotEnquiry


class PilotEnquiryForm(forms.ModelForm):
    website = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = PilotEnquiry
        fields = ["name", "email", "institution", "module_or_subject", "message"]
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "name"}),
            "email": forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"}),
            "institution": forms.TextInput(attrs={"autocomplete": "organization"}),
            "module_or_subject": forms.TextInput(),
            "message": forms.Textarea(attrs={"rows": 4}),
        }

    def clean_website(self):
        value = self.cleaned_data["website"]
        if value:
            raise forms.ValidationError("Please leave this field empty.")
        return value
