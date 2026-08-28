from .models_recordings import SessionRecording
from .models import Chapter
from rest_framework import serializers
from .models import Subject, Course, Board, CourseDetail, CourseCategory

# The "published & ready" recording status. Pull it from the model if it
# defines a named constant; otherwise fall back to the historical literal (4).
# Replace this with the real constant name once you confirm it on
# SessionRecording (e.g. SessionRecording.STATUS_READY).
PUBLISHED_RECORDING_STATUS = getattr(SessionRecording, "STATUS_READY", 4)


class SubjectSerializer(serializers.ModelSerializer):
    teachers = serializers.SerializerMethodField()
    chapters = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    stream_name = serializers.CharField(
        source="course.stream.name", read_only=True)
    board = serializers.SerializerMethodField()
    recordings_count = serializers.SerializerMethodField()

    class Meta:
        model = Subject
        fields = (
            "id",
            "name",
            "order",
            "image",
            "teachers",
            "chapters",
            "stream_name",
            "board",
            "recordings_count",
        )

    def get_image(self, obj):
        request = self.context.get('request')
        if obj.image:
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

    def get_teachers(self, obj):
        # Course-wide (batch=NULL) assignments only — there's no batch context
        # at this (subject) level, same as the legacy course-wide SubjectTeacher
        # this replaced.
        assignments = (
            obj.teaching_assignments
            .filter(batch__isnull=True, is_active=True)
            .select_related("teacher__teacher_profile")
            .order_by("order")
        )
        data = []
        for ta in assignments:
            teacher = ta.teacher
            if teacher is None:
                # TeachingAssignment.teacher is SET_NULL — a hard-deleted
                # teacher account has nothing left to show in this public
                # list, so skip the row rather than crash on it.
                continue
            profile = getattr(teacher, "teacher_profile", None)
            data.append({
                "id": teacher.id,
                "name": (lambda p: p.full_name if p and p.full_name else teacher.username)(teacher.self_learner_profile()),
                "display_role": ta.role,
                "qualification": profile.qualification if profile else "",
                "bio": profile.bio if profile else "",
                "rating": profile.rating if profile else None,
                "photo": profile.photo.url if profile and profile.photo else None,
            })
        return data

    def get_chapters(self, obj):
        return [
            {
                "id": str(ch.id),
                "title": ch.title,
                "order": ch.order,
            }
            for ch in obj.chapters.all().order_by("order")
        ]

    def get_board(self, obj):
        if not obj.course or not obj.course.board:
            return None
        return {
            "id": str(obj.course.board.id),
            "name": obj.course.board.name,
            "board_type": obj.course.board.board_type,
        }

    def get_recordings_count(self, obj):
        # Prefer a value annotated on the queryset by the view (no extra query).
        # In the view that lists subjects, annotate like:
        #
        #   from django.db.models import Count, Q
        #   from .serializers import PUBLISHED_RECORDING_STATUS
        #
        #   Subject.objects.annotate(
        #       published_recordings_count=Count(
        #           "recordings",
        #           filter=Q(recordings__is_published=True,
        #                    recordings__status=PUBLISHED_RECORDING_STATUS),
        #       )
        #   )
        annotated = getattr(obj, "published_recordings_count", None)
        if annotated is not None:
            return annotated

        # Fallback: one COUNT per subject (kept for safety / non-list usage).
        return obj.recordings.filter(
            is_published=True, status=PUBLISHED_RECORDING_STATUS
        ).count()


class BoardSerializer(serializers.ModelSerializer):
    logo = serializers.SerializerMethodField()

    class Meta:
        model = Board
        fields = (
            "id", "name", "board_type", "description",
            "slug", "logo", "display_order", "is_active",
        )
        read_only_fields = ("id",)

    def get_logo(self, obj):
        request = self.context.get("request")
        if obj.logo:
            if request:
                return request.build_absolute_uri(obj.logo.url)
            return obj.logo.url
        return None


class CourseDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseDetail
        fields = (
            "level", "duration_weeks", "syllabus", "language", "requirements",
            "highlights", "includes",
        )


class CourseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseCategory
        fields = (
            "id", "name", "slug", "group", "blurb", "icon",
            "display_order", "is_active",
        )
        read_only_fields = ("id",)


class CourseSerializer(serializers.ModelSerializer):
    board = BoardSerializer(read_only=True)
    board_id = serializers.PrimaryKeyRelatedField(
        source="board",
        queryset=Board.objects.all(),
        write_only=True,
        required=False,
        allow_null=True,
    )
    stream_name = serializers.CharField(source="stream.name", read_only=True)
    thumbnail = serializers.SerializerMethodField()
    details = serializers.SerializerMethodField()
    # Read-only nested representation for the admin edit form's multi-select.
    # Writes go through the raw `categories` key in request.data (a list of
    # category ids, or a JSON-encoded string of one under multipart — see
    # AdminCourseDetailView.patch), mirroring how `details` is handled: this
    # field never appears in validated_data, so it can't collide with that.
    categories = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = (
            "id",
            "title",
            "description",
            "price",
            "mrp",
            "discount_label",
            "badge",
            "is_featured",
            "display_order",
            "seo_title",
            "seo_description",
            "promo_video_url",
            "subscription_duration_days",
            "kind",
            "status",
            "class_level",
            "thumbnail",
            "details",
            "categories",
            "stream_name",
            "board",
            "board_id",
            "auto_record_enabled",
            "created_at",
            "updated_at",
        )
        # auto_record_enabled is READ-ONLY here on purpose. UpdateCourseView
        # hands this serializer the whole request payload with no allowlist and
        # is only IsTeacherContext, so a writable field would let any teacher
        # switch on automatic recording — billed per minute of egress — for a
        # whole course. Writes go through AdminCourseDetailView.patch (IsAdmin)
        # reading the raw key, the same pattern `categories` already uses.
        read_only_fields = (
            "id", "created_at", "updated_at", "auto_record_enabled",
        )

    def get_thumbnail(self, obj):
        request = self.context.get("request")
        if obj.thumbnail:
            if request:
                return request.build_absolute_uri(obj.thumbnail.url)
            return obj.thumbnail.url
        return None

    def get_details(self, obj):
        detail = getattr(obj, "details", None)
        if detail is None:
            return None
        return CourseDetailSerializer(detail).data

    def get_categories(self, obj):
        return CourseCategorySerializer(obj.categories.all(), many=True).data


class ChapterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chapter
        # is_custom / promoted_at let the picker style a teacher-typed
        # chapter differently from curated syllabus, and tell it whether an
        # admin has already accepted one into the course.
        fields = [
            "id", "title", "order", "content_html", "trusted_html",
            "is_custom", "promoted_at",
        ]
        read_only_fields = ["is_custom", "promoted_at"]


class RecordingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionRecording
        fields = [
            "id",
            "title",
            "subject",
            "chapter",
            "session_date",
            "duration_seconds",
            "bunny_video_id",
            "thumbnail_url",
            "created_at",
        ]
