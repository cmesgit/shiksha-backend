import logging
import os
from datetime import timedelta

from rest_framework import serializers
from django.utils import timezone
from django.db import transaction

from accounts.email_utils import send_gmail
from courses.models import Course, Batch

from .models import Enrollment, EnrollmentRequest, Subscription
from .services import grant_paid_subscription

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


# Subscription grant moved to enrollments.services.grant_paid_subscription
# (handles trial→paid conversion correctly).


class CourseBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ("id", "title", "price")


class BatchBriefSerializer(serializers.ModelSerializer):
    seats_taken = serializers.IntegerField(read_only=True)
    is_full = serializers.BooleanField(read_only=True)

    class Meta:
        model = Batch
        fields = ("id", "name", "code", "year", "capacity", "seats_taken", "is_full")


class BatchStudentSerializer(serializers.ModelSerializer):
    """One row per enrolled student, for the admin batch-roster view."""
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()
    course_title = serializers.CharField(source="course.title", read_only=True)
    batch = BatchBriefSerializer(read_only=True)

    class Meta:
        model = Enrollment
        fields = (
            "id",
            "user_email",
            "user_name",
            "course_title",
            "batch",
            "status",
            "enrolled_at",
        )

    def get_user_name(self, obj):
        profile = getattr(obj.user, "profile", None)
        if profile:
            full = f"{profile.first_name} {profile.last_name}".strip()
            if full:
                return full
            if getattr(profile, "full_name", ""):
                return profile.full_name
        return obj.user.username or obj.user.email


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

        if EnrollmentRequest.objects.filter(
            user=user, course=course, status=EnrollmentRequest.STATUS_PENDING
        ).exists():
            raise serializers.ValidationError(
                "You already have a pending request for this course."
            )

        # Trial-first policy: a user cannot pay for a course they've never tried.
        # They must use their free trial before submitting a paid enrollment request.
        if not Subscription.objects.filter(user=user, course=course).exists():
            raise serializers.ValidationError(
                "Please start your free trial before enrolling. "
                "You can pay to extend access once your trial begins or ends."
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
    # Optional: assign the student to a batch at approval time.
    batch = serializers.PrimaryKeyRelatedField(
        queryset=Batch.objects.all(),
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        # Cross-field checks that need the target request object.
        request_obj = self.context.get("request_obj")
        action = attrs.get("action")
        batch = attrs.get("batch")

        if action == "approve" and batch is not None and request_obj is not None:
            if batch.course_id != request_obj.course_id:
                raise serializers.ValidationError(
                    {"batch": "This batch does not belong to the requested course."}
                )
            if batch.is_full:
                raise serializers.ValidationError(
                    {"batch": "The selected batch is full."}
                )
        return attrs

    def save(self, *, request_obj, reviewer):
        action = self.validated_data["action"]
        note = self.validated_data.get("admin_note", "")
        batch = self.validated_data.get("batch")

        if request_obj.status != EnrollmentRequest.STATUS_PENDING:
            raise serializers.ValidationError("This request has already been reviewed.")

        with transaction.atomic():
            request_obj.admin_note = note
            request_obj.reviewed_by = reviewer
            request_obj.reviewed_at = timezone.now()

            if action == "approve":
                request_obj.status = EnrollmentRequest.STATUS_APPROVED

                enrollment, created = Enrollment.objects.get_or_create(
                    user=request_obj.user,
                    course=request_obj.course,
                    defaults={
                        "status": Enrollment.STATUS_ACTIVE,
                        "batch": batch,
                    },
                )

                # Re-approval / re-activation path: make sure status is ACTIVE
                # and apply the batch if the admin chose one.
                fields_to_update = []
                if enrollment.status != Enrollment.STATUS_ACTIVE:
                    enrollment.status = Enrollment.STATUS_ACTIVE
                    fields_to_update.append("status")
                if batch is not None and enrollment.batch_id != batch.id:
                    enrollment.batch = batch
                    fields_to_update.append("batch")
                if not created and fields_to_update:
                    enrollment.save(update_fields=fields_to_update)

                grant_paid_subscription(request_obj=request_obj)
            else:
                request_obj.status = EnrollmentRequest.STATUS_REJECTED

            request_obj.save()

        _send_enrollment_decision_email(request_obj)

        return request_obj
