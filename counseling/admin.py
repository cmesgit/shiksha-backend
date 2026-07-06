# PLACEMENT: backend/backend/counseling/admin.py   (NEW FILE)
from django.contrib import admin

from .models import (
    Appointment, AssessmentResponse, AssessmentTemplate, AvailabilitySlot,
    CounselingIntake, CounselorProfile, SessionNote, SessionReport,
    Specialization,
)


@admin.register(CounselorProfile)
class CounselorProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "user", "status", "is_listed", "avg_rating", "created_at")
    list_filter = ("status", "is_listed")
    search_fields = ("display_name", "user__email")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("learner_profile", "counselor", "scheduled_at", "status")
    list_filter = ("status",)


admin.site.register(Specialization)
admin.site.register(AvailabilitySlot)
admin.site.register(CounselingIntake)
admin.site.register(AssessmentTemplate)
admin.site.register(AssessmentResponse)
admin.site.register(SessionNote)
admin.site.register(SessionReport)
