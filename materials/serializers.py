from rest_framework import serializers

from courses.board_display import board_name_via
from courses.chapter_tags import serialize_tags

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

    class Meta:
        model = StudyMaterial
        fields = [
            "id",
            "title",
            "description",
            "created_at",
            "chapter_title",
            "chapter_tags",
            "chapter_note",
            "no_specific_chapter",
            "subject_id",
            "subject_name",
            "course_title",
            "board_name",
            "batch_name",
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