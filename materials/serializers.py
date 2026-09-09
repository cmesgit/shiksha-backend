from rest_framework import serializers

from courses.board_display import board_name_via
from courses.chapter_tags import serialize_tags
from courses.models import Batch, Chapter

from .models import StudyMaterial, MaterialFile


class MaterialFileSerializer(serializers.ModelSerializer):

    file_name = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    file_size = serializers.SerializerMethodField()

    class Meta:
        model = MaterialFile
        fields = ["id", "file_url", "file_name", "file_size"]

    def get_file_name(self, obj):
        try:
            return obj.filename()
        except (ValueError, AttributeError):
            return None

    def get_file_url(self, obj):
        if not obj.file:
            return None
        try:
            url = obj.file.url
        except ValueError:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def get_file_size(self, obj):
        if not obj.file:
            return None
        try:
            size = obj.file.size
        except (FileNotFoundError, OSError):
            return None
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{round(size / 1024, 1)} KB"
        return f"{round(size / (1024 * 1024), 1)} MB"


class StudyMaterialSerializer(serializers.ModelSerializer):

    files = serializers.SerializerMethodField()
    chapter_title = serializers.SerializerMethodField()
    # Full multi-chapter placement; chapter_title stays the single-value
    # view of it for the current UI.
    chapter_tags = serializers.SerializerMethodField()
    # The learner's Study Material screen is one flat, subject-filtered list, so
    # a row has to say which subject it belongs to. These read the material's
    # own non-null `subject` rather than walking the now-optional `chapter`, so a
    # chapter-less material still reports its subject instead of dropping out of
    # its pill. Callers listing across subjects should
    # select_related("subject__course__board", "chapter") so this doesn't cost a
    # query per row.
    subject_id = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()
    course_title = serializers.SerializerMethodField()
    board_name = serializers.SerializerMethodField()
    # NULL = course-wide (the model's own default — see materials/models.py).
    # Method field, not a dotted source, since `batch` is nullable.
    batch_name = serializers.SerializerMethodField()
    # NULL = course-wide. The name alone can't distinguish "course-wide" from
    # "a batch that happens to be called nothing", and a client filtering by
    # batch needs the id, not the label.
    batch_id = serializers.SerializerMethodField()
    # chapter_title alone cannot seed an edit form's chapter <select> — it has
    # no value to match against the option list. Both this and batch_id read
    # LOCAL columns (obj.chapter_id / obj.batch_id), so neither costs a query.
    chapter_id = serializers.SerializerMethodField()

    # DELIBERATELY NO uploaded_by_id / uploaded_by_name.
    #
    # They were added here first, to let a teacher tell their own work from a
    # colleague's, and both had to come straight back out:
    #
    #  · N+1. get_uploaded_by_name dereferences obj.uploaded_by, and none of
    #    the SEVEN querysets feeding this serializer select_related it — four
    #    of them are student-facing (StudentSubjectMaterials,
    #    StudentCourseMaterials, ChapterMaterials, StudyMaterialDetail). A
    #    course with 171 materials went to one extra query per row on a
    #    learner hot path, for a teacher-only feature.
    #  · Exposure. This serializer is shared with those student endpoints, so
    #    uploaded_by_id published the uploading teacher's internal auth-user
    #    UUID to every enrolled student.
    #
    # The hub that actually needs owner identity does not use this serializer:
    # dashboard/teacher_resources.py builds its own rows, select_relates the
    # owner, and resolves is_mine server-side. If a per-material owner is ever
    # wanted HERE, it needs a teacher-context branch and the select_related on
    # all seven querysets — not a bare field on the shared shape.

    class Meta:
        model = StudyMaterial
        fields = [
            "id",
            "title",
            "description",
            "created_at",
            "updated_at",
            "chapter_title",
            "chapter_id",
            "chapter_tags",
            "chapter_note",
            "no_specific_chapter",
            "subject_id",
            "subject_name",
            "course_title",
            "board_name",
            "batch_name",
            "batch_id",
            "files",
        ]

    def get_files(self, obj):
        request = self.context.get("request")
        return MaterialFileSerializer(
            obj.files.all(),
            many=True,
            context={"request": request},
        ).data

    def get_chapter_title(self, obj):
        if obj.chapter:
            return obj.chapter.title
        return getattr(obj, "custom_chapter", None) or "No chapter"

    def get_chapter_tags(self, obj):
        return serialize_tags(obj)

    def get_subject_id(self, obj):
        return str(obj.subject_id)

    def get_subject_name(self, obj):
        return obj.subject.name

    def get_course_title(self, obj):
        course = getattr(obj.subject, "course", None)
        return course.title if course else None

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

    def get_batch_name(self, obj):
        return obj.batch.name if obj.batch_id else None

    def get_batch_id(self, obj):
        return str(obj.batch_id) if obj.batch_id else None

    def get_chapter_id(self, obj):
        return str(obj.chapter_id) if obj.chapter_id else None


class StudyMaterialUpdateSerializer(serializers.ModelSerializer):
    """Metadata edit for an existing material. See StudyMaterialDetail.patch.

    Narrow ON PURPOSE. `subject` is absent because it is the authorization
    anchor: allowing it to move would let a teacher relocate a material onto a
    subject they are staffed on, editing content they could not otherwise
    reach — the check ran against the OLD subject. Re-file by deleting and
    re-uploading, which re-runs the upload gate.

    `uploaded_by` is absent for the same reason it isn't settable on create:
    attribution is derived from the request, never from the body.
    """

    chapter_id = serializers.PrimaryKeyRelatedField(
        source="chapter",
        queryset=Chapter.objects.all(),
        required=False,
        allow_null=True,
    )
    batch_id = serializers.PrimaryKeyRelatedField(
        source="batch",
        queryset=Batch.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = StudyMaterial
        fields = [
            "title",
            "description",
            "chapter_id",
            "batch_id",
            "chapter_note",
            "no_specific_chapter",
        ]
        extra_kwargs = {
            "title": {"required": False},
            "description": {"required": False},
            "chapter_note": {"required": False},
            "no_specific_chapter": {"required": False},
        }

    def validate_title(self, value):
        # The model's CharField is not blank=True, but ModelSerializer only
        # enforces that on a supplied value — and a PATCH that sends "   "
        # would otherwise store a material with a whitespace title that renders
        # as an unclickable blank row.
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("Give this material a title.")
        return cleaned

    def validate(self, attrs):
        # The triangle guard the create path already applies: chapter and batch
        # must both belong to THIS material's subject/course. Without it a
        # teacher staffed on two courses could file a Physics handout under a
        # History batch, and it would then be delivered to that batch.
        #
        # self.instance.subject is authoritative — subject is not editable
        # here, so it cannot have been moved earlier in this same payload.
        subject = self.instance.subject

        chapter = attrs.get("chapter", serializers.empty)
        if chapter not in (serializers.empty, None) and chapter.subject_id != subject.id:
            raise serializers.ValidationError(
                {"chapter_id": "That chapter belongs to a different subject."}
            )

        batch = attrs.get("batch", serializers.empty)
        if batch not in (serializers.empty, None) and batch.course_id != subject.course_id:
            raise serializers.ValidationError(
                {"batch_id": "That batch belongs to a different course."}
            )

        return attrs