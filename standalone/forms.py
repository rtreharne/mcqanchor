from datetime import timedelta

from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from standalone.models import (
    BlockConfig,
    ContentAsset,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    CourseMagicLink,
    LearningObjective,
    StudentInvitation,
    TeacherInvitation,
    User,
    ValidationEvent,
)
from standalone.services.content import SUPPORTED_EXTENSIONS, sanitize_learning_objective, sanitize_summary


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


class CourseTitleInlineForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["title"]

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Please enter a course title.")
        return title


class CourseConfigForm(forms.ModelForm):
    class Meta:
        model = CourseConfig
        exclude = ["course"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["self_enrol_enabled"].label = "Enable self-enrol allowlist"
        self.fields["self_enrol_enabled"].help_text = (
            "Students can use the course self-enrol URL only if their exact email address has been added to the allowlist."
        )
        self.fields["self_enrol_domain"].label = "Restrict enrolment domain"
        self.fields["self_enrol_domain"].help_text = (
            "Optional. Enter a domain such as example.ac.uk. It applies to both self-enrol allowlist signups and magic links."
        )
        self.fields["maq_ratio_percent"].label = "Multiple-answer question ratio (%)"
        self.fields["maq_ratio_percent"].help_text = (
            "Target percentage of newly generated questions that should allow multiple correct answers."
        )
        self.fields["waq_ratio_percent"].label = "Written-answer question ratio (%)"
        self.fields["waq_ratio_percent"].help_text = (
            "Target percentage of newly generated questions that should use typed written answers."
        )
        self.fields["advanced_question_start_percent"].label = "Start MAQ/WAQ after target progress (%)"
        self.fields["advanced_question_start_percent"].help_text = (
            "Block progress threshold before students are asked multiple-answer or written-answer questions. "
            "Use 0 to allow them from the start."
        )

    def clean_self_enrol_domain(self):
        value = (self.cleaned_data.get("self_enrol_domain") or "").strip().lower()
        if value.startswith("@"):
            value = value[1:]
        return value


class CourseAllowedEmailForm(forms.ModelForm):
    class Meta:
        model = CourseAllowedEmail
        fields = ["email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].label = "Allowed student email"
        self.fields["email"].help_text = (
            "Self-enrolment requires this exact email address. Magic links do not require an allowlist entry."
        )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


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


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]
        if data is None:
            return []
        return [single_clean(data, initial)]


class CourseBlockForm(forms.ModelForm):
    file = MultipleFileField(label="Files", required=False)

    class Meta:
        model = CourseBlock
        fields = ["title", "available_from"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["available_from"].initial = timezone.localdate()
        self.fields["available_from"].required = False
        self.fields["available_from"].help_text = "MCQs for this block will only be generated and shown to students from this date."
        self.fields["available_from"].widget = forms.DateInput(attrs={"type": "date"})
        self.fields["file"].widget.attrs.update(
            {
                "class": "upload-native-input",
                "data-upload-input": "true",
                "multiple": True,
            }
        )

    def clean_file(self):
        uploaded_files = self.cleaned_data["file"]
        for uploaded_file in uploaded_files:
            extension = f".{uploaded_file.name.rsplit('.', 1)[-1].lower()}" if "." in uploaded_file.name else ""
            if extension not in SUPPORTED_EXTENSIONS:
                raise forms.ValidationError(f"Unsupported file type for standalone content processing: {uploaded_file.name}")
        return uploaded_files

    def clean_available_from(self):
        return self.cleaned_data.get("available_from") or timezone.localdate()

    def save_assets(self, block, uploaded_by):
        assets = []
        for uploaded_file in self.cleaned_data.get("file", []):
            extension = f".{uploaded_file.name.rsplit('.', 1)[-1].lower()}" if "." in uploaded_file.name else ""
            asset = ContentAsset.objects.create(
                block=block,
                uploaded_by=uploaded_by,
                file=uploaded_file,
                include_in_generation=True,
                original_filename=uploaded_file.name,
                extension=extension,
            )
            assets.append(asset)
        return assets


class BlockTitleInlineForm(forms.ModelForm):
    class Meta:
        model = CourseBlock
        fields = ["title"]

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Please enter a block title.")
        return title


class BlockSummaryInlineForm(forms.ModelForm):
    class Meta:
        model = CourseBlock
        fields = ["summary"]

    def clean_summary(self):
        return sanitize_summary(self.cleaned_data["summary"])


class BlockAvailableFromInlineForm(forms.ModelForm):
    class Meta:
        model = CourseBlock
        fields = ["available_from"]

    def clean_available_from(self):
        value = self.cleaned_data["available_from"]
        if value is None:
            raise forms.ValidationError("Please enter an availability date.")
        return value


class BlockConfigTargetQuestionCountInlineForm(forms.ModelForm):
    class Meta:
        model = BlockConfig
        fields = ["target_question_count"]

    def clean_target_question_count(self):
        value = self.cleaned_data["target_question_count"]
        if value is None:
            raise forms.ValidationError("Please enter a target question count.")
        if value < 1:
            raise forms.ValidationError("Target question count must be at least 1.")
        return value


class LearningObjectiveInlineForm(forms.ModelForm):
    class Meta:
        model = LearningObjective
        fields = ["text"]

    def clean_text(self):
        text = sanitize_learning_objective(self.cleaned_data["text"])
        if not text:
            raise forms.ValidationError("Please enter a learning objective.")
        return text


class ContentAssetForm(forms.Form):
    file = MultipleFileField(label="Files")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].widget.attrs.update(
            {
                "class": "upload-native-input",
                "data-upload-input": "true",
                "multiple": True,
            }
        )

    def save_assets(self, block, uploaded_by):
        assets = []
        for uploaded_file in self.cleaned_data["file"]:
            extension = f".{uploaded_file.name.rsplit('.', 1)[-1].lower()}" if "." in uploaded_file.name else ""
            asset = ContentAsset.objects.create(
                block=block,
                uploaded_by=uploaded_by,
                file=uploaded_file,
                include_in_generation=True,
                original_filename=uploaded_file.name,
                extension=extension,
            )
            assets.append(asset)
        return assets

    def clean_file(self):
        uploaded_files = self.cleaned_data["file"]
        if not uploaded_files:
            raise forms.ValidationError("Please choose at least one file.")
        for uploaded_file in uploaded_files:
            extension = f".{uploaded_file.name.rsplit('.', 1)[-1].lower()}" if "." in uploaded_file.name else ""
            if extension not in SUPPORTED_EXTENSIONS:
                raise forms.ValidationError(f"Unsupported file type for standalone content processing: {uploaded_file.name}")
        return uploaded_files


class MagicLinkCreateForm(forms.ModelForm):
    class Meta:
        model = CourseMagicLink
        fields = ["max_uses"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["max_uses"].label = "Maximum new enrolments"
        self.fields["max_uses"].help_text = (
            "Each new student account enrolled through this active link consumes one use. Existing enrolled students can reuse an active link without consuming another use."
        )

    def clean_max_uses(self):
        value = self.cleaned_data["max_uses"]
        if value < 1:
            raise forms.ValidationError("Magic links must allow at least one enrolment.")
        return value

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
