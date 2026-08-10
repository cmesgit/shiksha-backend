from django.contrib import admin

from .models import (
    CheatSignalEvent,
    ExamAnswer,
    ExamQuestion,
    ExamSession,
    GuardianVerification,
    ScholarshipAward,
    ScholarshipBand,
    ScholarshipEligibilityRecord,
    ScholarshipQuestionBankItem,
    ScholarshipSettings,
)


@admin.register(ScholarshipSettings)
class ScholarshipSettingsAdmin(admin.ModelAdmin):
    # Singleton — no add/delete from the admin.
    def has_add_permission(self, request):
        return not ScholarshipSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScholarshipBand)
class ScholarshipBandAdmin(admin.ModelAdmin):
    list_display = ("min_correct", "max_correct", "discount_pct", "is_active")
    list_filter = ("is_active",)
    ordering = ("-min_correct",)


@admin.register(ScholarshipQuestionBankItem)
class ScholarshipQuestionBankItemAdmin(admin.ModelAdmin):
    list_display = ("class_level", "subject", "difficulty", "is_active", "source", "created_at")
    list_filter = ("class_level", "subject", "difficulty", "is_active", "source")
    search_fields = ("text",)
    autocomplete_fields = ("created_by",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(GuardianVerification)
class GuardianVerificationAdmin(admin.ModelAdmin):
    list_display = ("account", "method", "status", "provider", "created_at", "reviewed_at")
    list_filter = ("method", "status", "provider")
    search_fields = ("account__email", "verified_adult_name")
    autocomplete_fields = ("account", "reviewed_by")
    readonly_fields = ("created_at", "updated_at", "consent_given_at", "consent_ip", "consent_user_agent")


@admin.register(ScholarshipEligibilityRecord)
class ScholarshipEligibilityRecordAdmin(admin.ModelAdmin):
    list_display = ("learner_profile", "academic_year", "status", "created_at")
    list_filter = ("status", "academic_year")
    search_fields = ("learner_profile__display_name", "dedup_hash")
    autocomplete_fields = ("learner_profile", "guardian_verification", "voided_by")
    readonly_fields = ("dedup_hash", "created_at")


class ExamQuestionInline(admin.TabularInline):
    model = ExamQuestion
    extra = 0
    fields = ("order", "subject", "difficulty", "text")
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = (
        "learner_profile", "course", "status", "score", "awarded_discount_pct",
        "flagged_for_review", "started_at", "deadline",
    )
    list_filter = ("status", "flagged_for_review", "review_status")
    search_fields = ("learner_profile__display_name", "course__title")
    autocomplete_fields = ("learner_profile", "course", "eligibility_record", "reviewed_by")
    readonly_fields = ("started_at", "submitted_at")
    inlines = [ExamQuestionInline]


@admin.register(ExamAnswer)
class ExamAnswerAdmin(admin.ModelAdmin):
    list_display = ("question", "selected_option_index", "is_correct", "answered_at")
    list_filter = ("is_correct",)


@admin.register(CheatSignalEvent)
class CheatSignalEventAdmin(admin.ModelAdmin):
    list_display = ("session", "event_type", "created_at")
    list_filter = ("event_type",)
    autocomplete_fields = ("session",)


@admin.register(ScholarshipAward)
class ScholarshipAwardAdmin(admin.ModelAdmin):
    list_display = ("learner_profile", "course", "discount_pct", "status", "academic_year", "expires_at")
    list_filter = ("status", "academic_year")
    search_fields = ("learner_profile__display_name", "course__title")
    autocomplete_fields = ("learner_profile", "course", "exam_session", "voided_by")
    readonly_fields = ("created_at", "redeemed_at")
