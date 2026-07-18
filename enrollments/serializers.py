import logging
import os
from datetime import timedelta

from rest_framework import serializers
from django.utils import timezone
from django.db import transaction

from accounts.email_utils import send_gmail
from accounts.auth_flow import get_active_profile
from courses.models import Course

from .models import Enrollment, EnrollmentRequest, Subscription

logger = logging.getLogger(__name__)


def _send_enrollment_decision_email(request_obj):
    """Notify the student that their enrollment request was approved or rejected.

    Swallows errors so a mail outage cannot roll back the admin's decision.
    """
    user = request_obj.user
    course_title = request_obj.course.title
    status_value = request_obj.status
    student_app_url = os.getenv("STUDENT_APP_URL", "https://app.shikshacom.com")

    if status_value == EnrollmentRequest.STATUS_APPROVED:
        subject = f"Enrollment approved — {course_title}"
        text = (
            f"Hi,\n\n"
            f"Your enrollment for \"{course_title}\" has been approved. "
            f"You can now access your course on the student dashboard.\n\n"
            f"{student_app_url}\n\n"
            f"— Shiksha Team"
        )
        html = f"""
        <h2>Enrollment approved</h2>
        <p>Your enrollment for <strong>{course_title}</strong> has been approved.</p>
        <p>You can now access your course on the student dashboard.</p>
        <a href="{student_app_url}" style="padding:10px 15px;background:#2563eb;color:white;text-decoration:none;border-radius:5px;">
            Go to Dashboard
        </a>
        """
    elif status_value == EnrollmentRequest.STATUS_REJECTED:
        subject = f"Enrollment request declined — {course_title}"
        note = request_obj.admin_note.strip() if request_obj.admin_note else ""
        note_line = f"Reason from our team:\n{note}\n\n" if note else ""
        note_html = (
            f"<p><strong>Reason from our team:</strong><br>{note}</p>" if note else ""
        )
        text = (
            f"Hi,\n\n"
            f"Unfortunately your enrollment request for \"{course_title}\" was not approved.\n\n"
            f"{note_line}"
            f"If you believe this is a mistake, please contact support.\n\n"
            f"— Shiksha Team"
        )
        html = f"""
        <h2>Enrollment request declined</h2>
        <p>Unfortunately your enrollment request for <strong>{course_title}</strong> was not approved.</p>
        {note_html}
        <p>If you believe this is a mistake, please contact support.</p>
        """
    else:
        return

    try:
        send_gmail(to=user.email, subject=subject, message_text=text, html=html)
    except Exception as e:
        logger.error(
            "Failed to send enrollment %s email to %s: %s",
            status_value, user.email, e,
        )


def _grant_subscription(request_obj):
    """Create a new Subscription for an approved request, or extend the active one.

    If the user already has an active, non-expired subscription for this course,
    extend its expires_at by the course's subscription_duration_days. Otherwise
    start a fresh subscription from now.
    """
    course = request_obj.course
    days = course.subscription_duration_days or 30
    now = timezone.now()

    learner = request_obj.learner_profile

    active = (
        Subscription.objects
        .select_for_update()
        .filter(
            learner_profile=learner,
            course=course,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=now,
        )
        .order_by("-expires_at")
        .first()
    )

    if active:
        active.expires_at = active.expires_at + timedelta(days=days)
        active.save(update_fields=["expires_at", "updated_at"])
        return active

    return Subscription.objects.create(
        user=request_obj.user,
        learner_profile=learner,
        course=course,
        starts_at=now,
        expires_at=now + timedelta(days=days),
        status=Subscription.STATUS_ACTIVE,
        source_request=request_obj,
    )


class CourseBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ("id", "title", "price")


# -------- Student-facing --------

class EnrollmentRequestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "course",
            "amount_paid",
            "payment_method",
            "utr_number",
            "payment_date",
            "receipt",
        )
        read_only_fields = ("id",)

    def validate(self, attrs):
        request = self.context["request"]
        learner = get_active_profile(request)
        if learner is None:
            raise serializers.ValidationError(
                "Select a learner profile before enrolling."
            )
        course = attrs["course"]

        if EnrollmentRequest.objects.filter(
            learner_profile=learner, course=course,
            status=EnrollmentRequest.STATUS_PENDING,
        ).exists():
            raise serializers.ValidationError(
                "This learner already has a pending request for this course."
            )

        attrs["_learner"] = learner
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        learner = validated_data.pop("_learner")
        return EnrollmentRequest.objects.create(
            user=request.user,
            learner_profile=learner,
            **validated_data,
        )


class MyEnrollmentRequestSerializer(serializers.ModelSerializer):
    course = CourseBriefSerializer(read_only=True)
    receipt = serializers.ImageField(read_only=True)
    learner_name = serializers.SerializerMethodField()
    learner_profile_id = serializers.SerializerMethodField()

    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "course",
            "amount_paid",
            "payment_method",
            "utr_number",
            "payment_date",
            "receipt",
            "status",
            "admin_note",
            "submitted_at",
            "reviewed_at",
            "learner_name",
            "learner_profile_id",
        )

    def get_learner_name(self, obj):
        lp = obj.learner_profile
        if lp is None:
            return ""
        return (lp.full_name or "").strip() or lp.display_name

    def get_learner_profile_id(self, obj):
        return str(obj.learner_profile_id) if obj.learner_profile_id else None


# -------- Admin-facing --------

class AdminEnrollmentRequestListSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()
    learner_name = serializers.SerializerMethodField()
    course_title = serializers.CharField(source="course.title", read_only=True)
    course_price = serializers.IntegerField(source="course.price", read_only=True)
    course_id = serializers.UUIDField(source="course.id", read_only=True)

    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "user_email",
            "user_name",
            "learner_name",
            "course_title",
            "course_price",
            "course_id",
            "amount_paid",
            "payment_method",
            "utr_number",
            "payment_date",
            "receipt",
            "status",
            "admin_note",
            "submitted_at",
            "reviewed_at",
        )

    def get_user_name(self, obj):
        # The legacy one-to-one Profile model was removed; the account holder's
        # name now lives on the User (AbstractUser) or on their learner profile.
        full = (obj.user.get_full_name() or "").strip()
        if full:
            return full
        lp = obj.learner_profile
        if lp:
            name = f"{lp.first_name} {lp.last_name}".strip() or lp.display_name
            if name:
                return name
        return obj.user.username or obj.user.email

    def get_learner_name(self, obj):
        lp = obj.learner_profile
        if not lp:
            return None
        name = f"{lp.first_name} {lp.last_name}".strip()
        return name or lp.display_name


class AdminActionSerializer(serializers.Serializer):
    ACTION_CHOICES = [("approve", "approve"), ("reject", "reject")]

    action = serializers.ChoiceField(choices=ACTION_CHOICES)
    admin_note = serializers.CharField(required=False, allow_blank=True)
    # Optional: which batch to place the student in on approval.
    batch = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        from courses.models import Batch  # local import avoids app-load cycle

        request_obj = self.context.get("request_obj")
        batch_id = attrs.get("batch")

        if attrs["action"] == "approve" and batch_id:
            try:
                batch = Batch.objects.get(pk=batch_id)
            except Batch.DoesNotExist:
                raise serializers.ValidationError({"batch": "Batch not found."})
            if request_obj and batch.course_id != request_obj.course_id:
                raise serializers.ValidationError(
                    {"batch": "Batch does not belong to this request's course."}
                )
            if not batch.is_active:
                raise serializers.ValidationError({"batch": "Batch is not active."})
            attrs["_batch"] = batch
        return attrs

    def save(self, *, request_obj, reviewer):
        action = self.validated_data["action"]
        note = self.validated_data.get("admin_note", "")
        batch = self.validated_data.get("_batch")

        if request_obj.status != EnrollmentRequest.STATUS_PENDING:
            raise serializers.ValidationError("This request has already been reviewed.")

        with transaction.atomic():
            request_obj.admin_note = note
            request_obj.reviewed_by = reviewer
            request_obj.reviewed_at = timezone.now()

            if action == "approve":
                request_obj.status = EnrollmentRequest.STATUS_APPROVED

                # Capacity check (row-locked) — only when a capped batch is chosen.
                if batch is not None:
                    batch = type(batch).objects.select_for_update().get(pk=batch.pk)
                    if batch.capacity is not None:
                        taken = Enrollment.objects.filter(
                            batch=batch, status=Enrollment.STATUS_ACTIVE
                        ).count()
                        # Re-approving someone already in this batch shouldn't be
                        # blocked; that case is covered because the existing
                        # enrollment already counts toward `taken`.
                        already_here = Enrollment.objects.filter(
                            learner_profile=request_obj.learner_profile,
                            course=request_obj.course,
                            batch=batch,
                        ).exists()
                        if not already_here and taken >= batch.capacity:
                            raise serializers.ValidationError(
                                {"batch": f"Batch '{batch.code}' is full "
                                          f"({taken}/{batch.capacity})."}
                            )

                enrollment, _created = Enrollment.objects.get_or_create(
                    learner_profile=request_obj.learner_profile,
                    course=request_obj.course,
                    defaults={
                        "user": request_obj.user,
                        "status": Enrollment.STATUS_ACTIVE,
                    },
                )
                # (Re)assign batch + keep the legacy code in sync.
                update_fields = []
                if enrollment.status != Enrollment.STATUS_ACTIVE:
                    enrollment.status = Enrollment.STATUS_ACTIVE
                    update_fields.append("status")
                if batch is not None:
                    enrollment.batch = batch
                    enrollment.batch_code = batch.code
                    update_fields += ["batch", "batch_code"]
                if update_fields:
                    enrollment.save(update_fields=update_fields)

                _grant_subscription(request_obj)
            else:
                request_obj.status = EnrollmentRequest.STATUS_REJECTED

            request_obj.save()

        _send_enrollment_decision_email(request_obj)

        return request_obj


# -------- Batch roster (admin) --------

class BatchStudentSerializer(serializers.ModelSerializer):
    """Serializes an Enrollment for the admin batch roster view.

    The student's name comes from the linked learner profile (the legacy
    one-to-one Profile model was removed in accounts migration 0011).
    """
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()
    course_title = serializers.CharField(source="course.title", read_only=True)
    batch_code = serializers.CharField(read_only=True, default=None)
    batch_id = serializers.UUIDField(source="batch.id", read_only=True, default=None)
    batch_name = serializers.CharField(source="batch.name", read_only=True, default=None)

    class Meta:
        model = Enrollment
        fields = (
            "id",
            "user_email",
            "user_name",
            "course_title",
            "batch_id",
            "batch_name",
            "batch_code",
            "status",
            "enrolled_at",
        )

    def get_user_name(self, obj):
        lp = obj.learner_profile
        if lp:
            name = f"{lp.first_name} {lp.last_name}".strip() or lp.display_name
            if name:
                return name
        full = (obj.user.get_full_name() or "").strip()
        return full or obj.user.username or obj.user.email
