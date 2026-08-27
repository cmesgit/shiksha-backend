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
    SkillCategory, ExpertProfile, SkillSession,
)
from .course_models import (
    SkillCourse, SkillCourseSection, SkillCourseLecture,
    SkillCourseEnrollment,
)
from .review_models import ExpertReview
from .payment_models import SkillPaymentRequest
from .marketing_models import SkillMarketingBlock
from .listing_models import SkillListing, ListingModerationFlag


@admin.register(SkillListing)
class SkillListingAdmin(admin.ModelAdmin):
    list_display = ("title", "expert", "category", "price_paise",
                    "is_active", "is_suspended", "rating", "sessions_count")
    list_filter = ("is_active", "is_suspended", "category")
    search_fields = ("title", "description")
    # `is_suspended` is the admin-only takedown switch — a teacher's own pause
    # toggle (is_active) cannot lift it. See listing_views.patch.
    actions = ["suspend_listings", "unsuspend_listings"]

    @admin.action(description="Suspend selected skills")
    def suspend_listings(self, request, queryset):
        queryset.update(is_suspended=True)

    @admin.action(description="Lift suspension on selected skills")
    def unsuspend_listings(self, request, queryset):
        queryset.update(is_suspended=False)


@admin.register(ListingModerationFlag)
class ListingModerationFlagAdmin(admin.ModelAdmin):
    list_display = ("expert", "reason", "listing", "is_open", "created_at")
    list_filter = ("is_open", "reason")


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

admin.site.register(SkillCategory)
admin.site.register(ExpertProfile)
admin.site.register(SkillSession)
admin.site.register(SkillCourseSection)
admin.site.register(SkillCourseLecture)
admin.site.register(SkillCourseEnrollment)
admin.site.register(ExpertReview)


@admin.register(SkillMarketingBlock)
class SkillMarketingBlockAdmin(admin.ModelAdmin):
    list_display = ("key", "heading", "is_active", "updated_at")
    list_filter = ("is_active",)


# ─────────────────────────────────────────────────────────────────────────
# Guest-expert advertising subscriptions — bulk approve
# ─────────────────────────────────────────────────────────────────────────

from .subscription_models import ExpertAdSubscription  # noqa: E402


@admin.register(ExpertAdSubscription)
class ExpertAdSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("expert", "plan", "status", "amount", "current_period_end", "updated_at")
    list_filter = ("status", "plan")
    search_fields = ("expert__teacher_profile__user__email", "upi_reference")
    actions = ("approve_subscriptions", "cancel_subscriptions")

    @admin.action(description="Approve / activate selected subscriptions (30 days)")
    def approve_subscriptions(self, request, queryset):
        n = 0
        for sub in queryset:
            sub.activate(reviewer=request.user)
            n += 1
        self.message_user(request, f"{n} subscription(s) activated.")

    @admin.action(description="Cancel selected subscriptions")
    def cancel_subscriptions(self, request, queryset):
        n = 0
        for sub in queryset:
            sub.cancel()
            n += 1
        self.message_user(request, f"{n} subscription(s) cancelled.")
