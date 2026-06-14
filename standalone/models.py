import uuid
from datetime import timedelta

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
    practice_weight = models.PositiveSmallIntegerField(default=80, validators=[MinValueValidator(0), MaxValueValidator(100)])
    validation_weight = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(0), MaxValueValidator(100)])
    mastery_weight = models.PositiveSmallIntegerField(default=40, validators=[MinValueValidator(0), MaxValueValidator(100)])
    coverage_weight = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(0), MaxValueValidator(100)])
    engagement_weight = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(0), MaxValueValidator(100)])
    target_weight = models.PositiveSmallIntegerField(default=10, validators=[MinValueValidator(0), MaxValueValidator(100)])
    distractor_count = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1), MaxValueValidator(5)])
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
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="blocks")
    title = models.CharField(max_length=255)
    summary = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["order", "created_at"]

    def __str__(self) -> str:
        return f"{self.course}: {self.title}"


class BlockConfig(TimeStampedModel):
    block = models.OneToOneField(CourseBlock, on_delete=models.CASCADE, related_name="config")
    release_date = models.DateTimeField(null=True, blank=True)
    target_weight_override = models.PositiveSmallIntegerField(
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
    code = models.CharField(max_length=50)
    text = models.TextField()

    class Meta:
        ordering = ["block__order", "code"]

    def __str__(self) -> str:
        return f"{self.code}: {self.text[:60]}"


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


class QuestionBankItem(TimeStampedModel):
    class BankType(models.TextChoices):
        PRACTICE = "practice", "Practice"
        VALIDATION = "validation", "Validation"

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
    correct_answer = models.TextField()
    distractors = models.JSONField(default=list, blank=True)
    explanation = models.TextField(blank=True)
    difficulty = models.CharField(max_length=50, blank=True)
    question_hash = models.CharField(max_length=64, db_index=True)
    is_numerical = models.BooleanField(default=False)

    class Meta:
        ordering = ["bank_type", "block__order", "created_at"]

    def __str__(self) -> str:
        return f"{self.bank_type}: {self.stem[:80]}"


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


class ValidationEvent(TimeStampedModel):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="validation_events")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="validation_events_created")
    title = models.CharField(max_length=255)
    starts_at = models.DateTimeField()
    location = models.CharField(max_length=255)
    capacity = models.PositiveSmallIntegerField(default=30)
    freeze_at = models.DateTimeField()
    question_count = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ["starts_at"]

    def __str__(self) -> str:
        return f"{self.course} validation at {self.starts_at:%Y-%m-%d %H:%M}"

    @property
    def booked_count(self) -> int:
        return self.bookings.filter(status=ValidationBooking.Status.BOOKED).count()

    @property
    def has_space(self) -> bool:
        return self.booked_count < self.capacity


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
    qr_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    requires_manual_review = models.BooleanField(default=False)
    reviewer_notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Submission for {self.booking}"


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
