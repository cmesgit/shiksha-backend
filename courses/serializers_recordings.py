from django.db import transaction
from rest_framework import serializers

from .board_display import board_name_via
from .models import Batch, Chapter
from .models_recordings import SessionRecording, RecordingNote
from .chapter_tags import ChapterTagWriteMixin, serialize_tags


class SessionRecordingSerializer(serializers.ModelSerializer):

    uploaded_by_name = serializers.SerializerMethodField()
    # The flat faculty Recordings grid spans subjects, so a card has to
    # name its own. `subject` above is the raw FK id.
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_title = serializers.SerializerMethodField()
    board_name = serializers.SerializerMethodField()
    # Multi-chapter placement; `chapter` above stays the single-value view.
    chapter_tags = serializers.SerializerMethodField()
    # Length of the VISIBLE window once the trim is applied. `duration_seconds`
    # stays the full Bunny length; a client showing the wrong one shows a
    # runtime that disagrees with the scrubber.
    effective_duration_seconds = serializers.IntegerField(read_only=True)

    class Meta:
        model = SessionRecording
        fields = [
            "id",
            "subject",
            "subject_name",
            "course_title",
            "board_name",
            "chapter",
            "chapter_tags",
            "chapter_note",
            "no_specific_chapter",
            "batch",
            "live_session",
            "title",
            "description",
            "session_date",
            "duration_seconds",
            "bunny_video_id",
            "status",
            "thumbnail_url",
            "created_at",
            "is_published",
            "uploaded_by_name",
            "trim_start_seconds",
            "trim_end_seconds",
            "effective_duration_seconds",
        ]
        # READ-ONLY, every field. This serializer is only ever used to render.
        # It was previously writable, and CreateRecordingView fed it raw
        # request data — so a caller set their own `bunny_video_id`, `status`
        # and `is_published`. That view is gone; declaring the invariant here
        # is what stops a future `SessionRecordingSerializer(data=...)` from
        # quietly reopening the same hole. Writes go through
        # SessionRecordingUpdateSerializer, which has an explicit whitelist.
        read_only_fields = fields

    def get_course_title(self, obj):
        course = getattr(obj.subject, "course", None) if obj.subject_id else None
        return course.title if course else None

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

    def get_chapter_tags(self, obj):
        return serialize_tags(obj)

    def get_uploaded_by_name(self, obj):
        user = obj.uploaded_by
        if not user:
            return None
        profile = getattr(user, "profile", None)
        if profile and getattr(profile, "full_name", None):
            return profile.full_name
        return user.get_full_name() or user.username


class SessionRecordingUpdateSerializer(ChapterTagWriteMixin,
                                       serializers.ModelSerializer):
    """PATCH a recording's metadata. Modelled on TeacherAssignmentUpdateSerializer.

    THE FIELD LIST BELOW IS THE SECURITY BOUNDARY. Before this existed there
    was no update endpoint at all, so a typo in a title was only fixable
    through Django admin — but the fix must not reintroduce the hole
    CreateRecordingView had, where a raw ModelSerializer let a caller write
    `bunny_video_id`, `status` and `uploaded_by` straight through.

    Deliberately NOT writable: `id`, `subject`, `bunny_video_id`, `status`,
    `duration_seconds`, `thumbnail_url`, `uploaded_by`, `live_session`,
    `created_at`. `subject` in particular: the view authorises against the
    recording as it is on disk, so allowing a subject move would let a teacher
    relocate a recording into a subject they don't teach — passing the check on
    the way in and escaping it forever after. Same reasoning, and same guard,
    as the assignment serializer's chapter rule.
    """

    # `batch_id`, not `batch`: the edit form and SaveRecordingView both speak
    # ids. allow_null so "All batches" (course-wide) is expressible.
    batch_id = serializers.PrimaryKeyRelatedField(
        queryset=Batch.objects.all(), source="batch",
        write_only=True, required=False, allow_null=True,
    )
    chapter_id = serializers.PrimaryKeyRelatedField(
        queryset=Chapter.objects.all(), source="chapter",
        write_only=True, required=False, allow_null=True,
    )
    chapter_tags = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True,
    )
    save_chapters_to_course = serializers.BooleanField(
        required=False, write_only=True,
    )

    class Meta:
        model = SessionRecording
        fields = (
            "title",
            "description",
            "session_date",
            "batch_id",
            "is_published",
            "chapter_id",
            "chapter_tags",
            "save_chapters_to_course",
            "chapter_note",
            "no_specific_chapter",
            "trim_start_seconds",
            "trim_end_seconds",
        )
        extra_kwargs = {
            "title": {"required": False},
            "is_published": {"required": False},
            # Sending null clears the trim; omitting the key leaves it alone.
            "trim_start_seconds": {"required": False, "allow_null": True},
            "trim_end_seconds": {"required": False, "allow_null": True},
        }

    def validate_title(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError("Give the recording a title.")
        return value.strip()

    def validate(self, attrs):
        instance = self.instance

        # A batch from a DIFFERENT course is the bug SaveRecordingView already
        # guards against, for the same reason: the student read path filters on
        # `batch IS NULL OR batch_id = <their batch>`, which can never match a
        # foreign course's batch. The recording would show as published on the
        # teacher's grid while being invisible to every student alive.
        batch = attrs.get("batch")
        if batch is not None and instance is not None:
            if batch.course_id != instance.subject.course_id:
                raise serializers.ValidationError(
                    {"batch_id": "Pick a batch from this recording's own course."}
                )

        # A chapter move must stay inside the recording's own SUBJECT.
        chapter = attrs.get("chapter")
        if chapter is not None and instance is not None:
            if chapter.subject_id != instance.subject_id:
                raise serializers.ValidationError(
                    {"chapter_id": "Pick a chapter from this recording's own subject."}
                )

        self._validate_trim(attrs, instance)

        self._tag_input = self.pop_chapter_tag_input(attrs)
        return attrs

    def _validate_trim(self, attrs, instance):
        """The window must be non-empty and inside the video.

        Resolved against the MERGED state, not just what this request sent —
        PATCHing only `trim_end_seconds` has to be checked against the
        already-stored `trim_start_seconds`, or an inverted window slips
        through the serializer and is caught by the DB constraint as a 500
        instead of a 400.
        """
        def merged(field):
            if field in attrs:
                return attrs[field]
            return getattr(instance, field, None) if instance else None

        start = merged("trim_start_seconds")
        end = merged("trim_end_seconds")

        if start is not None and end is not None and end <= start:
            raise serializers.ValidationError(
                {"trim_end_seconds": "The end of the clip must come after its start."}
            )

        # duration_seconds is NULL until Bunny finishes transcoding and
        # something polls the status, so a trim on a still-processing upload
        # cannot be range-checked. Accept it rather than blocking the edit —
        # clamp_position() handles an over-long window at read time.
        duration = getattr(instance, "duration_seconds", None) if instance else None
        if duration:
            for field, value in (("trim_start_seconds", start),
                                 ("trim_end_seconds", end)):
                if value is not None and value > duration:
                    raise serializers.ValidationError({
                        field: f"This recording is only {duration} seconds long."
                    })

    @transaction.atomic
    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        tags, save_to_course, present = getattr(
            self, "_tag_input", ([], False, False),
        )
        # apply_chapter_tags keeps the legacy `chapter` FK in step via
        # primary_chapter(), so SessionRecording.chapter stays correct without
        # this method touching it.
        return self.apply_chapter_tags(
            instance, instance.subject, tags, save_to_course, present,
        )


class RecordingNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecordingNote
        fields = ["content", "updated_at"]
        read_only_fields = ["updated_at"]
