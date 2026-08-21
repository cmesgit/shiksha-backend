from rest_framework import serializers
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
import uuid
from zoneinfo import ZoneInfo

from .models import LiveSession, SessionReview, SessionNote
from courses.board_display import board_name_via
from courses.models import Subject, Batch
from courses.services import is_teacher_of

IST = ZoneInfo("Asia/Kolkata")


class LiveSessionCreateSerializer(serializers.ModelSerializer):
    subject_id = serializers.UUIDField(write_only=True)
    # A live class is a batch's timetable entry, so a batch is required for
    # new sessions (legacy batch=NULL rows stay valid — this is write-side only).
    batch_id = serializers.UUIDField(write_only=True)
    force_live = serializers.BooleanField(
        write_only=True, required=False, default=False)

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "title",
            "description",
            "start_time",
            "end_time",
            "subject_id",
            "batch_id",
            "force_live",
        ]
        read_only_fields = ["id"]

    def validate(self, data):
        request = self.context.get("request")
        user = request.user

        if not user.has_role("TEACHER"):
            raise serializers.ValidationError(
                {"non_field_errors": ["Only teachers can schedule sessions."]}
            )

        try:
            subject = Subject.objects.select_related("course").get(
                id=data["subject_id"]
            )
        except Subject.DoesNotExist:
            raise serializers.ValidationError(
                {"subject_id": ["Invalid subject."]}
            )

        try:
            batch = Batch.objects.get(id=data["batch_id"])
        except Batch.DoesNotExist:
            raise serializers.ValidationError(
                {"batch_id": ["Invalid batch."]}
            )

        # Triangle guard: the subject and the batch must be the same course.
        if subject.course_id != batch.course_id:
            raise serializers.ValidationError(
                {"batch_id": ["Batch and subject belong to different courses."]}
            )

        # Authz: assigned to this (batch, subject) — either scoped to the
        # batch, or course-wide (is_teacher_of() covers both).
        if not is_teacher_of(user, batch, subject):
            raise serializers.ValidationError(
                {"non_field_errors": ["You are not assigned to this subject."]}
            )

        start_time = data["start_time"]
        end_time = data["end_time"]

        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time, IST)

        if timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time, IST)

        data["start_time"] = start_time
        data["end_time"] = end_time

        now = timezone.now()

        if start_time >= end_time:
            raise serializers.ValidationError(
                {"end_time": ["End time must be after start time."]}
            )

        force_live = data.pop("force_live", False)
        if not force_live and start_time <= now:
            raise serializers.ValidationError(
                {"start_time": ["Cannot schedule a session in the past."]}
            )

        # Overlap is scoped to this batch+subject: two different batches may
        # legitimately hold the same subject at the same time.
        overlap_exists = LiveSession.objects.filter(
            subject=subject, batch=batch,
        ).exclude(
            status__in=[LiveSession.STATUS_CANCELLED,
                        LiveSession.STATUS_COMPLETED]
        ).filter(
            Q(start_time__lt=end_time) &
            Q(end_time__gt=start_time)
        ).exists()

        if overlap_exists:
            raise serializers.ValidationError(
                {"non_field_errors": [
                    "This session overlaps with an existing session."
                ]}
            )

        self._validated_subject = subject
        self._validated_batch = batch
        return data

    def create(self, validated_data):
        subject = self._validated_subject
        batch = self._validated_batch
        user = self.context["request"].user

        validated_data.pop("subject_id", None)
        validated_data.pop("batch_id", None)

        room_name = f"session_{uuid.uuid4().hex}"

        return LiveSession.objects.create(
            subject=subject,
            course=subject.course,
            batch=batch,
            room_name=room_name,
            created_by=user,
            **validated_data
        )


class RecurringLiveSessionSerializer(serializers.Serializer):
    """Generate a whole term's timetable in one call.

    Sessions could only ever be created one at a time, so a 6-month batch at
    two classes a week meant ~50 trips through the create form, and a
    12-month one over a hundred. That is the point where a teacher gives up
    and runs the course over WhatsApp instead.

    Deliberate choices:

    * A clash SKIPS that one date instead of failing the request. Over 50
      dates a collision with an existing class or a holiday reschedule is
      near-certain, and losing the other 49 to one bad slot is the behaviour
      that makes bulk tools untrusted. Every skip is reported back with its
      reason so nothing disappears silently.
    * Occurrences are stepped in whole DAYS on aware datetimes. India keeps a
      fixed UTC+05:30 with no daylight saving, so wall-clock time is stable
      across the series; this would need rewriting in terms of local dates if
      the platform ever runs in a DST timezone.
    * Every generated row shares a series_id so the set stays addressable.
    """

    REPEAT_DAILY = "daily"
    REPEAT_WEEKDAYS = "weekdays"
    REPEAT_WEEKLY = "weekly"

    # A term, not a lifetime. Guards against a typo'd `until` quietly
    # generating thousands of rows and their notifications.
    MAX_OCCURRENCES = 200
    _MAX_SCAN_DAYS = 800

    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    subject_id = serializers.UUIDField()
    batch_id = serializers.UUIDField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    repeat = serializers.ChoiceField(
        choices=[REPEAT_DAILY, REPEAT_WEEKDAYS, REPEAT_WEEKLY],
        default=REPEAT_WEEKLY,
    )
    # 0 = Monday .. 6 = Sunday, matching Python's weekday().
    days_of_week = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=6),
        required=False, allow_empty=True,
    )
    count = serializers.IntegerField(required=False, min_value=1)
    until = serializers.DateField(required=False)

    def validate(self, data):
        request = self.context["request"]
        user = request.user

        if not user.has_role("TEACHER"):
            raise serializers.ValidationError(
                {"non_field_errors": ["Only teachers can schedule sessions."]})

        try:
            subject = Subject.objects.select_related("course").get(id=data["subject_id"])
        except Subject.DoesNotExist:
            raise serializers.ValidationError({"subject_id": ["Invalid subject."]})
        try:
            batch = Batch.objects.get(id=data["batch_id"])
        except Batch.DoesNotExist:
            raise serializers.ValidationError({"batch_id": ["Invalid batch."]})

        if subject.course_id != batch.course_id:
            raise serializers.ValidationError(
                {"batch_id": ["Batch and subject belong to different courses."]})
        if not is_teacher_of(user, batch, subject):
            raise serializers.ValidationError(
                {"non_field_errors": ["You are not assigned to this subject."]})

        start = data["start_time"]
        end = data["end_time"]
        if timezone.is_naive(start):
            start = timezone.make_aware(start, IST)
        if timezone.is_naive(end):
            end = timezone.make_aware(end, IST)
        if start >= end:
            raise serializers.ValidationError(
                {"end_time": ["End time must be after start time."]})
        if end - start > timedelta(hours=12):
            raise serializers.ValidationError(
                {"end_time": ["A single class cannot run longer than 12 hours."]})

        if not data.get("count") and not data.get("until"):
            raise serializers.ValidationError({"non_field_errors": [
                "Give either `count` (how many classes) or `until` (a last date)."]})

        if data["repeat"] == self.REPEAT_WEEKLY and not data.get("days_of_week"):
            # Default to the weekday the first class falls on, which is what
            # "repeat weekly" means without further qualification.
            data["days_of_week"] = [start.weekday()]

        data["start_time"] = start
        data["end_time"] = end
        self._subject = subject
        self._batch = batch
        return data

    def _occurrence_starts(self, data):
        start = data["start_time"]
        repeat = data["repeat"]
        until = data.get("until")
        count = data.get("count") or self.MAX_OCCURRENCES
        count = min(count, self.MAX_OCCURRENCES)

        if repeat == self.REPEAT_DAILY:
            allowed = None
        elif repeat == self.REPEAT_WEEKDAYS:
            allowed = {0, 1, 2, 3, 4}
        else:
            allowed = set(data["days_of_week"])

        out = []
        cursor = start
        for _ in range(self._MAX_SCAN_DAYS):
            if len(out) >= count:
                break
            if until and cursor.date() > until:
                break
            if allowed is None or cursor.weekday() in allowed:
                out.append(cursor)
            cursor += timedelta(days=1)
        return out

    def create(self, validated_data):
        subject, batch = self._subject, self._batch
        user = self.context["request"].user
        duration = validated_data["end_time"] - validated_data["start_time"]
        now = timezone.now()
        series_id = uuid.uuid4()

        starts = self._occurrence_starts(validated_data)

        # Pull the batch+subject's existing timetable once rather than issuing
        # an overlap query per occurrence — 50 dates would mean 50 round trips.
        existing = list(
            LiveSession.objects.filter(subject=subject, batch=batch)
            .exclude(status__in=[LiveSession.STATUS_CANCELLED,
                                 LiveSession.STATUS_COMPLETED])
            .values_list("start_time", "end_time")
        )

        created, skipped = [], []
        for occ_start in starts:
            occ_end = occ_start + duration
            if occ_start <= now:
                skipped.append({"start_time": occ_start, "reason": "in the past"})
                continue
            if any(occ_start < e and occ_end > s for s, e in existing):
                skipped.append({"start_time": occ_start,
                                "reason": "clashes with an existing class"})
                continue

            created.append(LiveSession(
                title=validated_data["title"],
                description=validated_data.get("description", ""),
                subject=subject,
                course=subject.course,
                batch=batch,
                start_time=occ_start,
                end_time=occ_end,
                room_name=f"session_{uuid.uuid4().hex}",
                created_by=user,
                series_id=series_id,
            ))
            # Count the new class against later occurrences too, so a pattern
            # that overlaps itself cannot generate a self-clashing timetable.
            existing.append((occ_start, occ_end))

        LiveSession.objects.bulk_create(created)
        return {"series_id": series_id, "created": created, "skipped": skipped}


class LiveSessionUpdateSerializer(serializers.ModelSerializer):
    """
    Edit a session that hasn't started yet — title/description/time only.
    Subject and batch are fixed at creation; changing either would make this
    a different class's timetable entry, not a reschedule of this one.
    """

    class Meta:
        model = LiveSession
        fields = ["title", "description", "start_time", "end_time"]

    def validate(self, data):
        session = self.instance
        start_time = data.get("start_time", session.start_time)
        end_time = data.get("end_time", session.end_time)

        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time, IST)
        if timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time, IST)
        data["start_time"] = start_time
        data["end_time"] = end_time

        if start_time >= end_time:
            raise serializers.ValidationError(
                {"end_time": ["End time must be after start time."]}
            )

        if start_time <= timezone.now():
            raise serializers.ValidationError(
                {"start_time": ["Cannot reschedule to a time in the past."]}
            )

        # Same overlap rule as create, scoped to this batch+subject and
        # excluding the session being edited (it always "overlaps" itself).
        overlap_exists = LiveSession.objects.filter(
            subject=session.subject, batch=session.batch,
        ).exclude(id=session.id).exclude(
            status__in=[LiveSession.STATUS_CANCELLED,
                        LiveSession.STATUS_COMPLETED]
        ).filter(
            Q(start_time__lt=end_time) & Q(end_time__gt=start_time)
        ).exists()

        if overlap_exists:
            raise serializers.ValidationError(
                {"non_field_errors": [
                    "This time overlaps with another session."
                ]}
            )

        return data


class LiveSessionListSerializer(serializers.ModelSerializer):
    teacher = serializers.SerializerMethodField()
    can_join = serializers.SerializerMethodField()
    computed_status = serializers.SerializerMethodField()
    # Separate from computed_status ON PURPOSE: the frontends drive
    # join buttons and countdowns off computed_status, so widening it
    # with a value they have never seen would change behaviour, not
    # just wording. This is display-only.
    display_status = serializers.SerializerMethodField()
    subject_id = serializers.UUIDField(source="subject.id", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    course_name = serializers.CharField(source="course.title", read_only=True)
    board_name = serializers.SerializerMethodField()
    teacher_left_at = serializers.DateTimeField(read_only=True)
    status = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    batch_name = serializers.CharField(source="batch.name", read_only=True, default=None)
    batch_student_count = serializers.SerializerMethodField()

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "title",
            "description",
            "start_time",
            "end_time",
            "computed_status",
            "display_status",
            "teacher",
            "can_join",
            "subject_id",
            "subject_name",
            "course_name",
            "board_name",
            "teacher_left_at",
            "status",
            "batch_name",
            "batch_student_count",
        ]

    def get_board_name(self, obj):
        return board_name_via(obj, "course")

    def get_teacher(self, obj):
        # Show the host's real name (e.g. "Kavita Iyer"), not the raw login
        # email. Prefer the teacher account's SELF learner-profile name, then
        # the User's full name, then username/email as a last resort.
        u = getattr(obj, "created_by", None)
        if not u:
            return ""
        try:
            lp = u.default_learner_profile()
            if lp:
                name = f"{(lp.first_name or '').strip()} {(lp.last_name or '').strip()}".strip()
                if name:
                    return name
                if getattr(lp, "full_name", ""):
                    return lp.full_name
                if getattr(lp, "display_name", ""):
                    return lp.display_name
        except Exception:
            pass
        return u.get_full_name() or u.username or u.email

    def get_batch_student_count(self, obj):
        if not obj.batch_id:
            return None
        from enrollments.models import Enrollment
        return Enrollment.objects.filter(
            batch_id=obj.batch_id, status=Enrollment.STATUS_ACTIVE
        ).count()

    def get_display_status(self, obj):
        """MISSED when the class ended and nobody ever joined."""
        return obj.display_status()

    def get_computed_status(self, obj):
        now = timezone.now()

        if obj.status == LiveSession.STATUS_CANCELLED:
            return "CANCELLED"

        if obj.status == LiveSession.STATUS_COMPLETED:
            return "COMPLETED"

        # Sessions past end_time always show as completed
        if now >= obj.end_time:
            return "COMPLETED"

        # Manual pause takes priority over teacher_left_at timer
        if obj.status == LiveSession.STATUS_PAUSED and not obj.teacher_left_at:
            return "PAUSED"

        if obj.teacher_left_at:
            diff = now - obj.teacher_left_at
            if diff <= timedelta(minutes=10):
                return "RECONNECTING"
            if diff <= timedelta(minutes=60):
                return "PAUSED"
            return "COMPLETED"

        if obj.status == LiveSession.STATUS_LIVE:
            return "LIVE"

        if now < obj.start_time:
            return "SCHEDULED"

        return "WAITING_FOR_TEACHER"

    def get_can_join(self, obj):
        # Source of truth is the LIVE derived state, never the raw stored
        # `status` column (which the Celery sweep may not have updated yet).
        # A session past its end_time computes as COMPLETED even if the row
        # still says SCHEDULED/LIVE — so we must not offer Join for it, or the
        # card lies and the join endpoint rejects with a 400 ("Session has
        # ended") into an infinite spinner.
        status = obj.computed_status()

        if status in (LiveSession.STATUS_CANCELLED, LiveSession.STATUS_COMPLETED):
            return False

        # Allow joining paused/reconnecting/waiting sessions — the student
        # sees the appropriate holding screen inside the room.

        request = self.context.get("request")
        if request and request.user.has_role("TEACHER"):
            return True

        now = timezone.now()
        return now >= obj.start_time - timedelta(minutes=15)


class SessionReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionReview
        fields = ["id", "rating", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class SessionNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionNote
        fields = ["content", "updated_at"]
        read_only_fields = ["updated_at"]
