import csv

from django.contrib import admin
from django.http import HttpResponse

from standalone.models import (
    BlockConfig,
    ContentAsset,
    ContentChunk,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    CourseMagicLink,
    Enrollment,
    LearningObjective,
    NotificationLog,
    PracticeAttempt,
    PracticeAttemptQuestion,
    QuestionBankItem,
    StudentInvitation,
    StudentProfile,
    TeacherInvitation,
    TeacherProfile,
    User,
    ValidationBooking,
    ValidationEvent,
    ValidationPack,
    ValidationSubmission,
)


class CsvExportMixin:
    csv_filename = "export.csv"
    csv_fields: tuple[str, ...] = ()
    csv_headers: tuple[str, ...] | None = None

    @admin.action(description="Export selected as CSV")
    def export_selected_as_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{self.csv_filename}"'
        writer = csv.writer(response)
        writer.writerow(self.csv_headers or self.csv_fields)
        for obj in self.get_csv_queryset(queryset):
            writer.writerow(self.get_csv_row(obj))
        return response

    def get_csv_queryset(self, queryset):
        return queryset

    def get_csv_row(self, obj):
        return [getattr(obj, field) for field in self.csv_fields]


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "email", "role", "is_staff", "is_email_verified")
    list_filter = ("role", "is_staff", "is_superuser", "is_email_verified")
    search_fields = ("username", "email", "first_name", "last_name")


admin.site.register(TeacherProfile)
admin.site.register(StudentProfile)
admin.site.register(TeacherInvitation)
admin.site.register(StudentInvitation)
admin.site.register(Course)
admin.site.register(CourseConfig)
admin.site.register(CourseAllowedEmail)
admin.site.register(CourseMagicLink)
admin.site.register(CourseBlock)
admin.site.register(BlockConfig)
admin.site.register(ContentAsset)
admin.site.register(ContentChunk)
admin.site.register(LearningObjective)
admin.site.register(QuestionBankItem)
admin.site.register(ValidationPack)
admin.site.register(ValidationSubmission)
admin.site.register(NotificationLog)
admin.site.register(PracticeAttemptQuestion)


@admin.register(Enrollment)
class EnrollmentAdmin(CsvExportMixin, admin.ModelAdmin):
    list_display = ("course", "student", "status", "source", "mastery_score", "coverage_score")
    search_fields = ("course__title", "student__email", "student__username")
    actions = ("export_selected_as_csv",)
    csv_filename = "standalone-enrollments.csv"
    csv_fields = (
        "course",
        "student",
        "status",
        "source",
        "mastery_score",
        "coverage_score",
        "engagement_score",
        "target_score",
        "created_at",
    )


@admin.register(PracticeAttempt)
class PracticeAttemptAdmin(CsvExportMixin, admin.ModelAdmin):
    list_display = ("enrollment", "attempt_type", "score", "started_at", "completed_at")
    list_filter = ("attempt_type",)
    actions = ("export_selected_as_csv",)
    csv_filename = "standalone-practice-attempts.csv"
    csv_headers = (
        "course",
        "student",
        "attempt_type",
        "score",
        "started_at",
        "completed_at",
        "question_count",
    )

    def get_csv_queryset(self, queryset):
        return queryset.select_related("enrollment__course", "enrollment__student").prefetch_related("attempt_questions")

    def get_csv_row(self, obj):
        return [
            obj.enrollment.course.title,
            obj.enrollment.student.email,
            obj.attempt_type,
            obj.score,
            obj.started_at.isoformat(),
            obj.completed_at.isoformat() if obj.completed_at else "",
            obj.attempt_questions.count(),
        ]


@admin.register(ValidationEvent)
class ValidationEventAdmin(CsvExportMixin, admin.ModelAdmin):
    list_display = ("course", "title", "starts_at", "location", "capacity")
    actions = ("export_selected_as_csv",)
    csv_filename = "standalone-validation-events.csv"
    csv_fields = ("course", "title", "starts_at", "location", "capacity", "freeze_at", "question_count")


@admin.register(ValidationBooking)
class ValidationBookingAdmin(CsvExportMixin, admin.ModelAdmin):
    list_display = ("event", "enrollment", "status", "created_at")
    list_filter = ("status",)
    actions = ("export_selected_as_csv",)
    csv_filename = "standalone-validation-bookings.csv"
    csv_headers = ("course", "student", "event", "status", "created_at", "cancelled_at")

    def get_csv_queryset(self, queryset):
        return queryset.select_related("event__course", "enrollment__student")

    def get_csv_row(self, obj):
        return [
            obj.event.course.title,
            obj.enrollment.student.email,
            obj.event.title,
            obj.status,
            obj.created_at.isoformat(),
            obj.cancelled_at.isoformat() if obj.cancelled_at else "",
        ]
