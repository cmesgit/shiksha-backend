from rest_framework import serializers
from django.utils import timezone
from .models import Assignment, AssignmentSubmission
from courses.models import Chapter
import os


# ==========================================
# FILE TYPE VALIDATOR
# ==========================================

BLOCKED_EXTENSIONS = [
    ".exe", ".bat", ".cmd", ".sh", ".bash",
    ".php", ".py", ".rb", ".pl", ".cgi",
    ".js", ".vbs", ".ps1", ".msi", ".dll",
    ".com", ".scr", ".jar", ".app",
]


def validate_assignment_file(file):
    """
    Allows all file types except dangerous executables/scripts.
    Suitable for any LMS platform (PC, Android, iPhone).
    Max file size: 100MB
    """
    if file is None:
        return file

    ext = os.path.splitext(file.name)[1].lower()
    if ext in BLOCKED_EXTENSIONS:
        raise serializers.ValidationError(
            f"File type '{ext}' is not allowed for security reasons."
        )

    max_size = 100 * 1024 * 1024
    if file.size > max_size:
        raise serializers.ValidationError(
            "File too large. Maximum allowed size is 100MB."
        )

    return file


# ==========================================
# STUDENT SERIALIZERS
# ==========================================

class AssignmentListSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    subject_name = serializers.CharField(
        source="chapter.subject.name",
        read_only=True
    )
    course_id = serializers.UUIDField(
        source="chapter.subject.course.id",
        read_only=True
    )
    attachment = serializers.FileField(read_only=True)

    class Meta:
        model = Assignment
        fields = (
            "id",
            "title",
            "due_date",
            "status",
            "subject_name",
            "course_id",
            "attachment",
        )

    def get_status(self, obj):
        submission = getattr(obj, "user_submission", None)

        if submission:
            return "SUBMITTED"

        if obj.due_date < timezone.now():
            return "EXPIRED"

        return "PENDING"


class AssignmentDetailSerializer(serializers.ModelSerializer):
    submission_status = serializers.SerializerMethodField()
    submitted_file = serializers.SerializerMethodField()
    submitted_at = serializers.SerializerMethodField()
    subject_name = serializers.CharField(
        source="chapter.subject.name",
        read_only=True
    )
    course_name = serializers.CharField(
        source="chapter.subject.course.title",
        read_only=True
    )

    class Meta:
        model = Assignment
        fields = (
            "id",
            "title",
            "description",
            "attachment",
            "due_date",
            "subject_name",
            "course_name",
            "submission_status",
            "submitted_file",
            "submitted_at",
        )

    def get_submission(self, obj):
        return getattr(obj, "user_submission", None)

    def get_submission_status(self, obj):
        submission = self.get_submission(obj)

        if submission:
            return "SUBMITTED"

        if obj.due_date < timezone.now():
            return "EXPIRED"

        return "PENDING"

    def get_submitted_file(self, obj):
        submission = self.get_submission(obj)
        if submission and submission.submitted_file:
            return submission.submitted_file.url
        return None

    def get_submitted_at(self, obj):
        submission = self.get_submission(obj)
        return submission.submitted_at if submission else None


# ==========================================
# TEACHER SERIALIZERS
# ==========================================

class TeacherAssignmentCreateSerializer(serializers.ModelSerializer):

    chapter_id = serializers.PrimaryKeyRelatedField(
        queryset=Chapter.objects.all(),
        source="chapter",
        write_only=True
    )

    class Meta:
        model = Assignment
        fields = (
            "chapter_id",
            "title",
            "description",
            "due_date",
            "attachment",
        )

    def validate(self, attrs):
        if attrs["due_date"] < timezone.now():
            raise serializers.ValidationError(
                "Due date must be in the future."
            )
        return attrs

    def validate_attachment(self, value):
        return validate_assignment_file(value)

    # ✅ corrected validator name
    def validate_chapter(self, chapter):
        user = self.context["request"].user

        if not chapter.subject.subject_teachers.filter(
            teacher=user
        ).exists():
            raise serializers.ValidationError(
                "You are not assigned to this subject."
            )

        return chapter


class TeacherAssignmentUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Assignment
        fields = (
            "title",
            "description",
            "due_date",
            "attachment",
        )

    def validate_due_date(self, value):
        if value < timezone.now():
            raise serializers.ValidationError(
                "Due date must be in the future."
            )
        return value

    def validate_attachment(self, value):
        return validate_assignment_file(value)


class TeacherAssignmentListSerializer(serializers.ModelSerializer):

    chapter_name = serializers.SerializerMethodField()
    total_submissions = serializers.IntegerField(read_only=True)

    class Meta:
        model = Assignment
        fields = [
            "id",
            "title",
            "chapter_name",
            "due_date",
            "total_submissions",
        ]

    def get_chapter_name(self, obj):
        if obj.chapter:
            return obj.chapter.title
        return None


class TeacherSubmissionListSerializer(serializers.ModelSerializer):
    student_id = serializers.UUIDField(
        source="student.id",
        read_only=True
    )
    student_email = serializers.EmailField(
        source="student.email",
        read_only=True
    )
    student_name = serializers.CharField(
        source="student.profile.full_name",
        read_only=True
    )

    class Meta:
        model = AssignmentSubmission
        fields = (
            "id",
            "student_id",
            "student_email",
            "student_name",
            "submitted_file",
            "submitted_at",
        )
