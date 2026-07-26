from django.urls import path

from .views import (
    CreateQuizView,
    AddQuestionView,
    BulkAddQuestionsView,
    SubmitQuizForReviewView,
    StudentDashboardView,
    StartQuizView,
    SubmitQuizView,
    CheckAnswerView,
    QuizDetailView,
    QuizDetailDraftView,
    QuizResultView,
    StudentQuizSubjectsView,
    StudentQuizAttemptsView,
    TeacherDeleteQuizView,
    TeacherQuizAttemptDetailView,
    TeacherStudentAttemptsView,
    TeacherSubjectQuizListView,
    TeacherAllQuizListView,
    TeacherQuizAttemptsView,
    TeacherQuestionBankView,
    TeacherBankFiltersView,
    AdminQuizListView,
    AdminQuizDetailView,
    AdminQuizReviewView,
)

urlpatterns = [

    # ── Teacher ──────────────────────────────────────────────────────────────
    path("teacher/quizzes/", CreateQuizView.as_view()),
    path("teacher/quizzes/<uuid:pk>/questions/", AddQuestionView.as_view()),
    path("teacher/quizzes/<uuid:pk>/questions/bulk/", BulkAddQuestionsView.as_view()),
    # "publish" kept for backward compatibility; both now submit for admin review.
    path("teacher/quizzes/<uuid:pk>/publish/", SubmitQuizForReviewView.as_view()),
    path("teacher/quizzes/<uuid:pk>/submit-for-review/", SubmitQuizForReviewView.as_view()),
    path("teacher/quizzes/<uuid:pk>/delete/", TeacherDeleteQuizView.as_view()),
    path(
        "teacher/subjects/<uuid:subject_id>/quizzes/",
        TeacherSubjectQuizListView.as_view(),
    ),
    # Flat: every quiz across the subjects this teacher is assigned to.
    path(
        "teacher/quizzes/all/",
        TeacherAllQuizListView.as_view(),
    ),
    path(
        "teacher/quizzes/<uuid:pk>/attempts/",
        TeacherQuizAttemptsView.as_view(),
    ),
    path(
        "teacher/attempts/<uuid:pk>/",
        TeacherQuizAttemptDetailView.as_view(),
    ),
    path(
        "teacher/quizzes/<uuid:quiz_id>/attempts/<uuid:student_id>/",
        TeacherStudentAttemptsView.as_view(),
    ),

    # ── Teacher question bank ("finalized" reusable questions) ───────────────
    path("teacher/question-bank/", TeacherQuestionBankView.as_view()),
    path("teacher/question-bank/filters/", TeacherBankFiltersView.as_view()),

    # ── Student ───────────────────────────────────────────────────────────────
    path("student/quizzes/", StudentDashboardView.as_view()),
    path("student/quiz-subjects/", StudentQuizSubjectsView.as_view()),
    path("student/quizzes/<uuid:pk>/submit/", SubmitQuizView.as_view()),
    # Student's own attempts history for a quiz
    path("student/quizzes/<uuid:pk>/attempts/",
         StudentQuizAttemptsView.as_view()),

    # ── Shared (role-checked inside view) ─────────────────────────────────────
    path("quizzes/<uuid:pk>/", QuizDetailView.as_view()),
    # Teacher draft preview — unpublished quiz full data
    path("quizzes/<uuid:pk>/draft/", QuizDetailDraftView.as_view()),
    path("quizzes/<uuid:pk>/start/", StartQuizView.as_view()),
    path("quizzes/<uuid:pk>/result/", QuizResultView.as_view()),
    # Practice-mode instant feedback (one question at a time)
    path("quizzes/<uuid:pk>/questions/<uuid:qid>/check/", CheckAnswerView.as_view()),

    # ── Admin: Academy Quizzes (verification queue) ───────────────────────────
    path("quizzes/admin/", AdminQuizListView.as_view()),
    path("quizzes/admin/<uuid:pk>/", AdminQuizDetailView.as_view()),
    path("quizzes/admin/<uuid:pk>/review/", AdminQuizReviewView.as_view()),
]
