from django.urls import path

from .views import (
    CreateQuizView,
    AddQuestionView,
    PublishQuizView,
    StudentDashboardView,
    StartQuizView,
    SubmitQuizView,
    QuizDetailView,
    QuizDetailDraftView,
    QuizResultView,
    StudentQuizSubjectsView,
    StudentQuizAttemptsView,
    TeacherDeleteQuizView,
    TeacherQuizAttemptDetailView,
    TeacherStudentAttemptsView,
    TeacherSubjectQuizListView,
    TeacherQuizAttemptsView,
)

urlpatterns = [

    # ── Teacher ──────────────────────────────────────────────────────────────
    path("teacher/quizzes/", CreateQuizView.as_view()),
    path("teacher/quizzes/<uuid:pk>/questions/", AddQuestionView.as_view()),
    path("teacher/quizzes/<uuid:pk>/publish/", PublishQuizView.as_view()),
    path("teacher/quizzes/<uuid:pk>/delete/", TeacherDeleteQuizView.as_view()),
    path(
        "teacher/subjects/<uuid:subject_id>/quizzes/",
        TeacherSubjectQuizListView.as_view(),
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
]
