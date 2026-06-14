from django.contrib import admin

from .models import ChatConversation, ChatMessage, PilotEnquiry


@admin.register(PilotEnquiry)
class PilotEnquiryAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "institution", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("name", "email", "institution", "module_or_subject")
    readonly_fields = ("created_at",)


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ("role", "content", "created_at")
    can_delete = False


@admin.register(ChatConversation)
class ChatConversationAdmin(admin.ModelAdmin):
    list_display = ("public_id", "session_key", "ip_address", "started_at", "last_message_at")
    search_fields = ("public_id", "session_key", "ip_address", "user_agent")
    readonly_fields = ("public_id", "session_key", "ip_address", "user_agent", "started_at", "last_message_at")
    inlines = [ChatMessageInline]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "role", "created_at")
    list_filter = ("role", "created_at")
    search_fields = ("content", "conversation__public_id", "conversation__session_key")
    readonly_fields = ("conversation", "role", "content", "created_at")
