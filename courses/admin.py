from .models import Stream
from django.contrib import admin
from .models import Course, Subject, Chapter, SubjectTeacher
from .models_recordings import SessionRecording
from .models import Board

# =========================
# COURSE ADMIN
# =========================


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("title", "board", "created_at", "stream")
    search_fields = ("title", "board__name", "stream__name")
    list_filter = ("created_at", "board", "stream")
    autocomplete_fields = ["board", "stream"]

# =========================
# SUBJECT TEACHER INLINE
# =========================


class SubjectTeacherInline(admin.TabularInline):
    model = SubjectTeacher
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
    list_display = ("name", "course", "order", "get_teachers")
    list_filter = ("course__board", "course")
    ordering = ("course", "order")
    autocomplete_fields = ["course"]
    search_fields = ("name", "course__title")

    inlines = [
        SubjectTeacherInline,
        SessionRecordingInline,
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("course__board")   # ✅ FIXED

    def get_teachers(self, obj):   # ✅ RESTORED
        subject_teachers = obj.subject_teachers.select_related("teacher")
        return ", ".join([st.teacher.email for st in subject_teachers])

    get_teachers.short_description = "Teachers"

# =========================
# CHAPTER ADMIN
# =========================


@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "get_course", "get_board", "order")

    list_filter = (
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

    def get_course(self, obj):
        return obj.subject.course

    def get_board(self, obj):
        return obj.subject.course.board

    get_course.short_description = "Course"
    get_board.short_description = "Board"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("subject__course__board")

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
