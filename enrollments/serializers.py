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
        )


# -------- Admin-facing --------

class AdminEnrollmentRequestListSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()
    learner_name = serializers.SerializerMethodField()
    course_title = serializers.CharField(source="course.title", read_only=True)
    course_price = serializers.IntegerField(source="course.price", read_only=True)

    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "user_email",
            "user_name",
            "learner_name",
            "course_title",
            "course_price",
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
        profile = getattr(obj.user, "profile", None)
        if profile:
            full = f"{profile.first_name} {profile.last_name}".strip()
            if full:
                return full
            if profile.full_name:
                return profile.full_name
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

    def save(self, *, request_obj, reviewer):
        action = self.validated_data["action"]
        note = self.validated_data.get("admin_note", "")

        if request_obj.status != EnrollmentRequest.STATUS_PENDING:
            raise serializers.ValidationError("This request has already been reviewed.")

        with transaction.atomic():
            request_obj.admin_note = note
            request_obj.reviewed_by = reviewer
            request_obj.reviewed_at = timezone.now()

            if action == "approve":
                request_obj.status = EnrollmentRequest.STATUS_APPROVED
                Enrollment.objects.get_or_create(
                    learner_profile=request_obj.learner_profile,
                    course=request_obj.course,
                    defaults={
                        "user": request_obj.user,
                        "status": Enrollment.STATUS_ACTIVE,
                    },
                )
                _grant_subscription(request_obj)
            else:
                request_obj.status = EnrollmentRequest.STATUS_REJECTED

            request_obj.save()

        _send_enrollment_decision_email(request_obj)

        return request_obj
