from django.contrib import admin

from .models import PilotEnquiry


@admin.register(PilotEnquiry)
class PilotEnquiryAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "institution", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("name", "email", "institution", "module_or_subject")
    readonly_fields = ("created_at",)
