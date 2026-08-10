# PLACEMENT: backend/backend/counseling/admin.py   (NEW FILE)
from django.contrib import admin

from .guide_models import CareerGuide, GuideChapter, GuideSection
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


class GuideSectionInline(admin.TabularInline):
    model = GuideSection
    extra = 0
    fields = ("order", "chapter", "level", "title", "kind", "audience", "anchor")
    ordering = ("order",)
    show_change_link = True


@admin.register(CareerGuide)
class CareerGuideAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "stage_label", "status", "publish_at", "view_count", "section_count")
    list_filter = ("status", "stage")
    search_fields = ("title", "slug", "blurb")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [GuideSectionInline]

    def section_count(self, obj):
        return obj.sections.count()


@admin.register(GuideChapter)
class GuideChapterAdmin(admin.ModelAdmin):
    list_display = ("guide", "number", "title", "kind")
    list_filter = ("kind",)
    search_fields = ("title", "guide__title")


@admin.register(GuideSection)
class GuideSectionAdmin(admin.ModelAdmin):
    list_display = ("guide", "chapter", "order", "title", "kind", "audience")
    list_filter = ("kind", "audience")
    search_fields = ("title", "guide__title")
