from django.contrib import admin
from .models import Quiz, Question, Choice, QuizAttempt, StudentAnswer


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0
    show_change_link = True


@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "subject",
        "created_by",
        "is_published",
        "due_date",
        "total_marks",
        "created_at",
    )

    list_filter = (
        "is_published",
        "due_date",
        "subject__course",
        "subject",
    )

    search_fields = (
        "title",
        "created_by__email",
        "subject__name",
        "subject__course__title",
    )

    ordering = ("-created_at",)

    readonly_fields = (
        "created_at",
        "updated_at",
        "total_marks",
    )

    inlines = [QuestionInline]


class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 0


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("quiz", "order", "marks")
    list_filter = ("quiz__subject__course", "quiz__subject")
    ordering = ("quiz", "order")
    inlines = [ChoiceInline]


class StudentAnswerInline(admin.TabularInline):
    model = StudentAnswer
    extra = 0
    readonly_fields = ("question", "selected_choice", "is_correct")
    can_delete = False


@admin.register(QuizAttempt)
class QuizAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "student",
        "quiz",
        "score",
        "status",
        "submitted_at",
    )

    list_filter = ("status", "quiz__subject__course", "quiz__subject")
    search_fields = ("student__email", "quiz__title")
    ordering = ("-submitted_at",)

    readonly_fields = (
        "student",
        "quiz",
        "score",
        "status",
        "started_at",
        "submitted_at",
    )

    inlines = [StudentAnswerInline]


@admin.register(StudentAnswer)
class StudentAnswerAdmin(admin.ModelAdmin):
    list_display = (
        "attempt",
        "question",
        "selected_choice",
        "is_correct",
    )

    list_filter = ("is_correct", "attempt__quiz__subject__course")
    search_fields = ("attempt__student__email", "question__text")
