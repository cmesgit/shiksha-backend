import logging
import os

from rest_framework import serializers
from django.utils import timezone
from django.db import transaction

from accounts.email_utils import send_gmail
from courses.models import Course

from .models import Enrollment, EnrollmentRequest

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
        user = self.context["request"].user
        course = attrs["course"]

        if Enrollment.objects.filter(
            user=user, course=course, status=Enrollment.STATUS_ACTIVE
        ).exists():
            raise serializers.ValidationError("You are already enrolled in this course.")

        if EnrollmentRequest.objects.filter(
            user=user, course=course, status=EnrollmentRequest.STATUS_PENDING
        ).exists():
            raise serializers.ValidationError(
                "You already have a pending request for this course."
            )

        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        return EnrollmentRequest.objects.create(user=user, **validated_data)


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
    course_title = serializers.CharField(source="course.title", read_only=True)
    course_price = serializers.IntegerField(source="course.price", read_only=True)

    class Meta:
        model = EnrollmentRequest
        fields = (
            "id",
            "user_email",
            "user_name",
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
                    user=request_obj.user,
                    course=request_obj.course,
                    defaults={"status": Enrollment.STATUS_ACTIVE},
                )
            else:
                request_obj.status = EnrollmentRequest.STATUS_REJECTED

            request_obj.save()

        _send_enrollment_decision_email(request_obj)

        return request_obj
