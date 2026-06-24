"""
skills/admin.py — Django-admin registrations for the skills app, with bulk
approve / reject actions for the course-review queue and manual-UPI payments.

The student-facing approval API lives in course_views (AdminSkillCourseQueueView
etc.); these admin actions are the staff-side equivalent for doing it in bulk
from the Django admin changelist.
"""
from django.contrib import admin
from django.utils import timezone

from .models import (
    SkillCategory, ExpertProfile, TeacherApplication, SkillSession,
)
from .course_models import (
    SkillCourse, SkillCourseSection, SkillCourseLecture,
    SkillCourseEnrollment,
)
from .review_models import ExpertReview
from .payment_models import SkillPaymentRequest


# ─────────────────────────────────────────────────────────────────────────
# Skill courses — bulk approve / reject
# ─────────────────────────────────────────────────────────────────────────

@admin.register(SkillCourse)
class SkillCourseAdmin(admin.ModelAdmin):
    list_display = ("title", "teacher_profile", "level", "status", "price_rupees", "created_at")
    list_filter = ("status", "level", "category")
    search_fields = ("title", "subtitle")
    actions = ("approve_courses", "reject_courses")

    @admin.action(description="Approve selected courses (go live)")
    def approve_courses(self, request, queryset):
        updated = queryset.update(
            status=SkillCourse.STATUS_APPROVED,
            reject_reason="",
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"{updated} course(s) approved and live.")

    @admin.action(description="Reject selected courses")
    def reject_courses(self, request, queryset):
        updated = queryset.update(
            status=SkillCourse.STATUS_REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"{updated} course(s) rejected.")


# ─────────────────────────────────────────────────────────────────────────
# Manual-UPI payment requests — bulk approve / reject
# ─────────────────────────────────────────────────────────────────────────

@admin.register(SkillPaymentRequest)
class SkillPaymentRequestAdmin(admin.ModelAdmin):
    list_display = ("learner_profile", "purpose", "amount_rupees", "upi_reference", "status", "created_at")
    list_filter = ("status", "purpose")
    search_fields = ("upi_reference", "payer_vpa")
    actions = ("approve_payments", "reject_payments")

    @admin.action(description="Approve selected payments (unlock access)")
    def approve_payments(self, request, queryset):
        updated = queryset.update(
            status=SkillPaymentRequest.STATUS_APPROVED,
            reject_reason="",
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"{updated} payment request(s) approved.")

    @admin.action(description="Reject selected payments")
    def reject_payments(self, request, queryset):
        updated = queryset.update(
            status=SkillPaymentRequest.STATUS_REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"{updated} payment request(s) rejected.")


# ─────────────────────────────────────────────────────────────────────────
# Lightweight registrations for visibility
# ─────────────────────────────────────────────────────────────────────────

@admin.register(TeacherApplication)
class TeacherApplicationAdmin(admin.ModelAdmin):
    list_display = ("user", "skill_name", "track", "status", "created_at")
    list_filter = ("status", "track")
    search_fields = ("skill_name", "user__email")


admin.site.register(SkillCategory)
admin.site.register(ExpertProfile)
admin.site.register(SkillSession)
admin.site.register(SkillCourseSection)
admin.site.register(SkillCourseLecture)
admin.site.register(SkillCourseEnrollment)
admin.site.register(ExpertReview)
