from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Course,
    Subject,
    Chapter,
    CourseDetail,
    SubjectTeacher,
)


# =========================
# COURSE ADMIN
# =========================

@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("title", "created_at", "updated_at")
    search_fields = ("title",)
    list_filter = ("created_at",)
    ordering = ("-created_at",)


# =========================
# COURSE DETAIL ADMIN
# =========================

@admin.register(CourseDetail)
class CourseDetailAdmin(admin.ModelAdmin):
    list_display = ("course", "level", "duration_weeks", "language")
    search_fields = ("course__title",)
    list_filter = ("level", "language")


# =========================
# SUBJECT TEACHER INLINE
# =========================

class SubjectTeacherInline(admin.TabularInline):
    model = SubjectTeacher
    extra = 1
    autocomplete_fields = ("teacher",)
    fields = ("teacher", "display_role", "order")
    ordering = ("order",)


# =========================
# SUBJECT ADMIN
# =========================

@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "course",
        "order",
        "teacher_list",
        "created_at",
    )

    list_filter = ("course",)
    search_fields = ("name", "course__title")
    ordering = ("course", "order")

    inlines = [SubjectTeacherInline]

    def teacher_list(self, obj):
        teachers = obj.subject_teachers.select_related("teacher")
        return ", ".join(
            f"{st.teacher.email} ({st.get_display_role_display()})"
            for st in teachers
        ) or "—"

    teacher_list.short_description = "Teachers"


# =========================
# CHAPTER ADMIN
# =========================

@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "order", "created_at")
    list_filter = ("subject__course", "subject")
    search_fields = ("title", "subject__name")
    ordering = ("subject", "order")


# =========================
# SUBJECT TEACHER ADMIN (OPTIONAL)
# =========================

@admin.register(SubjectTeacher)
class SubjectTeacherAdmin(admin.ModelAdmin):
    list_display = (
        "subject",
        "teacher",
        "display_role",
        "order",
    )

    list_filter = (
        "display_role",
        "subject__course",
    )

    search_fields = (
        "teacher__email",
        "subject__name",
        "subject__course__title",
    )

    ordering = ("subject", "order")
