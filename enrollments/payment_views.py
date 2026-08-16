"""
enrollments/payment_views.py — additive endpoints for the pluggable payment
layer. Kept in a separate module so nothing in the existing views.py changes.

Routes (wired in enrollments/urls.py):
    GET  /api/enrollments/payment-config/   → active payment mode (any logged-in user)
    POST /api/enrollments/free-enroll/      → instant enrollment when mode is "free"
    POST /api/enrollments/select-batch/     → student picks their own batch
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from accounts.permissions import IsEmailVerified
from accounts.auth_flow import get_active_profile, profile_mismatch_response
from courses.models import Course, Batch

from .models import Enrollment, Subscription
from .payments import get_payment_provider
from .services import get_active_subscription


def _validate_batch_choice(course, batch_id):
    """Validate a student-chosen batch_id against `course`, mirroring the
    checks admin's own batch-assignment paths enforce (_move_batch in
    admin_enrollment_views.py, AdminActionSerializer in serializers.py):
    must belong to this course, be active, and have room. Returns
    (batch, None) on success or (None, error_response) on failure — the
    error_response is a ready-to-return DRF Response.
    """
    batch = Batch.objects.select_for_update().filter(
        id=batch_id, course_id=course.id).first()
    if not batch:
        return None, Response(
            {"batch": "Batch not found for this course."},
            status=status.HTTP_400_BAD_REQUEST)
    if not batch.is_active:
        return None, Response(
            {"batch": "That batch is inactive."},
            status=status.HTTP_400_BAD_REQUEST)
    if batch.is_full:
        return None, Response(
            {"batch": f"'{batch.name}' is full ({batch.seats_taken}/{batch.capacity})."},
            status=status.HTTP_400_BAD_REQUEST)
    return batch, None


def _create_or_extend_subscription(*, user, learner, course):
    """Create (or extend) an active subscription for this learner+course.

    Defensive about the Subscription schema: only fields that actually exist on
    the model are passed, and `kind=PAID` is set only when the model defines it.
    This keeps the free path working regardless of trial-feature drift.
    """
    field_names = {f.name for f in Subscription._meta.get_fields()}
    days = getattr(course, "subscription_duration_days", None) or 30
    now = timezone.now()

    # Match on whatever owner field the model uses (learner_profile preferred).
    match = {"course": course, "status": Subscription.STATUS_ACTIVE, "expires_at__gt": now}
    if "learner_profile" in field_names and learner is not None:
        match["learner_profile"] = learner
    else:
        match["user"] = user

    active = Subscription.objects.filter(**match).order_by("-expires_at").first()
    if active:
        active.expires_at = active.expires_at + timedelta(days=days)
        active.save(update_fields=["expires_at"] + (["updated_at"] if "updated_at" in field_names else []))
        return active

    kwargs = {
        "user": user,
        "learner_profile": learner,
        "course": course,
        "starts_at": now,
        "expires_at": now + timedelta(days=days),
        "status": Subscription.STATUS_ACTIVE,
    }
    # Mark it PAID-equivalent when the model tracks a kind (free grant = full access).
    kind_paid = getattr(Subscription, "KIND_PAID", None)
    if "kind" in field_names and kind_paid is not None:
        kwargs["kind"] = kind_paid

    kwargs = {k: v for k, v in kwargs.items() if k in field_names}
    return Subscription.objects.create(**kwargs)


def _redeem_scholarship_award_if_any(*, learner, course, enrollment, subscription):
    """Mark a scholarship award redeemed on successful enrollment, if this
    learner earned one for this course. Best-effort and defensive: the free
    path must keep working even if the scholarship app is absent, mid-deploy,
    or errors — a missed redemption mark is recoverable from the admin
    awards list, but a broken free-enroll flow is not.

    While GlobalSettings.free_trial_enabled is True the award was only ever
    "locked" (informational) and the real ManualUpi/Razorpay providers don't
    apply a discount yet — see scholarship/models.py ScholarshipAward and
    scholarship/services.py get_active_award for the redemption story once
    paid pricing goes live.
    """
    try:
        from scholarship.services import get_active_award, redeem_award
        award = get_active_award(learner, course)
        if award is not None:
            redeem_award(award, enrollment=enrollment, subscription=subscription)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Failed to mark scholarship award redeemed for learner=%s course=%s", learner.id, course.id,
        )


class PaymentConfigView(APIView):
    """What payment mode is live right now. Frontends use this to decide
    whether to show the UPI form, a gateway button, or a one-tap free enroll."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(get_payment_provider().describe())


class FreeEnrollView(APIView):
    """Instant enrollment while the platform is free. Refuses unless the active
    provider auto-activates, so flipping to a paid provider closes this door."""
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request):
        provider = get_payment_provider()
        if not provider.auto_activate:
            return Response(
                {"detail": "This course requires payment.", "provider": provider.name},
                status=status.HTTP_400_BAD_REQUEST,
            )

        course_id = request.data.get("course")
        if not course_id:
            return Response({"course": "This field is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            course = Course.objects.get(pk=course_id)
        except Course.DoesNotExist:
            return Response({"detail": "Course not found."},
                            status=status.HTTP_404_NOT_FOUND)

        learner = get_active_profile(request)
        if learner is None:
            return Response({"detail": "Select a learner profile before enrolling."},
                            status=status.HTTP_400_BAD_REQUEST)
        mismatch = profile_mismatch_response(request, request.data.get("active_profile_id"))
        if mismatch is not None:
            return mismatch

        # Optional: student's own batch choice (Morning/Afternoon/Evening/Night
        # etc — see courses.models.Batch). Validated + locked inside the same
        # transaction as the enrollment write so two students can't both win
        # the last seat in a capped batch.
        batch_id = request.data.get("batch")
        with transaction.atomic():
            batch = None
            if batch_id:
                batch, err = _validate_batch_choice(course, batch_id)
                if err is not None:
                    return err

            # Grant access (idempotent on the unique (learner_profile, course) pair).
            enroll_defaults = {"user": request.user, "status": Enrollment.STATUS_ACTIVE}
            if batch is not None:
                enroll_defaults["batch"] = batch
                enroll_defaults["batch_code"] = batch.code
            enrollment, created = Enrollment.objects.get_or_create(
                learner_profile=learner, course=course, defaults=enroll_defaults
            )
            # Re-calling this endpoint (e.g. the student skipped batch choice
            # the first time and comes back to pick one) sets the batch on an
            # already-existing enrollment — but only while it's still unset.
            # Once a batch is assigned, changing it is an admin action
            # (BatchRosterModal's "Move to"), matching the boundary
            # _move_batch already draws for admin-initiated moves.
            if not created and batch is not None and enrollment.batch_id is None:
                enrollment.batch = batch
                enrollment.batch_code = batch.code
                enrollment.save(update_fields=["batch", "batch_code"])

        # Idempotent: a student can call this endpoint repeatedly (retry,
        # double-click, or a scripted loop) — without this check, every call
        # unconditionally extended expires_at by another `days`, letting a
        # student stack unlimited free access. Only grant/extend once there
        # is no currently-active subscription; a repeat call while one is
        # still active returns it unchanged. (The admin-initiated grant in
        # AdminCreateEnrollmentView is deliberately NOT gated this way — an
        # admin re-submitting the same action each time is an intentional,
        # authorized re-grant, not a self-serve loop.)
        sub = get_active_subscription(user=request.user, course=course, learner_profile=learner)
        if sub is None:
            sub = _create_or_extend_subscription(user=request.user, learner=learner, course=course)
        _redeem_scholarship_award_if_any(learner=learner, course=course, enrollment=enrollment, subscription=sub)

        return Response(
            {
                "detail": "You're enrolled.",
                "course_id": str(course.id),
                "batch": (
                    {"id": str(enrollment.batch_id), "name": enrollment.batch.name}
                    if enrollment.batch_id else None
                ),
                "subscription": {
                    "id": str(sub.id),
                    "status": sub.status,
                    "expires_at": sub.expires_at,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class SelectEnrollmentBatchView(APIView):
    """Let an already-enrolled student choose their own batch, for the case
    the free-enroll/payment flow didn't collect one (enrolled before the
    course had batches, or skipped the picker). Only ever moves batch=None
    to a real batch — never reassigns an already-set one, matching the
    boundary admin's own _move_batch draws for reassignment (an admin
    action from BatchRosterModal, not a student self-service one)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        course_id = request.data.get("course")
        batch_id = request.data.get("batch")
        if not course_id or not batch_id:
            return Response({"detail": "course and batch are required."},
                            status=status.HTTP_400_BAD_REQUEST)

        learner = get_active_profile(request)
        if learner is None:
            return Response({"detail": "Select a learner profile."},
                            status=status.HTTP_400_BAD_REQUEST)
        mismatch = profile_mismatch_response(request, request.data.get("active_profile_id"))
        if mismatch is not None:
            return mismatch

        try:
            course = Course.objects.get(pk=course_id)
        except Course.DoesNotExist:
            return Response({"detail": "Course not found."},
                            status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            enrollment = Enrollment.objects.select_for_update().filter(
                learner_profile=learner, course=course,
                status=Enrollment.STATUS_ACTIVE,
            ).first()
            if enrollment is None:
                return Response(
                    {"detail": "You are not enrolled in this course."},
                    status=status.HTTP_404_NOT_FOUND)
            if enrollment.batch_id is not None:
                return Response(
                    {"detail": "You already have a batch. Contact support to change it."},
                    status=status.HTTP_400_BAD_REQUEST)

            batch, err = _validate_batch_choice(course, batch_id)
            if err is not None:
                return err

            enrollment.batch = batch
            enrollment.batch_code = batch.code
            enrollment.save(update_fields=["batch", "batch_code"])

        return Response({
            "detail": "Batch selected.",
            "batch": {"id": str(batch.id), "name": batch.name},
        })
