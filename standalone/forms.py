from datetime import timedelta
import json

from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from standalone.models import (
    BlockConfig,
    BlockProject,
    ContentAsset,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    CourseImport,
    CourseMagicLink,
    LearningObjective,
    StudentInvitation,
    TeacherInvitation,
    User,
    ValidationEvent,
)
from standalone.services.content import SUPPORTED_EXTENSIONS, sanitize_learning_objective, sanitize_summary
from standalone.services.demo_mode import normalize_demo_iframe_origins
from standalone.services.guidance import sanitize_assistant_guidance


def _apply_question_setting_field_attributes(fields, *, block_level: bool = False) -> None:
    guidance_help_prefix = "Optional. Add free-text steering notes"
    if block_level:
        guidance_help_suffix = " for this block. Leave blank to inherit the course default."
    else:
        guidance_help_suffix = (
            " for question generation and student course chat, such as audience age, notation rules, or preferred wording."
        )
    fields["assistant_guidance"].label = "Assistant guidance"
    fields["assistant_guidance"].help_text = guidance_help_prefix + guidance_help_suffix
    fields["assistant_guidance"].widget = forms.Textarea(
        attrs={
            "rows": 5,
            "placeholder": (
                "Inherit course default"
                if block_level
                else "E.g. KS2 mathematics for 10-11 year olds. Keep language age-appropriate and concrete."
            ),
        }
    )
    fields["numeric_ratio_percent"].label = "Numeric question ratio (%)"
    fields["numeric_ratio_percent"].help_text = (
        "Leave blank to inherit the course default."
        if block_level
        else "Target percentage of newly generated questions that should be numeric single-answer items with locally validated calculations."
    )
    fields["maq_ratio_percent"].label = "Multiple-answer question ratio (%)"
    fields["maq_ratio_percent"].help_text = (
        "Leave blank to inherit the course default."
        if block_level
        else "Target percentage of newly generated questions that should allow multiple correct answers."
    )
    fields["waq_ratio_percent"].label = "Written-answer question ratio (%)"
    fields["waq_ratio_percent"].help_text = (
        "Leave blank to inherit the course default."
        if block_level
        else "Target percentage of newly generated questions that should use typed written answers."
    )
    fields["coding_question_ratio_percent"].label = "Coding question ratio (%)"
    fields["coding_question_ratio_percent"].help_text = (
        "Leave blank to inherit the course default."
        if block_level
        else "Target percentage of newly generated questions that should use coding comprehension or debugging snippets when coding content is detected."
    )
    fields["advanced_question_start_percent"].label = "Start MAQ/WAQ after engagement progress (%)"
    fields["advanced_question_start_percent"].help_text = (
        "Leave blank to inherit the course default."
        if block_level
        else "Block progress threshold before students are asked multiple-answer or written-answer questions. Use 0 to allow them from the start."
    )
    fields["distractor_count"].help_text = (
        "Leave blank to inherit the course default."
        if block_level
        else "Number of distractors to include when generating single-answer questions."
    )

    for name in (
        "assistant_guidance",
        "distractor_count",
        "numeric_ratio_percent",
        "maq_ratio_percent",
        "waq_ratio_percent",
        "coding_question_ratio_percent",
        "advanced_question_start_percent",
    ):
        field = fields[name]
        if block_level and not isinstance(field.widget, forms.CheckboxInput):
            field.widget.attrs["placeholder"] = field.widget.attrs.get("placeholder", "Inherit course default") or "Inherit course default"


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
        fields = ["title"]

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Please provide a valid course title.")
        return title

    def _build_unique_slug(self, title: str) -> str:
        base_slug = slugify(title)
        if not base_slug:
            raise forms.ValidationError("Please provide a valid course title.")

        slug = base_slug
        suffix = 2
        queryset = Course.objects.all()
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        while queryset.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    def save(self, commit=True):
        course = super().save(commit=False)
        course.slug = self._build_unique_slug(course.title)
        course.summary = ""
        course.is_active = True
        if commit:
            course.save()
        return course


class CourseTitleInlineForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["title"]

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Please enter a course title.")
        return title


class CourseImportUploadForm(forms.ModelForm):
    class Meta:
        model = CourseImport
        fields = ["source_file"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        max_size_bytes = int(getattr(settings, "PDF_IMPORT_MAX_FILE_SIZE_BYTES", 200 * 1024 * 1024) or 0)
        max_size_mb = max(1, (max_size_bytes + (1024 * 1024) - 1) // (1024 * 1024)) if max_size_bytes > 0 else 0
        self.fields["source_file"].label = "PDF textbook"
        self.fields["source_file"].help_text = (
            f"Upload one PDF. The system will detect chapters before creating any blocks. "
            f"Maximum file size: {max_size_mb} MB."
            if max_size_mb > 0
            else "Upload one PDF. The system will detect chapters before creating any blocks."
        )
        self.fields["source_file"].widget.attrs.update(
            {
                "accept": ".pdf,application/pdf",
                "class": "upload-native-input",
                "data-upload-input": "true",
                "data-max-file-size-bytes": str(max_size_bytes) if max_size_bytes > 0 else "",
                "data-max-file-size-label": f"{max_size_mb} MB" if max_size_mb > 0 else "",
            }
        )

    def clean_source_file(self):
        uploaded_file = self.cleaned_data["source_file"]
        filename = uploaded_file.name.lower()
        content_type = getattr(uploaded_file, "content_type", "")
        if not filename.endswith(".pdf") and content_type != "application/pdf":
            raise forms.ValidationError("Please upload a PDF file.")
        max_size_bytes = int(getattr(settings, "PDF_IMPORT_MAX_FILE_SIZE_BYTES", 200 * 1024 * 1024) or 0)
        if max_size_bytes > 0 and getattr(uploaded_file, "size", 0) > max_size_bytes:
            max_size_mb = max(1, (max_size_bytes + (1024 * 1024) - 1) // (1024 * 1024))
            raise forms.ValidationError(f"PDF must be {max_size_mb} MB or smaller.")
        return uploaded_file

    def save(self, course, uploaded_by, commit=True):
        course_import = super().save(commit=False)
        course_import.course = course
        course_import.uploaded_by = uploaded_by
        course_import.original_filename = self.cleaned_data["source_file"].name
        if commit:
            course_import.save()
        return course_import


class CourseImportChapterSelectionForm(forms.Form):
    selected_chapters = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, required=True)

    def __init__(self, *args, chapters=None, max_selected_chapters: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.chapters = list(chapters or [])
        self.max_selected_chapters = max_selected_chapters
        self.fields["selected_chapters"].choices = [(str(chapter.pk), chapter.title) for chapter in self.chapters]

    def clean_selected_chapters(self):
        selected = self.cleaned_data["selected_chapters"]
        if not selected:
            raise forms.ValidationError("Select at least one chapter.")
        valid_ids = {str(chapter.pk) for chapter in self.chapters}
        invalid_ids = set(selected) - valid_ids
        if invalid_ids:
            raise forms.ValidationError("One or more selected chapters are not available for this import.")
        if self.max_selected_chapters and self.max_selected_chapters > 0 and len(selected) > self.max_selected_chapters:
            raise forms.ValidationError(
                f"Select at most {self.max_selected_chapters} chapters at a time for this deployment."
            )
        return [int(chapter_id) for chapter_id in selected]


class CourseConfigForm(forms.ModelForm):
    class Meta:
        model = CourseConfig
        exclude = ["course"]

    def __init__(self, *args, **kwargs):
        can_edit_homepage_demo = kwargs.pop("can_edit_homepage_demo", False)
        super().__init__(*args, **kwargs)
        self.fields["self_enrol_enabled"].label = "Enable self-enrol allowlist"
        self.fields["self_enrol_enabled"].help_text = (
            "Students can use the course self-enrol URL only if their exact email address has been added to the allowlist."
        )
        self.fields["self_enrol_domain"].label = "Restrict enrolment domain"
        self.fields["self_enrol_domain"].help_text = (
            "Optional. Enter a domain such as example.ac.uk. It applies to both self-enrol allowlist signups and magic links."
        )
        self.fields["demo_enabled"].label = "Enable public demo mode"
        self.fields["demo_enabled"].help_text = (
            "Publishes a no-login student demo link for this course. Demo practice is shared across all visitors."
        )
        if can_edit_homepage_demo:
            self.fields["homepage_demo_enabled"].label = "Show this demo on the MCQ Anchor homepage"
            self.fields["homepage_demo_enabled"].help_text = (
                "Superusers only. Adds this course's public demo to the public homepage demo section."
            )
        else:
            self.fields.pop("homepage_demo_enabled", None)
        self.fields["demo_iframe_allowed_origins"].label = "Allowed iframe origins"
        self.fields["demo_iframe_allowed_origins"].help_text = (
            "Optional. Enter exact origins such as https://yourinstitution.instructure.com, separated by commas or new lines."
        )
        self.fields["demo_iframe_allowed_origins"].widget = forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "https://yourinstitution.instructure.com",
            }
        )
        _apply_question_setting_field_attributes(self.fields)
        self.fields["practice_weight"].help_text = "Weighting of practice relative to validation in the overall course score."
        self.fields["validation_weight"].help_text = (
            "Weighting of validation relative to practice in the overall course score."
        )
        self.fields["mastery_weight"].help_text = "Weighting of correctness within overall practice scoring."
        self.fields["coverage_weight"].help_text = "Weighting of learning-objective coverage within overall practice scoring."
        self.fields["engagement_weight"].help_text = (
            "Weighting of on-time practice activity within overall practice scoring."
        )
        self.fields["allow_pre_engagement"].label = "Allow pre-engagement before release"
        self.fields["allow_pre_engagement"].help_text = (
            "If enabled, students can practise unreleased blocks early. "
            "Any answers submitted before the block release date receive full engagement credit."
        )
        self.fields["engagement_half_life_days"].label = "Engagement half-life (days)"
        self.fields["engagement_half_life_days"].help_text = (
            "Optional. Engagement decays exponentially from each block release date. "
            "After one half-life, an answered question counts for 50%; after two, 25%. "
            "Leave blank to measure engagement by completed questions only."
        )
        self.fields["revalidation_attempts"].help_text = "Number of additional validation attempts permitted after the first."
        self.fields["show_validation_feedback_immediately"].label = "Release validation feedback immediately"
        self.fields["show_validation_feedback_immediately"].help_text = (
            "If enabled, students can review validation feedback as soon as they submit."
        )

        for name, field in self.fields.items():
            widget_classes = field.widget.attrs.get("class", "").split()
            widget_classes.extend(["course-setting-input"])
            if isinstance(field.widget, forms.CheckboxInput):
                widget_classes.append("course-setting-input-checkbox")
            field.widget.attrs["class"] = " ".join(dict.fromkeys(widget_classes))
            field.widget.attrs["data-course-config-input"] = name

    def clean_self_enrol_domain(self):
        value = (self.cleaned_data.get("self_enrol_domain") or "").strip().lower()
        if value.startswith("@"):
            value = value[1:]
        return value

    def clean_assistant_guidance(self):
        return sanitize_assistant_guidance(self.cleaned_data.get("assistant_guidance", ""))

    def clean_demo_iframe_allowed_origins(self):
        return normalize_demo_iframe_origins(self.cleaned_data.get("demo_iframe_allowed_origins", ""))


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


class BlockConfigForm(forms.ModelForm):
    class Meta:
        model = BlockConfig
        fields = [
            "assistant_guidance",
            "distractor_count",
            "numeric_ratio_percent",
            "maq_ratio_percent",
            "waq_ratio_percent",
            "coding_question_ratio_percent",
            "advanced_question_start_percent",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_question_setting_field_attributes(self.fields, block_level=True)
        for name, field in self.fields.items():
            widget_classes = field.widget.attrs.get("class", "").split()
            widget_classes.extend(["course-setting-input", "block-setting-input"])
            field.widget.attrs["class"] = " ".join(dict.fromkeys(widget_classes))
            field.widget.attrs["data-block-config-input"] = name

    def clean_assistant_guidance(self):
        return sanitize_assistant_guidance(self.cleaned_data.get("assistant_guidance", ""))


class BlockConfigTargetQuestionCountInlineForm(forms.ModelForm):
    class Meta:
        model = BlockConfig
        fields = ["target_question_count"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_question_count"].label = "Engagement target"

    def clean_target_question_count(self):
        value = self.cleaned_data["target_question_count"]
        if value is None:
            raise forms.ValidationError("Please enter an engagement target.")
        if value < 1:
            raise forms.ValidationError("Engagement target must be at least 1 question.")
        return value


class BlockProjectCreateForm(forms.ModelForm):
    class Meta:
        model = BlockProject
        fields = ["teacher_prompt", "example_text"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teacher_prompt"].label = "Teacher prompt"
        self.fields["teacher_prompt"].widget = forms.Textarea(
            attrs={
                "rows": 5,
                "placeholder": "Describe the mini-project you want students to complete.",
            }
        )
        self.fields["example_text"].label = "Example text"
        self.fields["example_text"].required = False
        self.fields["example_text"].widget = forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": "Optional example project wording or scaffold.",
            }
        )

    def clean_teacher_prompt(self):
        prompt = (self.cleaned_data.get("teacher_prompt") or "").strip()
        if not prompt:
            raise forms.ValidationError("Add a teacher prompt first.")
        return prompt


class BlockProjectEditForm(forms.ModelForm):
    spec_json_text = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}), label="Engine parameters (JSON)")
    hint_plan_json_text = forms.CharField(widget=forms.Textarea(attrs={"rows": 7}), label="Hint plan (JSON)")

    class Meta:
        model = BlockProject
        fields = [
            "title",
            "teacher_prompt",
            "example_text",
            "student_instructions",
            "answer_label",
            "answer_unit",
            "decimal_places",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].help_text = "Student-facing project title."
        self.fields["teacher_prompt"].widget = forms.Textarea(attrs={"rows": 4})
        self.fields["example_text"].required = False
        self.fields["example_text"].widget = forms.Textarea(attrs={"rows": 3})
        self.fields["student_instructions"].widget = forms.Textarea(attrs={"rows": 6})
        self.fields["answer_unit"].required = False
        self.fields["spec_json_text"].initial = json.dumps(self.instance.spec_json or {}, indent=2, sort_keys=True)
        self.fields["hint_plan_json_text"].initial = json.dumps(self.instance.hint_plan_json or {}, indent=2, sort_keys=True)

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Add a project title first.")
        return title

    def clean_answer_unit(self):
        return (self.cleaned_data.get("answer_unit") or "").strip()

    def clean_teacher_prompt(self):
        prompt = (self.cleaned_data.get("teacher_prompt") or "").strip()
        if not prompt:
            raise forms.ValidationError("Add a teacher prompt first.")
        return prompt

    def clean_student_instructions(self):
        instructions = (self.cleaned_data.get("student_instructions") or "").strip()
        if not instructions:
            raise forms.ValidationError("Add student-facing instructions before publishing.")
        return instructions

    def clean_spec_json_text(self):
        value = (self.cleaned_data.get("spec_json_text") or "").strip()
        if not value:
            raise forms.ValidationError("Add engine parameters JSON first.")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError("Enter valid JSON for the engine parameters.") from exc
        if not isinstance(parsed, dict):
            raise forms.ValidationError("Engine parameters must be a JSON object.")
        return parsed

    def clean_hint_plan_json_text(self):
        value = (self.cleaned_data.get("hint_plan_json_text") or "").strip()
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError("Enter valid JSON for the hint plan.") from exc
        if not isinstance(parsed, dict):
            raise forms.ValidationError("Hint plan must be a JSON object.")
        return parsed

    def save(self, commit=True):
        project = super().save(commit=False)
        project.spec_json = self.cleaned_data["spec_json_text"]
        project.hint_plan_json = self.cleaned_data["hint_plan_json_text"]
        if commit:
            project.save()
        return project


class LearningObjectiveInlineForm(forms.ModelForm):
    class Meta:
        model = LearningObjective
        fields = ["text"]

    def clean_text(self):
        text = sanitize_learning_objective(self.cleaned_data["text"])
        if not text:
            raise forms.ValidationError("Please enter a learning objective.")
        return text


class LearningObjectiveGuidanceInlineForm(forms.ModelForm):
    class Meta:
        model = LearningObjective
        fields = ["assistant_guidance"]

    def clean_assistant_guidance(self):
        return sanitize_assistant_guidance(self.cleaned_data.get("assistant_guidance", ""))


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
        fields = [
            "starts_at",
            "ends_at",
            "location",
            "capacity",
            "late_booking_cutoff_minutes",
            "question_count",
            "time_limit_minutes",
            "audit_prompt_count",
            "feedback_release_mode",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, course=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.course = course
        self.fields["time_limit_minutes"].label = "Time limit (minutes)"
        self.fields["audit_prompt_count"].label = "Digital room-code audits"
        self.fields["audit_prompt_count"].help_text = "Choose 0, 2, or 3 room-code interruptions."
        self.fields["feedback_release_mode"].label = "Review release"
        self.fields["feedback_release_mode"].help_text = (
            "Immediate shows score and review as soon as the validation completes. Manual keeps review hidden until released."
        )
        self.fields["question_count"].label = "Validation questions"
        self.fields["ends_at"].label = "Session ends"
        self.fields["ends_at"].required = True
        self.fields["ends_at"].help_text = "Students may arrive at any point between the start and this end time. Sessions must be at least 50 minutes long."
        self.fields["late_booking_cutoff_minutes"].label = "Stop booking this many minutes before session end"
        self.fields["late_booking_cutoff_minutes"].required = True
        self.fields["late_booking_cutoff_minutes"].help_text = (
            "Students can still book while the session is running, until this buffer before the end time."
        )
        if course is not None:
            self.fields["feedback_release_mode"].initial = (
                ValidationEvent.FeedbackReleaseMode.IMMEDIATE
                if course.config.show_validation_feedback_immediately
                else ValidationEvent.FeedbackReleaseMode.MANUAL
            )
        self.initial["audit_prompt_count"] = self.initial.get("audit_prompt_count") or 2

    def clean(self):
        cleaned_data = super().clean()
        starts_at = cleaned_data.get("starts_at")
        ends_at = cleaned_data.get("ends_at")
        late_booking_cutoff_minutes = int(cleaned_data.get("late_booking_cutoff_minutes") or 0)
        if not ends_at:
            raise forms.ValidationError("Please provide a session end time.")
        if starts_at and ends_at and ends_at <= starts_at:
            raise forms.ValidationError("Session end time must be after the validation start.")
        if starts_at and ends_at and (ends_at - starts_at) < timedelta(minutes=50):
            raise forms.ValidationError("Validation sessions must be at least 50 minutes long.")
        if late_booking_cutoff_minutes < 0:
            raise forms.ValidationError("Late-booking cutoff must be zero or more minutes.")
        audit_prompt_count = int(cleaned_data.get("audit_prompt_count") or 0)
        if audit_prompt_count not in {0, 2, 3}:
            raise forms.ValidationError("Digital invigilation audit prompts must be set to 0, 2, or 3.")
        if self.course is not None:
            released_exists = self.course.blocks.filter(available_from__lte=timezone.localdate()).exists()
            if not released_exists:
                raise forms.ValidationError("Validation sessions require at least one released content block in this course.")
        return cleaned_data

    def save(self, commit=True):
        event = super().save(commit=False)
        if not str(getattr(event, "title", "") or "").strip():
            start_label = event.starts_at.strftime("%d %b %Y %H:%M") if event.starts_at else "TBC"
            event.title = f"Validation session {start_label}"
        if event.ends_at is not None:
            event.freeze_at = event.ends_at - timedelta(minutes=max(0, int(event.late_booking_cutoff_minutes or 0)))
        if commit:
            event.save()
        return event


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
