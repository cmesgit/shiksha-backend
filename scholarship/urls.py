from django.urls import path

from . import admin_views, views

urlpatterns = [
    path("config/", views.PublicScholarshipConfigView.as_view()),

    # ── Student ──────────────────────────────────────────────────────────
    path("verification/", views.GuardianVerificationCreateView.as_view()),
    path("verification/status/", views.GuardianVerificationStatusView.as_view()),
    path("eligibility/check/", views.EligibilityCheckView.as_view()),
    path("exam/start/", views.ExamStartView.as_view()),
    path("exam/session/current/", views.CurrentExamSessionView.as_view()),
    path("exam/session/<uuid:session_id>/", views.ExamSessionDetailView.as_view()),
    path("exam/session/<uuid:session_id>/questions/", views.ExamQuestionListView.as_view()),
    path(
        "exam/session/<uuid:session_id>/questions/<uuid:question_id>/answer/",
        views.ExamAnswerView.as_view(),
    ),
    path("exam/session/<uuid:session_id>/cheat-signal/", views.ExamCheatSignalView.as_view()),
    path("exam/session/<uuid:session_id>/submit/", views.ExamSubmitView.as_view()),
    path("exam/session/<uuid:session_id>/result/", views.ExamResultView.as_view()),
    path("awards/", views.MyAwardsView.as_view()),
    path("awards/<uuid:award_id>/", views.AwardDetailView.as_view()),

    # ── Admin ────────────────────────────────────────────────────────────
    path("admin/settings/", admin_views.ScholarshipSettingsAdminView.as_view()),
    path("admin/bands/", admin_views.ScholarshipBandListCreateView.as_view()),
    path("admin/bands/<int:pk>/", admin_views.ScholarshipBandDetailView.as_view()),
    path("admin/question-bank/", admin_views.QuestionBankListCreateView.as_view()),
    path("admin/question-bank/<uuid:pk>/", admin_views.QuestionBankDetailView.as_view()),
    path("admin/question-bank/generate-ai/", admin_views.QuestionBankGenerateAIView.as_view()),
    path("admin/question-bank/bulk-create/", admin_views.QuestionBankBulkCreateView.as_view()),
    path("admin/verifications/", admin_views.GuardianVerificationQueueView.as_view()),
    path(
        "admin/verifications/<uuid:verification_id>/action/",
        admin_views.GuardianVerificationActionView.as_view(),
    ),
    path("admin/sessions/", admin_views.ExamSessionMonitorView.as_view()),
    path("admin/sessions/<uuid:session_id>/", admin_views.ExamSessionDetailAdminView.as_view()),
    path("admin/eligibility/", admin_views.EligibilityLedgerView.as_view()),
    path("admin/eligibility/<uuid:record_id>/void/", admin_views.EligibilityVoidView.as_view()),
    path("admin/awards/", admin_views.AwardListView.as_view()),
    path("admin/awards/<uuid:award_id>/void/", admin_views.AwardVoidView.as_view()),
    path("admin/stats/", admin_views.ScholarshipDashboardStatsView.as_view()),
]
