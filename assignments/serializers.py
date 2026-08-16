from rest_framework import serializers
from django.utils import timezone
from .models import Assignment, AssignmentFile, AssignmentSubmission
from courses.models import Chapter, Batch
from courses.services import is_teacher_of
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

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB


def validate_assignment_file(file):
    if file is None:
        return file

    ext = os.path.splitext(file.name)[1].lower()
    if ext in BLOCKED_EXTENSIONS:
        raise serializers.ValidationError(
            f"File type '{ext}' is not allowed for security reasons."
        )

    if file.size > MAX_FILE_SIZE:
        raise serializers.ValidationError(
            "File too large. Maximum allowed size is 100 MB."
        )

    return file


# ==========================================
# ASSIGNMENT FILE SERIALIZER
# ==========================================

class AssignmentFileSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = AssignmentFile
        fields = ("id", "original_filename", "url", "uploaded_at")

    def get_url(self, obj):
        request = self.context.get("request")
        if request and obj.file:
            return request.build_absolute_uri(obj.file.url)
        return obj.file.url if obj.file else None


# ==========================================
# STUDENT SERIALIZERS
# ==========================================

class AssignmentListSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    subject_name = serializers.CharField(
        source="chapter.subject.name",
        read_only=True,
    )
    # The learner's Assignments screen is one flat, subject-filtered list built
    # from this endpoint, so it needs the id (not just the name) to group rows
    # into subject pills and to link each row at
    # /subjects/<subject_id>/assignments/<id>.
    subject_id = serializers.UUIDField(
        source="chapter.subject.id",
        read_only=True,
    )
    course_id = serializers.UUIDField(
        source="chapter.subject.course.id",
        read_only=True,
    )
    # Legacy single attachment kept for backwards compat
    attachment = serializers.FileField(read_only=True)
    marks_obtained = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = (
            "id",
            "title",
            "due_date",
            "status",
            "subject_name",
            "subject_id",
            "course_id",
            "attachment",
            "max_marks",
            "marks_obtained",
        )

    def get_status(self, obj):
        submission = getattr(obj, "user_submission", None)
        if submission:
            return "GRADED" if submission.marks_obtained is not None else "SUBMITTED"
        if obj.due_date < timezone.now():
            return "EXPIRED"
        return "PENDING"

    def get_marks_obtained(self, obj):
        submission = getattr(obj, "user_submission", None)
        return submission.marks_obtained if submission else None


class AssignmentDetailSerializer(serializers.ModelSerializer):
    submission_status = serializers.SerializerMethodField()
    submission_status_label = serializers.SerializerMethodField()
    submitted_file = serializers.SerializerMethodField()
    submitted_at = serializers.SerializerMethodField()
    marks_obtained = serializers.SerializerMethodField()
    feedback = serializers.SerializerMethodField()
    graded_at = serializers.SerializerMethodField()

    subject_name = serializers.CharField(
        source="chapter.subject.name",         read_only=True)
    course_name = serializers.CharField(
        source="chapter.subject.course.title",  read_only=True)
    chapter_name = serializers.CharField(
        source="chapter.title",                 read_only=True)
    teacher_name = serializers.SerializerMethodField()
    assigned_on = serializers.DateTimeField(
        source="created_at",                read_only=True)

    # Exposes all teacher-uploaded files (new multi-file system)
    files = AssignmentFileSerializer(many=True, read_only=True)

    class Meta:
        model = Assignment
        fields = (
            "id",
            "title",
            "description",
            "attachment",   # legacy
            "files",        # new multi-file list
            "due_date",
            "assigned_on",
            "chapter_name",
            "subject_name",
            "course_name",
            "teacher_name",
            "submission_status",
            "submitted_file",
            "submitted_at",
            "submission_status_label",
            "max_marks",
            "marks_obtained",
            "feedback",
            "graded_at",
        )

    def get_submission(self, obj):
        return getattr(obj, "user_submission", None)

    def get_submission_status(self, obj):
        if self.get_submission(obj):
            return "SUBMITTED"
        if obj.due_date < timezone.now():
            return "EXPIRED"
        return "PENDING"

    def get_submitted_file(self, obj):
        request = self.context.get("request")
        submission = self.get_submission(obj)
        if submission and submission.submitted_file:
            return request.build_absolute_uri(submission.submitted_file.url)
        return None

    def get_submitted_at(self, obj):
        submission = self.get_submission(obj)
        return submission.submitted_at if submission else None

    def get_marks_obtained(self, obj):
        submission = self.get_submission(obj)
        return submission.marks_obtained if submission else None

    def get_feedback(self, obj):
        submission = self.get_submission(obj)
        return submission.feedback if submission else ""

    def get_graded_at(self, obj):
        submission = self.get_submission(obj)
        return submission.graded_at if submission else None

    def get_teacher_name(self, obj):
        subject = obj.chapter.subject
        teacher = subject.teaching_assignments.filter(
            batch__isnull=True, is_active=True,
        ).select_related("teacher").first()
        if teacher and teacher.teacher.default_learner_profile():
            return teacher.teacher.default_learner_profile().full_name
        return None

    def get_submission_status_label(self, obj):
        submission = self.get_submission(obj)
        if not submission:
            return None
        return "On time" if submission.submitted_at <= obj.due_date else "Late"


# ==========================================
# TEACHER SERIALIZERS
# ==========================================

class TeacherAssignmentCreateSerializer(serializers.ModelSerializer):
    chapter_id = serializers.PrimaryKeyRelatedField(
        queryset=Chapter.objects.all(),
        source="chapter",
        write_only=True,
    )
    # Due dates are cohort-relative, so a batch is required for new
    # assignments (legacy batch=NULL rows stay valid — write-side only).
    batch_id = serializers.PrimaryKeyRelatedField(
        queryset=Batch.objects.all(),
        source="batch",
        write_only=True,
    )

    # Optional idempotency key from the frontend form session
    idempotency_key = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Assignment
        fields = (
            "chapter_id",
            "batch_id",
            "title",
            "description",
            "due_date",
            "attachment",
            "idempotency_key",
            "max_marks",
        )
        extra_kwargs = {"max_marks": {"required": False}}

    def validate(self, attrs):
        due_date = attrs.get("due_date")
        # localtime: due_date is an aware DateTimeField — comparing raw
        # (UTC) .date() against raw (UTC) today rejects/accepts the wrong
        # calendar day near IST midnight (this check is deliberately
        # calendar-date-based, not an instant comparison, so a due_date set
        # for later TODAY, IST, at an already-past clock time is still
        # allowed).
        if due_date and timezone.localtime(due_date).date() < timezone.localtime(timezone.now()).date():
            raise serializers.ValidationError(
                {"due_date": "Due date must be today or in the future."}
            )

        chapter = attrs.get("chapter")
        batch = attrs.get("batch")
        user = self.context["request"].user

        # Triangle guard: the batch and the chapter's subject share a course.
        if chapter and batch and chapter.subject.course_id != batch.course_id:
            raise serializers.ValidationError(
                {"batch_id": "Batch and chapter belong to different courses."}
            )

        # Authz: assigned to this (batch, subject) — either scoped to the
        # batch, or course-wide (is_teacher_of() covers both).
        if chapter and batch and not is_teacher_of(user, batch, chapter.subject):
            raise serializers.ValidationError(
                {"non_field_errors": ["You are not assigned to this subject."]}
            )
        return attrs

    def validate_attachment(self, value):
        return validate_assignment_file(value)


class TeacherAssignmentUpdateSerializer(serializers.ModelSerializer):
    """
    Supports:
      - Editing title / description / due_date
      - Replacing legacy attachment
      - Adding new files via `new_files` (list of uploaded files)
      - Deleting specific files via `delete_file_ids` (list of UUIDs)
    """

    # Accept multiple new file uploads
    new_files = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
    )

    # Accept a list of AssignmentFile UUIDs to delete
    delete_file_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
    )

    class Meta:
        model = Assignment
        fields = (
            "title",
            "description",
            "due_date",
            "attachment",
            "new_files",
            "delete_file_ids",
            "max_marks",
        )
        extra_kwargs = {"max_marks": {"required": False}}

    def validate_due_date(self, value):
        if value and value < timezone.now():
            raise serializers.ValidationError(
                "Due date must be in the future.")
        return value

    def validate_attachment(self, value):
        return validate_assignment_file(value)

    def validate_new_files(self, files):
        return [validate_assignment_file(f) for f in files]

    def update(self, instance, validated_data):
        new_files = validated_data.pop("new_files", [])
        delete_ids = validated_data.pop("delete_file_ids", [])

        # Delete requested files — only those belonging to this assignment
        if delete_ids:
            instance.files.filter(id__in=delete_ids).delete()

        # Persist standard field changes
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Attach new files
        for f in new_files:
            AssignmentFile.objects.create(
                assignment=instance,
                file=f,
                original_filename=os.path.basename(f.name),
            )

        return instance


class TeacherAssignmentListSerializer(serializers.ModelSerializer):
    chapter_name = serializers.SerializerMethodField()
    total_submissions = serializers.IntegerField(read_only=True)
    files = AssignmentFileSerializer(many=True, read_only=True)
    # The teacher's Assignments screen is one flat list across every subject
    # they teach, so a row has to name its subject — for the pill filter and to
    # target per-subject actions (edit / submissions) at the right class.
    # Method fields because `chapter` is nullable; a dotted source would raise.
    subject_id = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()
    # The design filters the faculty Assignments list by BATCH — its teacher
    # chips are ["All", "10-A", "10-B", "9-A"], matched against the row's meta
    # line (Academy Dashboard.dc.html, AP_CHIPS at line 3276). Assignment.batch
    # already exists on the model and simply wasn't serialised. NULL means
    # course-wide (legacy rows), which the UI shows as "All batches".
    batch_id = serializers.UUIDField(source="batch.id", read_only=True, allow_null=True)
    batch_name = serializers.CharField(source="batch.name", read_only=True, allow_null=True)

    class Meta:
        model = Assignment
        fields = (
            "id",
            "title",
            "chapter_name",
            "subject_id",
            "subject_name",
            "batch_id",
            "batch_name",
            "due_date",
            "total_submissions",
            "attachment",   # legacy
            "files",        # new multi-file
            "max_marks",
        )

    def get_chapter_name(self, obj):
        return obj.chapter.title if obj.chapter else None

    def _subject(self, obj):
        return getattr(obj.chapter, "subject", None) if obj.chapter else None

    def get_subject_id(self, obj):
        s = self._subject(obj)
        return str(s.id) if s else None

    def get_subject_name(self, obj):
        s = self._subject(obj)
        return s.name if s else None


class TeacherSubmissionListSerializer(serializers.ModelSerializer):
    student_id = serializers.UUIDField(
        source="student.id",               read_only=True)
    student_email = serializers.EmailField(
        source="student.email",            read_only=True)
    student_name = serializers.SerializerMethodField()
    learner_profile_id = serializers.UUIDField(
        source="learner_profile.id", read_only=True, default=None)
    submission_status = serializers.CharField(read_only=True)
    max_marks = serializers.IntegerField(source="assignment.max_marks", read_only=True)

    class Meta:
        model = AssignmentSubmission
        fields = (
            "id",
            "student_id",
            "student_email",
            "student_name",
            "learner_profile_id",
            "submitted_file",
            "submitted_at",
            "submission_status",
            "marks_obtained",
            "max_marks",
            "feedback",
            "graded_at",
        )

    def get_student_name(self, obj):
        # The learner who actually submitted — on a shared family account the
        # account username can't distinguish between children.
        lp = obj.learner_profile or obj.student.default_learner_profile()
        if lp:
            name = (lp.full_name or "").strip() or (lp.display_name or "").strip()
            if name:
                return name
        return obj.student.username or obj.student.email
