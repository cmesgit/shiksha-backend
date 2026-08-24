from django.urls import path

from .views import (
    CreateQuizView,
    TeacherUpdateQuizView,
    AddQuestionView,
    BulkAddQuestionsView,
    SubmitQuizForReviewView,
    TeacherQuizAssignView,
    TeacherQuizSectionsView,
    StudentDashboardView,
    StudentQuizStatsView,
    StartQuizView,
    SubmitQuizView,
    CheckAnswerView,
    QuizDetailView,
    QuizDetailDraftView,
    QuizResultView,
    StudentQuizSubjectsView,
    StudentQuizAttemptsView,
    TeacherDeleteQuizView,
    TeacherQuizDuplicateView,
    TeacherQuizAttemptDetailView,
    TeacherStudentAttemptsView,
    TeacherSubjectQuizListView,
    TeacherAllQuizListView,
    TeacherQuizStatsView,
    TeacherQuizAttemptsView,
    TeacherQuizAnalyticsView,
    TeacherQuizRemindView,
    TeacherGenerateAIQuestionsView,
    TeacherQuestionBankView,
    TeacherBankFiltersView,
    TeacherBankSummaryView,
    TeacherBankStatusView,
    TeacherQuestionBankStateView,
    AdminQuizListView,
    AdminQuizDetailView,
    AdminQuizReviewView,
)

urlpatterns = [

    # ── Teacher ──────────────────────────────────────────────────────────────
    path("teacher/quizzes/", CreateQuizView.as_view()),
    # Builder edit-load-meta (questions load via quizzes/<pk>/draft/, save via
    # the bulk endpoint's PUT below).
    path("teacher/quizzes/<uuid:pk>/", TeacherUpdateQuizView.as_view()),
    path("teacher/quizzes/<uuid:pk>/questions/", AddQuestionView.as_view()),
    # POST appends (bulk-paste/bank), PUT replaces the full set (builder save).
    path("teacher/quizzes/<uuid:pk>/questions/bulk/", BulkAddQuestionsView.as_view()),
    # "publish" kept for backward compatibility; both now submit for admin review.
    path("teacher/quizzes/<uuid:pk>/publish/", SubmitQuizForReviewView.as_view()),
    path("teacher/quizzes/<uuid:pk>/submit-for-review/", SubmitQuizForReviewView.as_view()),
    # Make a quiz live for the teacher's own batches — no admin involved. This
    # is the one that controls student visibility; the two above only ask an
    # admin to review the questions.
    path("teacher/quizzes/<uuid:pk>/assign/", TeacherQuizAssignView.as_view()),
    # Mock-test section set. PUT replaces it, matching by id — see the view's
    # docstring for why a naive delete-and-recreate would flatten the paper.
    path("teacher/quizzes/<uuid:pk>/sections/", TeacherQuizSectionsView.as_view()),
    path("teacher/quizzes/<uuid:pk>/delete/", TeacherDeleteQuizView.as_view()),
    path("teacher/quizzes/<uuid:pk>/duplicate/", TeacherQuizDuplicateView.as_view()),
    path("teacher/quizzes/<uuid:pk>/analytics/", TeacherQuizAnalyticsView.as_view()),
    path("teacher/quizzes/<uuid:pk>/remind/", TeacherQuizRemindView.as_view()),
    path("teacher/quizzes/generate-ai/", TeacherGenerateAIQuestionsView.as_view()),
    path(
        "teacher/subjects/<uuid:subject_id>/quizzes/",
        TeacherSubjectQuizListView.as_view(),
    ),
    # Flat: every quiz across the subjects this teacher is assigned to.
    path(
        "teacher/quizzes/all/",
        TeacherAllQuizListView.as_view(),
    ),
    # T1 stat strip: attempts this week vs last (Phase 6).
    path("teacher/quizzes/stats/", TeacherQuizStatsView.as_view()),
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
    path("teacher/question-bank/summary/", TeacherBankSummaryView.as_view()),
    # T4 · ShikshaCom bank status (Phase 6).
    path("teacher/bank-status/", TeacherBankStatusView.as_view()),
    # Per-question site-bank opt-in/out (Phase 2). Question.id is a UUID.
    path("teacher/questions/<uuid:pk>/bank/", TeacherQuestionBankStateView.as_view()),

    # ── Student ───────────────────────────────────────────────────────────────
    path("student/quizzes/", StudentDashboardView.as_view()),
    path("student/quizzes/stats/", StudentQuizStatsView.as_view()),
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
