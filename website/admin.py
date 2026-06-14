import csv

from django.contrib import admin
from django.http import HttpResponse

from .models import ChatConversation, ChatMessage, PilotEnquiry


class CsvExportMixin:
    csv_filename = "export.csv"
    csv_fields: tuple[str, ...] = ()
    csv_headers: tuple[str, ...] | None = None

    @admin.action(description="Export selected as CSV")
    def export_selected_as_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{self.csv_filename}"'

        writer = csv.writer(response)
        headers = self.csv_headers or self.csv_fields
        writer.writerow(headers)

        for obj in self.get_csv_queryset(queryset):
            writer.writerow(self.get_csv_row(obj))

        return response

    def get_csv_queryset(self, queryset):
        return queryset

    def get_csv_row(self, obj):
        return [getattr(obj, field) for field in self.csv_fields]


@admin.register(PilotEnquiry)
class PilotEnquiryAdmin(CsvExportMixin, admin.ModelAdmin):
    list_display = ("name", "email", "institution", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("name", "email", "institution", "module_or_subject")
    readonly_fields = ("created_at",)
    actions = ("export_selected_as_csv",)
    csv_filename = "pilot-enquiries.csv"
    csv_fields = (
        "name",
        "email",
        "institution",
        "module_or_subject",
        "message",
        "status",
        "internal_notes",
        "created_at",
    )


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ("role", "content", "created_at")
    can_delete = False


@admin.register(ChatConversation)
class ChatConversationAdmin(CsvExportMixin, admin.ModelAdmin):
    list_display = ("public_id", "session_key", "ip_address", "started_at", "last_message_at")
    search_fields = ("public_id", "session_key", "ip_address", "user_agent")
    readonly_fields = ("public_id", "session_key", "ip_address", "user_agent", "started_at", "last_message_at")
    inlines = [ChatMessageInline]
    actions = ("export_selected_as_csv",)
    csv_filename = "chat-conversations.csv"
    csv_headers = (
        "public_id",
        "session_key",
        "ip_address",
        "user_agent",
        "started_at",
        "last_message_at",
        "message_count",
        "transcript",
    )

    def get_csv_queryset(self, queryset):
        return queryset.prefetch_related("messages")

    def get_csv_row(self, obj):
        transcript = "\n\n".join(
            f"[{message.created_at.isoformat()}] {message.role}: {message.content}"
            for message in obj.messages.all()
        )
        return [
            str(obj.public_id),
            obj.session_key,
            obj.ip_address or "",
            obj.user_agent,
            obj.started_at.isoformat(),
            obj.last_message_at.isoformat(),
            obj.messages.count(),
            transcript,
        ]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "role", "created_at")
    list_filter = ("role", "created_at")
    search_fields = ("content", "conversation__public_id", "conversation__session_key")
    readonly_fields = ("conversation", "role", "content", "created_at")
