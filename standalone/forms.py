from datetime import timedelta

from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from standalone.models import (
    ContentAsset,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    CourseMagicLink,
    StudentInvitation,
    TeacherInvitation,
    User,
    ValidationEvent,
)
from standalone.services.content import SUPPORTED_EXTENSIONS


class EmailOrUsernameAuthenticationForm(forms.Form):
    username = forms.CharField(label="Email or username", max_length=150)
    password = forms.CharField(label="Password", widget=forms.PasswordInput())


class TeacherInvitationForm(forms.ModelForm):
    class Meta:
        model = TeacherInvitation
        fields = ["email"]

    def save(self, invited_by=None, commit=True):
        invitation = super().save(commit=False)
        invitation.invited_by = invited_by
        invitation.expires_at = TeacherInvitation.default_expiry()
        if commit:
            invitation.save()
        return invitation


class TeacherActivationForm(forms.Form):
    full_name = forms.CharField(max_length=150)
    password1 = forms.CharField(widget=forms.PasswordInput())
    password2 = forms.CharField(widget=forms.PasswordInput())
    institution = forms.CharField(max_length=255, required=False)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("password1") != cleaned_data.get("password2"):
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["title", "slug", "summary", "is_active"]

    def clean_slug(self):
        slug = slugify(self.cleaned_data["slug"] or self.cleaned_data["title"])
        if not slug:
            raise forms.ValidationError("Please provide a valid course title.")
        return slug


class CourseConfigForm(forms.ModelForm):
    class Meta:
        model = CourseConfig
        exclude = ["course"]


class CourseAllowedEmailForm(forms.ModelForm):
    class Meta:
        model = CourseAllowedEmail
        fields = ["email"]


class StudentInvitationForm(forms.ModelForm):
    class Meta:
        model = StudentInvitation
        fields = ["email"]

    def save(self, course, created_by, commit=True):
        invitation = super().save(commit=False)
        invitation.course = course
        invitation.created_by = created_by
        invitation.invitation_type = StudentInvitation.InvitationType.EMAIL
        invitation.expires_at = timezone.now() + timedelta(hours=settings.STANDALONE_INVITE_EXPIRY_HOURS)
        if commit:
            invitation.save()
        return invitation


class StudentActivationForm(forms.Form):
    full_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    password1 = forms.CharField(widget=forms.PasswordInput())
    password2 = forms.CharField(widget=forms.PasswordInput())
    institution = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, locked_email="", **kwargs):
        super().__init__(*args, **kwargs)
        if locked_email:
            self.fields["email"].initial = locked_email
            self.fields["email"].disabled = True

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("password1") != cleaned_data.get("password2"):
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class SelfEnrolForm(StudentActivationForm):
    pass


class MagicLinkEmailForm(forms.Form):
    full_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    password1 = forms.CharField(widget=forms.PasswordInput())
    password2 = forms.CharField(widget=forms.PasswordInput())
    institution = forms.CharField(max_length=255, required=False)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("password1") != cleaned_data.get("password2"):
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class CourseBlockForm(forms.ModelForm):
    class Meta:
        model = CourseBlock
        fields = ["title", "summary", "order"]


class ContentAssetForm(forms.ModelForm):
    class Meta:
        model = ContentAsset
        fields = ["file", "include_in_generation"]

    def save(self, block, uploaded_by, commit=True):
        asset = super().save(commit=False)
        asset.block = block
        asset.uploaded_by = uploaded_by
        asset.original_filename = self.cleaned_data["file"].name
        asset.extension = f".{self.cleaned_data['file'].name.rsplit('.', 1)[-1].lower()}" if "." in self.cleaned_data["file"].name else ""
        if commit:
            asset.save()
        return asset

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        extension = f".{uploaded_file.name.rsplit('.', 1)[-1].lower()}" if "." in uploaded_file.name else ""
        if extension not in SUPPORTED_EXTENSIONS:
            raise forms.ValidationError("Unsupported file type for standalone content processing.")
        return uploaded_file


class MagicLinkCreateForm(forms.ModelForm):
    class Meta:
        model = CourseMagicLink
        fields = ["max_uses"]

    def save(self, course, created_by, commit=True):
        magic_link = super().save(commit=False)
        magic_link.course = course
        magic_link.created_by = created_by
        magic_link.expires_at = CourseMagicLink.default_expiry()
        if commit:
            magic_link.save()
        return magic_link


class ValidationEventForm(forms.ModelForm):
    class Meta:
        model = ValidationEvent
        fields = ["title", "starts_at", "location", "capacity", "freeze_at", "question_count"]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "freeze_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        starts_at = cleaned_data.get("starts_at")
        freeze_at = cleaned_data.get("freeze_at")
        if starts_at and freeze_at and freeze_at >= starts_at:
            raise forms.ValidationError("Freeze-out time must be before the validation start.")
        return cleaned_data


class UserCreationFromInviteMixin:
    @staticmethod
    def build_username(email: str) -> str:
        base = slugify(email.split("@")[0])[:20] or "mcq-user"
        candidate = base
        counter = 1
        while User.objects.filter(username=candidate).exists():
            counter += 1
            candidate = f"{base[:16]}-{counter}"
        return candidate
