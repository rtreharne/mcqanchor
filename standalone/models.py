import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        INTERNAL = "internal", "Internal"
        TEACHER = "teacher", "Teacher"
        STUDENT = "student", "Student"

    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STUDENT)
    is_email_verified = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.get_full_name() or self.email or self.username


class TeacherProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="teacher_profile")
    institution = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return f"Teacher profile for {self.user}"


class StudentProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student_profile")
    institution = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return f"Student profile for {self.user}"


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def project_artifact_upload_to(instance, filename: str) -> str:
    safe_name = Path(str(filename or "artifact")).name or "artifact"
    assignment_id = getattr(instance, "assignment_id", "unassigned")
    return f"standalone/project_artifacts/{assignment_id}/{safe_name}"


class TeacherInvitation(TimeStampedModel):
    email = models.EmailField(unique=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teacher_invitations_sent",
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teacher_invitation_record",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Teacher invitation for {self.email}"

    @classmethod
    def default_expiry(cls):
        return timezone.now() + timedelta(hours=settings.STANDALONE_INVITE_EXPIRY_HOURS)

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class Course(TimeStampedModel):
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="courses")
    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    summary = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title


class CourseConfig(TimeStampedModel):
    course = models.OneToOneField(Course, on_delete=models.CASCADE, related_name="config")
    self_enrol_enabled = models.BooleanField(default=True)
    self_enrol_domain = models.CharField(max_length=255, blank=True)
    demo_enabled = models.BooleanField(default=False)
    demo_iframe_allowed_origins = models.TextField(blank=True)
    assistant_guidance = models.TextField(blank=True)
    practice_weight = models.PositiveSmallIntegerField(default=80, validators=[MinValueValidator(0), MaxValueValidator(100)])
    validation_weight = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(0), MaxValueValidator(100)])
    mastery_weight = models.PositiveSmallIntegerField(default=40, validators=[MinValueValidator(0), MaxValueValidator(100)])
    coverage_weight = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(0), MaxValueValidator(100)])
    engagement_weight = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(0), MaxValueValidator(100)])
    allow_pre_engagement = models.BooleanField(default=False)
    engagement_half_life_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(3650)],
    )
    target_weight = models.PositiveSmallIntegerField(default=10, validators=[MinValueValidator(0), MaxValueValidator(100)])
    distractor_count = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1), MaxValueValidator(5)])
    numeric_ratio_percent = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    maq_ratio_percent = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(0), MaxValueValidator(100)])
    waq_ratio_percent = models.PositiveSmallIntegerField(default=10, validators=[MinValueValidator(0), MaxValueValidator(100)])
    coding_question_ratio_percent = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    advanced_question_start_percent = models.PositiveSmallIntegerField(default=50, validators=[MinValueValidator(0), MaxValueValidator(100)])
    revalidation_attempts = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    show_validation_feedback_immediately = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"Config for {self.course}"


class CourseAllowedEmail(TimeStampedModel):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="allowed_emails")
    email = models.EmailField()

    class Meta:
        unique_together = ("course", "email")
        ordering = ["email"]

    def __str__(self) -> str:
        return f"{self.email} for {self.course}"


class CourseMagicLink(TimeStampedModel):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="magic_links")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="magic_links_created")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = models.DateTimeField()
    max_uses = models.PositiveSmallIntegerField(default=1)
    use_count = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Magic link for {self.course}"

    @classmethod
    def default_expiry(cls):
        return timezone.now() + timedelta(hours=settings.STANDALONE_MAGIC_LINK_EXPIRY_HOURS)

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class CourseDemoAccess(TimeStampedModel):
    course = models.OneToOneField(Course, on_delete=models.CASCADE, related_name="demo_access")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    shared_practice_state = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["course__title"]

    def __str__(self) -> str:
        return f"Demo access for {self.course}"


class CourseDemoValidationSession(TimeStampedModel):
    demo_access = models.ForeignKey(CourseDemoAccess, on_delete=models.CASCADE, related_name="validation_sessions")
    visitor_key = models.CharField(max_length=64)
    validation_state = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-updated_at", "-created_at"]
        unique_together = ("demo_access", "visitor_key")

    def __str__(self) -> str:
        return f"Demo validation session for {self.demo_access.course} ({self.visitor_key})"


class Enrollment(TimeStampedModel):
    class Status(models.TextChoices):
        INVITED = "invited", "Invited"
        ACTIVE = "active", "Active"
        WITHDRAWN = "withdrawn", "Withdrawn"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="enrollments")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="enrollments")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    source = models.CharField(max_length=30, default="invite")
    mastery_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    coverage_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    engagement_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    target_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    mastery_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    coverage_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    engagement_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    target_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        unique_together = ("course", "student")
        ordering = ["course__title", "student__email"]

    def __str__(self) -> str:
        return f"{self.student} on {self.course}"


class StudentInvitation(TimeStampedModel):
    class InvitationType(models.TextChoices):
        EMAIL = "email", "Email invitation"
        MAGIC = "magic", "Magic link"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="student_invitations")
    email = models.EmailField(blank=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    invitation_type = models.CharField(max_length=20, choices=InvitationType.choices, default=InvitationType.EMAIL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student_invitations_created")
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    enrolled_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_student_invitations",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_invitation_type_display()} for {self.course}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class CourseBlock(TimeStampedModel):
    class RegenerationStatus(models.TextChoices):
        IDLE = "idle", "Idle"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="blocks")
    title = models.CharField(max_length=255)
    summary = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=1)
    available_from = models.DateField(default=timezone.localdate)
    regeneration_status = models.CharField(
        max_length=20,
        choices=RegenerationStatus.choices,
        default=RegenerationStatus.IDLE,
    )
    regeneration_progress = models.PositiveSmallIntegerField(default=0)
    regeneration_error = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "created_at"]

    def __str__(self) -> str:
        return f"{self.course}: {self.title}"

    def is_available(self, on_date=None) -> bool:
        comparison_date = on_date or timezone.localdate()
        return self.available_from <= comparison_date

    @property
    def preview_target_question_count(self) -> int:
        try:
            return self.config.target_question_count
        except BlockConfig.DoesNotExist:
            return 20

    def _block_config_or_none(self):
        try:
            return self.config
        except BlockConfig.DoesNotExist:
            return None

    def _resolved_text_override(self, block_field: str, course_field: str) -> str:
        config = self._block_config_or_none()
        value = getattr(config, block_field, "") if config is not None else ""
        if str(value or "").strip():
            return value
        return getattr(self.course.config, course_field)

    def _resolved_numeric_override(self, block_field: str, course_field: str) -> int:
        config = self._block_config_or_none()
        value = getattr(config, block_field, None) if config is not None else None
        if value is not None:
            return int(value)
        return int(getattr(self.course.config, course_field))

    @property
    def question_assistant_guidance(self) -> str:
        return self._resolved_text_override("assistant_guidance", "assistant_guidance")

    @property
    def question_distractor_count(self) -> int:
        return self._resolved_numeric_override("distractor_count", "distractor_count")

    @property
    def question_numeric_ratio_percent(self) -> int:
        return self._resolved_numeric_override("numeric_ratio_percent", "numeric_ratio_percent")

    @property
    def question_maq_ratio_percent(self) -> int:
        return self._resolved_numeric_override("maq_ratio_percent", "maq_ratio_percent")

    @property
    def question_waq_ratio_percent(self) -> int:
        return self._resolved_numeric_override("waq_ratio_percent", "waq_ratio_percent")

    @property
    def question_coding_question_ratio_percent(self) -> int:
        return self._resolved_numeric_override("coding_question_ratio_percent", "coding_question_ratio_percent")

    @property
    def question_advanced_question_start_percent(self) -> int:
        return self._resolved_numeric_override("advanced_question_start_percent", "advanced_question_start_percent")

    def question_type_ratio_targets(self) -> dict[str, float]:
        numeric_target = max(0.0, min(100.0, float(self.question_numeric_ratio_percent or 0)))
        maq_target = max(0.0, min(100.0, float(self.question_maq_ratio_percent or 0)))
        waq_target = max(0.0, min(100.0, float(self.question_waq_ratio_percent or 0)))
        mcq_target = max(0.0, 100.0 - numeric_target - maq_target - waq_target)
        total_target = mcq_target + numeric_target + maq_target + waq_target
        if total_target <= 0:
            return {
                QuestionBankItem.QuestionType.MCQ: 100.0,
                QuestionBankItem.QuestionType.NUM: 0.0,
                QuestionBankItem.QuestionType.MAQ: 0.0,
                QuestionBankItem.QuestionType.WAQ: 0.0,
            }
        return {
            QuestionBankItem.QuestionType.MCQ: (mcq_target * 100.0 / total_target),
            QuestionBankItem.QuestionType.NUM: (numeric_target * 100.0 / total_target),
            QuestionBankItem.QuestionType.MAQ: (maq_target * 100.0 / total_target),
            QuestionBankItem.QuestionType.WAQ: (waq_target * 100.0 / total_target),
        }


class BlockConfig(TimeStampedModel):
    block = models.OneToOneField(CourseBlock, on_delete=models.CASCADE, related_name="config")
    release_date = models.DateTimeField(null=True, blank=True)
    target_question_count = models.PositiveSmallIntegerField(default=20)
    target_weight_override = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    assistant_guidance = models.TextField(blank=True)
    distractor_count = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    numeric_ratio_percent = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    maq_ratio_percent = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    waq_ratio_percent = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    coding_question_ratio_percent = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    advanced_question_start_percent = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    def __str__(self) -> str:
        return f"Config for {self.block}"


class ContentAsset(TimeStampedModel):
    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    block = models.ForeignKey(CourseBlock, on_delete=models.CASCADE, related_name="assets")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="uploaded_assets")
    file = models.FileField(upload_to="standalone/assets/%Y/%m/%d")
    original_filename = models.CharField(max_length=255)
    extension = models.CharField(max_length=20)
    include_in_generation = models.BooleanField(default=True)
    processing_status = models.CharField(max_length=20, choices=ProcessingStatus.choices, default=ProcessingStatus.PENDING)
    extracted_text = models.TextField(blank=True)
    processing_error = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return self.original_filename


class LearningObjective(TimeStampedModel):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="learning_objectives")
    block = models.ForeignKey(CourseBlock, on_delete=models.CASCADE, related_name="learning_objectives")
    source_asset = models.ForeignKey(ContentAsset, on_delete=models.CASCADE, related_name="learning_objectives")
    position = models.PositiveSmallIntegerField(default=1)
    code = models.CharField(max_length=50)
    text = models.TextField()
    assistant_guidance = models.TextField(blank=True)

    class Meta:
        ordering = ["block__order", "position", "pk"]

    def __str__(self) -> str:
        return f"{self.code}: {self.text[:60]}"


class LearningObjectiveCorrection(TimeStampedModel):
    learning_objective = models.ForeignKey(
        LearningObjective,
        on_delete=models.CASCADE,
        related_name="corrections",
    )
    question = models.ForeignKey(
        "QuestionBankItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="learning_objective_corrections",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="learning_objective_corrections_created",
    )
    instruction = models.TextField()
    question_stem_snapshot = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"Correction for {self.learning_objective.code}"


class ContentChunk(TimeStampedModel):
    asset = models.ForeignKey(ContentAsset, on_delete=models.CASCADE, related_name="chunks")
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="content_chunks")
    block = models.ForeignKey(CourseBlock, on_delete=models.CASCADE, related_name="content_chunks")
    ordinal = models.PositiveIntegerField()
    text = models.TextField()
    token_count = models.PositiveIntegerField(default=0)
    embedding_model = models.CharField(max_length=100, blank=True)
    embedding_vector = models.JSONField(default=list, blank=True)
    checksum = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["asset", "ordinal"]
        unique_together = ("asset", "ordinal")

    def __str__(self) -> str:
        return f"Chunk {self.ordinal} for {self.asset}"


class CourseImport(TimeStampedModel):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        ANALYZING = "analyzing", "Analyzing"
        READY = "ready", "Ready"
        CREATING = "creating", "Creating blocks"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="imports")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="course_imports")
    source_file = models.FileField(upload_to="standalone/imports/%Y/%m/%d")
    original_filename = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPLOADED)
    progress = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.original_filename} for {self.course}"


class CourseImportChapter(TimeStampedModel):
    course_import = models.ForeignKey(CourseImport, on_delete=models.CASCADE, related_name="chapters")
    title = models.CharField(max_length=255)
    order = models.PositiveSmallIntegerField(default=1)
    start_page = models.PositiveIntegerField(default=1)
    end_page = models.PositiveIntegerField(default=1)
    confidence = models.PositiveSmallIntegerField(default=50)
    extracted_text = models.TextField(blank=True)
    selected = models.BooleanField(default=True)
    created_block = models.ForeignKey(
        CourseBlock,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_chapters",
    )

    class Meta:
        ordering = ["order", "start_page", "pk"]
        unique_together = ("course_import", "order")

    def __str__(self) -> str:
        return f"{self.course_import}: {self.title}"


class QuestionBankItem(TimeStampedModel):
    class BankType(models.TextChoices):
        PRACTICE = "practice", "Practice"
        VALIDATION = "validation", "Validation"

    class QuestionType(models.TextChoices):
        MCQ = "mcq", "Single-answer"
        NUM = "num", "Numeric"
        MAQ = "maq", "Multiple-answer"
        WAQ = "waq", "Written-answer"

    class CodingQuestionKind(models.TextChoices):
        COMPREHENSION = "comprehension", "Code comprehension"
        DEBUG = "debug", "Debugging"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        FLAGGED = "flagged", "Flagged"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="question_bank_items")
    block = models.ForeignKey(CourseBlock, on_delete=models.CASCADE, related_name="question_bank_items")
    learning_objective = models.ForeignKey(
        LearningObjective,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="question_bank_items",
    )
    source_chunk = models.ForeignKey(ContentChunk, on_delete=models.SET_NULL, null=True, blank=True, related_name="question_bank_items")
    bank_type = models.CharField(max_length=20, choices=BankType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    linked_question = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="linked_from")
    stem = models.TextField()
    question_type = models.CharField(max_length=20, choices=QuestionType.choices, default=QuestionType.MCQ)
    correct_answer = models.TextField()
    additional_correct_answers = models.JSONField(default=list, blank=True)
    written_answer_keywords = models.JSONField(default=list, blank=True)
    further_study_questions = models.JSONField(default=list, blank=True)
    distractors = models.JSONField(default=list, blank=True)
    explanation = models.TextField(blank=True)
    difficulty = models.CharField(max_length=50, blank=True)
    question_hash = models.CharField(max_length=64, db_index=True)
    is_numerical = models.BooleanField(default=False)
    numeric_metadata = models.JSONField(default=dict, blank=True)
    is_coding_question = models.BooleanField(default=False)
    coding_language = models.CharField(max_length=50, blank=True)
    coding_question_kind = models.CharField(max_length=30, choices=CodingQuestionKind.choices, blank=True)
    code_snippet = models.TextField(blank=True)

    class Meta:
        ordering = ["bank_type", "block__order", "created_at"]

    @classmethod
    def display_label_for_question_type(cls, question_type: str) -> str:
        return {
            cls.QuestionType.MCQ: "MCQ",
            cls.QuestionType.NUM: "Numerical MCQ",
            cls.QuestionType.MAQ: "Multiple-answer MCQ",
            cls.QuestionType.WAQ: "Written answer",
        }.get(question_type, "Question")

    def __str__(self) -> str:
        return f"{self.bank_type}: {self.stem[:80]}"

    def save(self, *args, **kwargs):
        self.is_numerical = self.question_type == self.QuestionType.NUM
        if not self.is_numerical and self.numeric_metadata:
            self.numeric_metadata = {}
        super().save(*args, **kwargs)

    def correct_answers(self) -> list[str]:
        answers = [self.correct_answer, *self.additional_correct_answers]
        normalized = []
        for answer in answers:
            cleaned = str(answer).strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    def all_answer_options(self) -> list[str]:
        if self.is_written_answer():
            return []
        options = []
        for option in [*self.correct_answers(), *self.distractors]:
            cleaned = str(option).strip()
            if cleaned and cleaned not in options:
                options.append(cleaned)
        return options

    def is_multiple_answer(self) -> bool:
        return self.question_type == self.QuestionType.MAQ and len(self.correct_answers()) > 1

    def is_written_answer(self) -> bool:
        return self.question_type == self.QuestionType.WAQ

    def is_numeric(self) -> bool:
        return self.question_type == self.QuestionType.NUM

    def question_type_label(self) -> str:
        return self.display_label_for_question_type(self.question_type)


class EnrollmentQuestionState(TimeStampedModel):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="question_states")
    question = models.ForeignKey(QuestionBankItem, on_delete=models.CASCADE, related_name="enrollment_states")
    times_presented = models.PositiveIntegerField(default=0)
    times_correct = models.PositiveIntegerField(default=0)
    times_incorrect = models.PositiveIntegerField(default=0)
    last_presented_sequence = models.PositiveIntegerField(default=0)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["enrollment", "question"]
        unique_together = ("enrollment", "question")

    def __str__(self) -> str:
        return f"State for {self.enrollment} / {self.question_id}"


class QuestionFlag(TimeStampedModel):
    question = models.ForeignKey(QuestionBankItem, on_delete=models.CASCADE, related_name="flags")
    flagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="question_flags",
    )
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="question_flags",
    )
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Flag for question {self.question_id}"


class PracticeAttempt(TimeStampedModel):
    class AttemptType(models.TextChoices):
        PRACTICE = "practice", "Practice"
        VALIDATION_PRACTICE = "validation_practice", "Validation practice"

    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="practice_attempts")
    attempt_type = models.CharField(max_length=30, choices=AttemptType.choices, default=AttemptType.PRACTICE)
    block = models.ForeignKey(CourseBlock, on_delete=models.SET_NULL, null=True, blank=True, related_name="practice_attempts")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    time_limit_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    feedback_visible_immediately = models.BooleanField(default=True)
    mastery_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    coverage_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    engagement_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    target_delta = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.attempt_type} attempt for {self.enrollment}"


class PracticeAttemptQuestion(TimeStampedModel):
    attempt = models.ForeignKey(PracticeAttempt, on_delete=models.CASCADE, related_name="attempt_questions")
    question = models.ForeignKey(QuestionBankItem, on_delete=models.CASCADE, related_name="attempt_questions")
    order = models.PositiveSmallIntegerField(default=1)
    selected_answer = models.TextField(blank=True)
    is_correct = models.BooleanField(default=False)
    feedback = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "created_at"]
        unique_together = ("attempt", "question")

    def __str__(self) -> str:
        return f"Question {self.order} in {self.attempt}"


class PracticeMessage(TimeStampedModel):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="practice_messages")
    block = models.ForeignKey(CourseBlock, on_delete=models.CASCADE, related_name="practice_messages")
    question = models.ForeignKey(
        QuestionBankItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="practice_messages",
    )
    attempt_question = models.ForeignKey(
        PracticeAttemptQuestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="practice_messages",
    )
    message_id = models.CharField(max_length=100)
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=20)
    kind = models.CharField(max_length=30)
    text = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    source_blocks = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["enrollment", "sequence", "created_at"]
        unique_together = (("enrollment", "message_id"), ("enrollment", "sequence"))

    def __str__(self) -> str:
        return f"{self.kind} message for {self.enrollment}"


class BlockProject(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    class EngineType(models.TextChoices):
        TABULAR_ANALYSIS = "tabular_analysis", "Tabular analysis"
        SEEDED_SCRIPT_OUTPUT = "seeded_script_output", "Seeded script output"

    class GenerationStatus(models.TextChoices):
        IDLE = "idle", "Idle"
        RUNNING = "running", "Running"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        UNSUPPORTED = "unsupported", "Unsupported"

    block = models.ForeignKey(CourseBlock, on_delete=models.CASCADE, related_name="projects")
    title = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    engine_type = models.CharField(max_length=30, choices=EngineType.choices, blank=True)
    teacher_prompt = models.TextField()
    example_text = models.TextField(blank=True)
    student_instructions = models.TextField(blank=True)
    answer_label = models.CharField(max_length=120, default="Answer")
    answer_unit = models.CharField(max_length=40, blank=True)
    decimal_places = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(6)],
    )
    spec_json = models.JSONField(default=dict, blank=True)
    hint_plan_json = models.JSONField(default=dict, blank=True)
    generation_status = models.CharField(
        max_length=20,
        choices=GenerationStatus.choices,
        default=GenerationStatus.IDLE,
    )
    generation_error = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["block__order", "created_at"]

    def __str__(self) -> str:
        return f"{self.block}: {self.title}"

    @property
    def is_locked(self) -> bool:
        return self.status == self.Status.PUBLISHED and self.assignments.exists()


class ProjectAssignment(TimeStampedModel):
    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETE = "complete", "Complete"

    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="project_assignments")
    block_project = models.ForeignKey(BlockProject, on_delete=models.CASCADE, related_name="assignments")
    seed = models.CharField(max_length=32)
    expected_answer_display = models.CharField(max_length=120)
    normalized_expected_answer = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    completed_at = models.DateTimeField(null=True, blank=True)
    submission_count = models.PositiveIntegerField(default=0)
    latest_submitted_answer = models.TextField(blank=True)
    latest_normalized_answer = models.CharField(max_length=120, blank=True)
    engine_payload_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["enrollment", "block_project", "created_at"]
        unique_together = ("enrollment", "block_project")

    def __str__(self) -> str:
        return f"Assignment for {self.enrollment} / {self.block_project}"


class ProjectArtifact(TimeStampedModel):
    class Kind(models.TextChoices):
        DATASET = "dataset", "Dataset"
        STARTER_SCRIPT = "starter_script", "Starter script"
        SUPPORTING_FILE = "supporting_file", "Supporting file"

    assignment = models.ForeignKey(ProjectAssignment, on_delete=models.CASCADE, related_name="artifacts")
    kind = models.CharField(max_length=30, choices=Kind.choices)
    label = models.CharField(max_length=120)
    file = models.FileField(upload_to=project_artifact_upload_to)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["assignment", "created_at"]

    def __str__(self) -> str:
        return f"{self.label} for {self.assignment}"


class ProjectSubmission(TimeStampedModel):
    assignment = models.ForeignKey(ProjectAssignment, on_delete=models.CASCADE, related_name="submissions")
    raw_answer = models.TextField(blank=True)
    normalized_answer = models.CharField(max_length=120, blank=True)
    is_correct = models.BooleanField(default=False)
    feedback_code = models.CharField(max_length=40, blank=True)
    feedback_text = models.TextField(blank=True)

    class Meta:
        ordering = ["assignment", "created_at", "pk"]

    def __str__(self) -> str:
        return f"Submission for {self.assignment}"


class ProjectMessage(TimeStampedModel):
    assignment = models.ForeignKey(ProjectAssignment, on_delete=models.CASCADE, related_name="messages")
    message_id = models.CharField(max_length=100)
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=20)
    kind = models.CharField(max_length=30)
    text = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["assignment", "sequence", "created_at"]
        unique_together = (("assignment", "message_id"), ("assignment", "sequence"))

    def __str__(self) -> str:
        return f"{self.kind} project message for {self.assignment}"


class ValidationEvent(TimeStampedModel):
    class Mode(models.TextChoices):
        SELF = "self", "Self-validation"
        DIGITAL_INVIGILATION = "digital_invigilation", "Digital invigilation"
        PAPER_INVIGILATION = "paper_invigilation", "Paper invigilation"

    class FeedbackReleaseMode(models.TextChoices):
        IMMEDIATE = "immediate", "Immediate"
        MANUAL = "manual", "Manual release"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="validation_events")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="validation_events_created")
    title = models.CharField(max_length=255)
    mode = models.CharField(max_length=30, choices=Mode.choices, default=Mode.SELF)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=255)
    capacity = models.PositiveSmallIntegerField(default=30)
    freeze_at = models.DateTimeField()
    late_booking_cutoff_minutes = models.PositiveSmallIntegerField(default=20)
    question_count = models.PositiveSmallIntegerField(default=10)
    time_limit_minutes = models.PositiveSmallIntegerField(default=20)
    audit_prompt_count = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(3)])
    feedback_release_mode = models.CharField(
        max_length=20,
        choices=FeedbackReleaseMode.choices,
        default=FeedbackReleaseMode.IMMEDIATE,
    )
    room_code_secret = models.CharField(max_length=64, blank=True)
    blocks = models.ManyToManyField(CourseBlock, related_name="validation_events", blank=True)

    class Meta:
        ordering = ["starts_at"]

    def __str__(self) -> str:
        return f"{self.course} validation at {self.starts_at:%Y-%m-%d %H:%M}"

    @property
    def booked_count(self) -> int:
        return self.bookings.filter(status=ValidationBooking.Status.BOOKED).count()

    @property
    def has_student_submissions(self) -> bool:
        return self.attempts.filter(attempt_questions__answered_at__isnull=False).exists()

    @property
    def can_be_deleted(self) -> bool:
        return not self.has_student_submissions

    @property
    def spaces_left(self) -> int:
        return max(0, int(self.capacity or 0) - self.booked_count)

    @property
    def has_space(self) -> bool:
        return self.spaces_left > 0

    @property
    def requires_booking(self) -> bool:
        return self.mode == self.Mode.DIGITAL_INVIGILATION

    @property
    def session_end_at(self):
        if self.ends_at is not None:
            return self.ends_at
        if self.freeze_at and self.freeze_at > self.starts_at:
            return self.freeze_at
        return self.starts_at + timedelta(minutes=max(1, int(self.time_limit_minutes or 20)))

    @property
    def booking_deadline(self):
        if self.mode == self.Mode.DIGITAL_INVIGILATION:
            if self.ends_at is not None:
                return self.ends_at - timedelta(minutes=max(0, int(self.late_booking_cutoff_minutes or 0)))
            return self.freeze_at
        return None

    def booking_is_open(self, at=None) -> bool:
        if not self.requires_booking:
            return False
        current_time = at or timezone.now()
        deadline = self.booking_deadline
        if deadline is None:
            return False
        return current_time < deadline and self.has_space

    def recent_booking_count(self, *, hours: int = 24) -> int:
        window_start = timezone.now() - timedelta(hours=hours)
        return self.bookings.filter(
            status=ValidationBooking.Status.BOOKED,
            updated_at__gte=window_start,
        ).count()


class ValidationBooking(TimeStampedModel):
    class Status(models.TextChoices):
        BOOKED = "booked", "Booked"
        CANCELLED = "cancelled", "Cancelled"

    event = models.ForeignKey(ValidationEvent, on_delete=models.CASCADE, related_name="bookings")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="validation_bookings")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BOOKED)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("event", "enrollment")
        ordering = ["event__starts_at", "enrollment__student__email"]

    def __str__(self) -> str:
        return f"{self.enrollment} - {self.event}"


class ValidationPack(TimeStampedModel):
    event = models.ForeignKey(ValidationEvent, on_delete=models.CASCADE, related_name="packs")
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="validation_packs_generated")
    generated_for_booking_count = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Pack for {self.event}"


class ValidationSubmission(TimeStampedModel):
    booking = models.OneToOneField(ValidationBooking, on_delete=models.CASCADE, related_name="submission")
    attempt = models.OneToOneField(
        "ValidationAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submission",
    )
    qr_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    requires_manual_review = models.BooleanField(default=False)
    reviewer_notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Submission for {self.booking}"


class ValidationAttempt(TimeStampedModel):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        EXPIRED = "expired", "Expired"
        VOIDED = "voided", "Voided"

    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="validation_attempts")
    event = models.ForeignKey(ValidationEvent, on_delete=models.CASCADE, related_name="attempts")
    booking = models.OneToOneField(
        ValidationBooking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attempt",
    )
    mode = models.CharField(max_length=30, choices=ValidationEvent.Mode.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    feedback_release_mode = models.CharField(
        max_length=20,
        choices=ValidationEvent.FeedbackReleaseMode.choices,
        default=ValidationEvent.FeedbackReleaseMode.IMMEDIATE,
    )
    review_released_at = models.DateTimeField(null=True, blank=True)
    requires_manual_review = models.BooleanField(default=False)
    navigation_warning_count = models.PositiveSmallIntegerField(default=0)
    invalidated_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        unique_together = ("enrollment", "event")

    def __str__(self) -> str:
        return f"{self.mode} validation for {self.enrollment}"

    @property
    def review_visible(self) -> bool:
        return bool(self.review_released_at)


class ValidationAttemptQuestion(TimeStampedModel):
    attempt = models.ForeignKey(ValidationAttempt, on_delete=models.CASCADE, related_name="attempt_questions")
    question = models.ForeignKey(QuestionBankItem, on_delete=models.CASCADE, related_name="validation_attempt_questions")
    order = models.PositiveSmallIntegerField(default=1)
    question_type = models.CharField(max_length=20, choices=QuestionBankItem.QuestionType.choices)
    selected_answers = models.JSONField(default=list, blank=True)
    answer_text = models.TextField(blank=True)
    is_correct = models.BooleanField(null=True, blank=True)
    feedback = models.TextField(blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "created_at"]
        unique_together = (("attempt", "order"), ("attempt", "question"))

    def __str__(self) -> str:
        return f"Validation question {self.order} in {self.attempt}"


class ValidationAttemptMessage(TimeStampedModel):
    attempt = models.ForeignKey(ValidationAttempt, on_delete=models.CASCADE, related_name="messages")
    question = models.ForeignKey(
        QuestionBankItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validation_attempt_messages",
    )
    attempt_question = models.ForeignKey(
        ValidationAttemptQuestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    message_id = models.CharField(max_length=100)
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=20)
    kind = models.CharField(max_length=30)
    text = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    source_blocks = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["attempt", "sequence", "created_at"]
        unique_together = (("attempt", "message_id"), ("attempt", "sequence"))

    def __str__(self) -> str:
        return f"{self.kind} validation message for {self.attempt}"


class ValidationAuditPrompt(TimeStampedModel):
    attempt = models.ForeignKey(ValidationAttempt, on_delete=models.CASCADE, related_name="audit_prompts")
    prompt_index = models.PositiveSmallIntegerField(default=1)
    due_at = models.DateTimeField()
    expected_code = models.CharField(max_length=64)
    presented_at = models.DateTimeField(null=True, blank=True)
    submitted_code = models.CharField(max_length=64, blank=True)
    is_correct = models.BooleanField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    message_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["attempt", "prompt_index", "due_at"]
        unique_together = (("attempt", "prompt_index"),)

    def __str__(self) -> str:
        return f"Audit prompt {self.prompt_index} for {self.attempt}"


class NotificationLog(TimeStampedModel):
    recipient_email = models.EmailField()
    event_type = models.CharField(max_length=60)
    subject = models.CharField(max_length=255)
    body_preview = models.TextField(blank=True)
    related_object = models.CharField(max_length=255, blank=True)
    delivered = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.event_type} to {self.recipient_email}"
