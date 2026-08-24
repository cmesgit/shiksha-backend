from rest_framework import serializers
from django.utils import timezone
from .models import Assignment, AssignmentFile, AssignmentSubmission
from courses.board_display import board_name_via
from courses.models import Chapter, Batch, Subject
from courses.services import is_teacher_of, resolve_or_create_chapter
from courses.chapter_tags import ChapterTagWriteMixin, serialize_tags
import os


# ==========================================
# FILE TYPE VALIDATOR
# ==========================================

BLOCKED_EXTENSIONS = [
    ".exe", ".bat", ".cmd", ".sh", ".bash",
    ".php", ".py", ".rb", ".pl", ".cgi",
    ".js", ".vbs", ".ps1", ".msi", ".dll",
    ".com", ".scr", ".jar", ".app",
    # ── Active content served back over HTTP ──────────────────────────────
    # Everything above is about a file being *executed on a machine*. These
    # are about a file being *rendered by a browser*: MEDIA_URL serves
    # uploads with a guessed Content-Type, so an uploaded .html/.svg comes
    # back as text/html or image/svg+xml and its inline <script> runs on the
    # media origin. Cookies here are set on the parent domain, so that is a
    # real session-stealing XSS and not a cosmetic one. Blocked on the
    # teacher path too — a teacher's attachment is served to every student.
    ".html", ".htm", ".xhtml", ".shtml", ".mhtml", ".mht",
    ".svg", ".svgz", ".xml", ".xsl", ".xslt", ".hta", ".swf",
]

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

# What a student may hand in. An ALLOWLIST rather than the teacher path's
# blocklist, deliberately: submissions arrive from the least-trusted party on
# the platform, land in a directory the teacher then opens in their own
# browser, and the UI only ever offers three formats anyway
# (AssignmentDetail.jsx's `accept=".pdf,.doc,.docx"`). Images are included
# because photographing handwritten work is the normal path on a phone.
ALLOWED_SUBMISSION_EXTENSIONS = [
    ".pdf",
    ".doc", ".docx", ".odt", ".rtf", ".txt",
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif",
    ".zip",
]

# Submissions are a student's own worksheet, not a lecture recording. The
# 100 MB teacher cap exists for video; nothing a learner hands in needs it,
# and every one of these uploads is stored forever on a 4 GB box.
MAX_SUBMISSION_SIZE = 25 * 1024 * 1024  # 25 MB


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


def validate_submission_file(file):
    """Gate for STUDENT uploads (`AssignmentSubmission.submitted_file`).

    Until this existed, SubmitAssignmentView wrote `request.FILES["file"]`
    straight onto the model with no check of any kind: the extension blocklist
    and the size cap above were only ever wired to the teacher-facing
    serializers, and the model field carries no validators. A student could
    store `payload.html` and the teacher's own "Review" click executed it.
    The frontend check is advisory only — it passes when EITHER the MIME type
    or the extension matches, and it runs on the client regardless.
    """
    if file is None:
        raise serializers.ValidationError("File required.")

    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_SUBMISSION_EXTENSIONS:
        raise serializers.ValidationError(
            "Only PDF, Word, text, image or ZIP files can be submitted "
            f"(got '{ext or 'no extension'}')."
        )

    if file.size > MAX_SUBMISSION_SIZE:
        raise serializers.ValidationError(
            "File too large. Maximum allowed size is 25 MB."
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
        source="subject.name",
        read_only=True,
    )
    # The learner's Assignments screen is one flat, subject-filtered list built
    # from this endpoint, so it needs the id (not just the name) to group rows
    # into subject pills and to link each row at
    # /subjects/<subject_id>/assignments/<id>.
    subject_id = serializers.UUIDField(
        source="subject.id",
        read_only=True,
    )
    course_id = serializers.UUIDField(
        source="subject.course.id",
        read_only=True,
    )
    board_name = serializers.SerializerMethodField()
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
            "board_name",
            "attachment",
            "max_marks",
            "marks_obtained",
        )

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

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
        source="subject.name",             read_only=True)
    course_name = serializers.CharField(
        source="subject.course.title",     read_only=True)
    board_name = serializers.SerializerMethodField()
    # Method field, not source="chapter.title": `chapter` is optional now, and
    # a dotted source raises rather than yielding null when a hop is None.
    chapter_name = serializers.SerializerMethodField()
    # Students see every chapter this covers, plus the teacher's own note.
    chapter_tags = serializers.SerializerMethodField()
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
            "chapter_tags",
            "chapter_note",
            "no_specific_chapter",
            "subject_name",
            "course_name",
            "board_name",
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

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

    def get_chapter_name(self, obj):
        return obj.chapter.title if obj.chapter_id else None

    def get_chapter_tags(self, obj):
        return serialize_tags(obj)

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
        subject = obj.subject
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

class TeacherAssignmentCreateSerializer(ChapterTagWriteMixin,
                                        serializers.ModelSerializer):
    # ── Chapter placement, three accepted shapes ──────────────────────────
    # 1. `chapter_id`      — legacy single value. The live Edit/Create screens
    #                        send this today.
    # 2. `custom_chapter`  — legacy free-text single value, shipped 2026-08-24
    #                        and in daily use. Mints or reuses a real Chapter.
    # 3. `chapter_tags`    — the new multi-value payload, plus `chapter_note`
    #                        and `no_specific_chapter`.
    # All three coexist deliberately: (1) and (2) are what production clients
    # send right now and must not break. Whichever path runs, the `chapter` FK
    # is left populated when a single chapter resolves, so authorization and
    # every legacy read path keep working.
    #
    # `chapter_tags` and `save_chapters_to_course` are declared by the mixin,
    # below, since they are not model fields.
    chapter_id = serializers.PrimaryKeyRelatedField(
        queryset=Chapter.objects.all(),
        source="chapter",
        write_only=True,
        required=False,
    )
    chapter_tags = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True,
    )
    save_chapters_to_course = serializers.BooleanField(
        required=False, write_only=True,
    )
    # Free-text chapter name. Not a model field — resolved (and popped) in
    # validate() via resolve_or_create_chapter(), which reuses an existing
    # chapter of the same name (case-insensitive) rather than duplicating it.
    custom_chapter = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
    )
    # Only needed alongside custom_chapter — without an existing chapter there
    # is no other way to know which subject the new chapter belongs to.
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), write_only=True, required=False,
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
            "custom_chapter",
            "chapter_tags",
            "save_chapters_to_course",
            "chapter_note",
            "no_specific_chapter",
            "subject_id",
            "batch_id",
            "title",
            "description",
            "due_date",
            "attachment",
            "idempotency_key",
            "max_marks",
            # Optional; the model defaults to True so an existing client that
            # never sends it keeps publishing immediately, exactly as before.
            # Send false to save a draft.
            "is_published",
        )
        extra_kwargs = {
            "max_marks": {"required": False},
            "is_published": {"required": False},
            "chapter_note": {"required": False},
            "no_specific_chapter": {"required": False},
        }

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

        batch = attrs.get("batch")
        user = self.context["request"].user

        chapter = attrs.get("chapter")
        custom_chapter = (attrs.pop("custom_chapter", "") or "").strip()
        subject = attrs.pop("subject_id", None)

        # ── 1. Establish the SUBJECT first ────────────────────────────────
        # Subject is the authorization anchor and the model's NOT NULL column,
        # so it is resolved before anything else and every check below reads
        # it. A chapter, if one was sent, implies it; otherwise the caller must
        # name it. Chapter itself is now OPTIONAL — zero chapters is a valid
        # assignment — which is exactly why authorization can no longer be
        # derived from it.
        if chapter is not None:
            subject = chapter.subject
        if subject is None:
            raise serializers.ValidationError(
                {"subject_id": "Pick a subject for this assignment."}
            )
        attrs["subject"] = subject

        # ── 2. Triangle guard: batch and subject share a course ───────────
        if batch and subject.course_id != batch.course_id:
            raise serializers.ValidationError(
                {"batch_id": "Batch and subject belong to different courses."}
            )

        # ── 3. Staffing guard, BEFORE anything is created ─────────────────
        # Runs on the subject, so it applies identically whether the caller
        # sent a chapter, a custom chapter name, chapter tags, or nothing at
        # all. Ordering matters: an unauthorized request must not leave a
        # stray Chapter row behind under a subject this teacher has no claim
        # to, so no chapter is minted until this has passed.
        if batch and not is_teacher_of(user, batch, subject):
            # Names the BATCH, not just the subject. The old wording ("You are
            # not assigned to this subject") was false and unactionable for the
            # case that actually produces it: a teacher who does teach the
            # subject, but only in another batch. They read it as a bug.
            raise serializers.ValidationError(
                {"non_field_errors": [
                    f"You are not assigned to teach {subject.name} "
                    f"in {batch.name}. Pick a batch you teach."
                ]}
            )

        # ── 4. Legacy single-value shim ───────────────────────────────────
        # `custom_chapter` is the key the live Create-assignment screen sends
        # today; it must keep minting (or case-insensitively reusing) a real
        # Chapter exactly as it does now.
        if chapter is None and custom_chapter:
            chapter = resolve_or_create_chapter(
                subject, custom_title=custom_chapter, created_by=user,
            )
            attrs["chapter"] = chapter

        # ── 5. New chapter-tag payload ────────────────────────────────────
        # Held on the serializer and applied in create(), because tags need
        # the saved row's pk. Validated here so a contradictory request 400s
        # before anything is written.
        self._tag_input = self.pop_chapter_tag_input(attrs)
        self._tag_subject = subject
        return attrs

    def create(self, validated_data):
        instance = super().create(validated_data)
        tags, save_to_course, present = getattr(
            self, "_tag_input", ([], False, False)
        )
        return self.apply_chapter_tags(
            instance, self._tag_subject, tags, save_to_course, present,
        )

    def validate_attachment(self, value):
        return validate_assignment_file(value)


class TeacherAssignmentUpdateSerializer(ChapterTagWriteMixin,
                                        serializers.ModelSerializer):
    """
    Supports:
      - Editing title / description / due_date
      - Replacing legacy attachment
      - Adding new files via `new_files` (list of uploaded files)
      - Deleting specific files via `delete_file_ids` (list of UUIDs)
      - Re-tagging chapters via `chapter_tags` / `chapter_note` /
        `no_specific_chapter`, or the legacy single-value `chapter_id`
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

    # The edit form has always POSTed chapter_id, and this serializer has
    # always dropped it on the floor — DRF silently discards unknown keys, so
    # the teacher got "Assignment updated successfully" and the chapter was
    # unchanged. Accepting it here is the half that makes the control real;
    # the list serializer returning `chapter_id` (below) is the half that
    # stops the select starting blank and forcing a pick in the first place.
    chapter_id = serializers.PrimaryKeyRelatedField(
        queryset=Chapter.objects.all(),
        source="chapter",
        write_only=True,
        required=False,
    )
    chapter_tags = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True,
    )
    save_chapters_to_course = serializers.BooleanField(
        required=False, write_only=True,
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
            "chapter_id",
            "chapter_tags",
            "save_chapters_to_course",
            "chapter_note",
            "no_specific_chapter",
            "max_marks",
            # PATCH is_published=true to publish a draft — that transition is
            # what fires the class notification (activity/signals.py's
            # assignment_created gates on the False→True edge, so publishing
            # notifies exactly once and re-saving a live assignment is silent).
            "is_published",
        )
        extra_kwargs = {
            "max_marks": {"required": False},
            "is_published": {"required": False},
        }

    def validate_due_date(self, value):
        if value and value < timezone.now():
            raise serializers.ValidationError(
                "Due date must be in the future.")
        return value

    def validate_attachment(self, value):
        return validate_assignment_file(value)

    def validate_new_files(self, files):
        return [validate_assignment_file(f) for f in files]

    def validate(self, attrs):
        # A chapter move must stay inside the same SUBJECT. The view's
        # ownership check runs against the assignment as it is on disk, so
        # allowing a cross-subject move would let a teacher relocate an
        # assignment into a subject they don't teach — passing the check on
        # the way in and escaping it forever after.
        chapter = attrs.get("chapter")
        if chapter is not None and self.instance is not None:
            if chapter.subject_id != self.instance.subject_id:
                raise serializers.ValidationError(
                    {"chapter_id": "Pick a chapter from this assignment's own subject."}
                )

        # Tags are confined to the assignment's OWN subject for the same
        # reason: resolve_tags() rejects a chapter_id outside it, and a
        # newly-created custom chapter is created under it, so re-tagging can
        # never walk an assignment into a subject the teacher doesn't teach.
        self._tag_input = self.pop_chapter_tag_input(attrs)
        return attrs

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

        tags, save_to_course, present = getattr(
            self, "_tag_input", ([], False, False)
        )
        return self.apply_chapter_tags(
            instance, instance.subject, tags, save_to_course, present,
        )


class TeacherAssignmentListSerializer(serializers.ModelSerializer):
    chapter_name = serializers.SerializerMethodField()
    # The full multi-chapter placement. `chapter_name`/`chapter_id` above
    # remain the single-value view of it for the current UI.
    chapter_tags = serializers.SerializerMethodField()
    # chapter_id: the Edit form's chapter <select> is seeded from this row, so
    # without it the select opened blank and forced the teacher to re-pick a
    # chapter they hadn't asked to change.
    chapter_id = serializers.UUIDField(
        source="chapter.id", read_only=True, allow_null=True)
    total_submissions = serializers.IntegerField(read_only=True)
    files = AssignmentFileSerializer(many=True, read_only=True)
    # The teacher's Assignments screen is one flat list across every subject
    # they teach, so a row has to name its subject — for the pill filter and to
    # target per-subject actions (edit / submissions) at the right class.
    # Method fields for symmetry with the rest of this serializer; they now read
    # the assignment's own non-null `subject` rather than walking the optional
    # chapter, so a chapter-less assignment still reports its subject (and so
    # still appears under the right pill instead of an "unknown subject" row).
    subject_id = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()
    # The design filters the faculty Assignments list by BATCH — its teacher
    # chips are ["All", "10-A", "10-B", "9-A"], matched against the row's meta
    # line (Academy Dashboard.dc.html, AP_CHIPS at line 3276). Assignment.batch
    # already exists on the model and simply wasn't serialised. NULL means
    # course-wide (legacy rows), which the UI shows as "All batches".
    batch_id = serializers.UUIDField(source="batch.id", read_only=True, allow_null=True)
    batch_name = serializers.CharField(source="batch.name", read_only=True, allow_null=True)
    # A teacher who teaches the same subject under two boards saw two identical
    # rows; batch_name is NULL on course-wide assignments so it can't stand in.
    course_title = serializers.SerializerMethodField()
    board_name = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = (
            "id",
            "title",
            # description: the ONLY payload the teacher Edit form is seeded
            # from is a row of this serializer. Without this field the form
            # initialised description to "", its own validation then refused
            # to submit while empty, and a teacher opening Edit to fix a typo
            # in the title had to retype the entire brief — silently
            # overwriting a long one with whatever they could remember.
            "description",
            "chapter_name",
            "chapter_id",
            "chapter_tags",
            "chapter_note",
            "no_specific_chapter",
            "subject_id",
            "subject_name",
            "course_title",
            "board_name",
            "batch_id",
            "batch_name",
            "due_date",
            "total_submissions",
            "attachment",   # legacy
            "files",        # new multi-file
            "max_marks",
            "is_published",   # false = draft: students can't see it yet
        )

    def get_chapter_name(self, obj):
        return obj.chapter.title if obj.chapter else None

    def get_chapter_tags(self, obj):
        return serialize_tags(obj)

    def _subject(self, obj):
        return obj.subject

    def get_course_title(self, obj):
        s = self._subject(obj)
        course = getattr(s, "course", None) if s else None
        return course.title if course else None

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

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
