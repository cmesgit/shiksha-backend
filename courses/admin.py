from .models import Stream
from django.contrib import admin
from django.db.models import Count, Q
from django.utils import timezone
from .models import Course, Subject, Chapter, TeachingAssignment, Batch
from .models_recordings import SessionRecording
from .models import Board, BoardNotifyRequest, CourseNotifyRequest

# =========================
# COURSE ADMIN
# =========================


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("title", "board", "status", "created_at", "stream")
    search_fields = ("title", "board__name", "stream__name")
    list_filter = ("status", "created_at", "board", "stream")
    autocomplete_fields = ["board", "stream"]

# =========================
# TEACHING ASSIGNMENT INLINE
# =========================


class TeachingAssignmentInline(admin.TabularInline):
    model = TeachingAssignment
    extra = 1


# =========================
# SESSION RECORDING INLINE
# =========================

class SessionRecordingInline(admin.TabularInline):
    model = SessionRecording
    extra = 1
    fields = (
        "title",
        "chapter",
        "session_date",
        "duration_seconds",
        "bunny_video_id",
        "is_published",
    )


# =========================
# SUBJECT ADMIN
# =========================

@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "course", "textbook", "order", "get_teachers")
    list_filter = ("course__board", "course")
    ordering = ("course", "order")
    autocomplete_fields = ["course"]
    search_fields = ("name", "course__title")

    inlines = [
        TeachingAssignmentInline,
        SessionRecordingInline,
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("course__board")

    def get_teachers(self, obj):
        assignments = obj.teaching_assignments.filter(is_active=True).select_related("teacher")
        return ", ".join([ta.teacher.email for ta in assignments])

    get_teachers.short_description = "Teachers"

# =========================
# CHAPTER ADMIN
# =========================


@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    # is_covered is editable right in the list for quick ticking.
    list_display = ("title", "subject", "get_course", "get_board", "order", "is_covered")
    list_editable = ("is_covered",)

    list_filter = (
        "is_covered",
        "subject__course__board",
        "subject__course",
        "subject",
    )

    search_fields = (
        "title",
        "subject__name",
        "subject__course__title",
        "subject__course__board__name",
    )

    ordering = ("subject__course", "subject", "order")

    autocomplete_fields = ["subject"]
    readonly_fields = ("covered_at", "marked_by")

    def get_course(self, obj):
        return obj.subject.course

    def get_board(self, obj):
        return obj.subject.course.board

    get_course.short_description = "Course"
    get_board.short_description = "Board"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("subject__course__board")

    def save_model(self, request, obj, form, change):
        # Stamp covered_at / marked_by when ticked from the change form.
        # (Bulk ticks via the list view set is_covered only; covered_at is
        # informational and progress uses is_covered, so that's fine.)
        if "is_covered" in form.changed_data:
            if obj.is_covered:
                obj.covered_at = obj.covered_at or timezone.now()
                obj.marked_by = request.user
            else:
                obj.covered_at = None
                obj.marked_by = None
        super().save_model(request, obj, form, change)

# =========================
# SESSION RECORDING ADMIN
# =========================


@admin.register(SessionRecording)
class SessionRecordingAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "subject",
        "chapter",
        "session_date",
        "is_published",
        "uploaded_by",
    )
    list_filter = ("subject", "is_published")
    search_fields = ("title", "subject__name")
    ordering = ("-session_date",)
    readonly_fields = ("created_at",)


@admin.register(Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ("name", "board_type", "created_at", "course_count")
    list_filter = ("board_type", "created_at")
    search_fields = ("name",)
    ordering = ("board_type", "name")

    def course_count(self, obj):
        return obj.courses.count()

    course_count.short_description = "Courses"


@admin.register(Stream)
class StreamAdmin(admin.ModelAdmin):
    search_fields = ["name"]


@admin.register(BoardNotifyRequest)
class BoardNotifyRequestAdmin(admin.ModelAdmin):
    list_display = ("board", "email", "created_at")
    list_filter = ("board",)
    search_fields = ("email", "board__name")
    ordering = ("-created_at",)


@admin.register(CourseNotifyRequest)
class CourseNotifyRequestAdmin(admin.ModelAdmin):
    list_display = ("course", "email", "created_at")
    list_filter = ("course",)
    search_fields = ("email", "course__title")
    ordering = ("-created_at",)


# =========================
# BATCH ADMIN
# =========================


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "course", "year", "is_active", "seats_taken", "capacity")
    list_filter = ("is_active", "year", "course__board", "course")
    search_fields = ("name", "code", "course__title")
    autocomplete_fields = ["course"]
    ordering = ("-year", "code")

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("course")
        return qs.annotate(
            _seats=Count("enrollments", filter=Q(enrollments__status="ACTIVE"))
        )

    def seats_taken(self, obj):
        return obj._seats

    seats_taken.short_description = "Seats taken"
    seats_taken.admin_order_field = "_seats"
