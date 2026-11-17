# documents/urls.py — the Explore document library. Mounted under /api/explore/.
# Public routes first, then the IsDocumentsModerator-gated mod/... block.

from django.urls import path

from .views import (
    DocumentsMeView, FacetsView, LandingView,
    DocumentsView, DocumentDetailView,
    ToggleSaveView, ToggleLikeView, RecordViewView, RecordDownloadView,
    CreateReportView,
    AuthorDetailView, FollowAuthorView,
    CollectionsView, CollectionDetailView,
    CollectionDocumentsView, CollectionDocumentDetailView,
)
from .moderation_views import (
    ModReportsView, ModReportDismissView, ModReportRemoveView,
    ModReportWarnView, ModReportSuspendView, ModReportBanView,
    ModDuplicatesView, ModDuplicateConfirmView, ModDuplicateDismissView,
    ModUploadersView, ModUploaderWarnView, ModUploaderSuspendView,
    ModUploaderBanView, ModUploaderUnbanView,
    ModLogView, ModAnalyticsView,
)

from .research_views import (ResearchListView, ResearchDetailView, ResearchRevisionView,
    ResearchFileView, ResearchReviewersView, ResearchReviewerAdminView, ResearchCorrectionView)

from .verification_views import (
    VerificationOverviewView, VerificationQueueView, VerificationTargetView,
    NewVersionView,
    VerificationNotificationsView, VerificationNotificationsReadView,
    SpecializationsView, SpecializationDetailView,
    AdminSettingsView, AdminReviewersView, AdminReviewerActionView,
    AdminAttentionView, AdminAttentionActionView, AdminEligibleReviewersView,
)

urlpatterns = [
    path("research/", ResearchListView.as_view()),
    path("research/reviewers/", ResearchReviewersView.as_view()),
    path("research/reviewers/<int:reviewer_id>/", ResearchReviewerAdminView.as_view()),
    path("research/<int:paper_id>/", ResearchDetailView.as_view()),
    path("research/<int:paper_id>/corrections/", ResearchCorrectionView.as_view()),
    path("research/<int:paper_id>/revisions/", ResearchRevisionView.as_view()),
    path("research/<int:paper_id>/revisions/<int:revision_id>/file/", ResearchFileView.as_view()),
    # Current-user context + taxonomy
    path("me/", DocumentsMeView.as_view(), name="explore-me"),
    path("facets/", FacetsView.as_view(), name="explore-facets"),
    path("landing/", LandingView.as_view(), name="explore-landing"),

    # Documents — GET search / POST upload share the list route
    path("documents/", DocumentsView.as_view(), name="explore-documents"),
    path("documents/<int:document_id>/", DocumentDetailView.as_view(), name="explore-document-detail"),
    path("documents/<int:document_id>/save/", ToggleSaveView.as_view(), name="explore-document-save"),
    path("documents/<int:document_id>/like/", ToggleLikeView.as_view(), name="explore-document-like"),
    path("documents/<int:document_id>/view/", RecordViewView.as_view(), name="explore-document-view"),
    path("documents/<int:document_id>/download/", RecordDownloadView.as_view(), name="explore-document-download"),
    path("documents/<int:document_id>/report/", CreateReportView.as_view(), name="explore-document-report"),

    # Authors (contributors)
    path("authors/<str:author_key>/", AuthorDetailView.as_view(), name="explore-author-detail"),
    path("authors/<str:author_key>/follow/", FollowAuthorView.as_view(), name="explore-author-follow"),

    # Collections
    path("collections/", CollectionsView.as_view(), name="explore-collections"),
    path("collections/<slug:slug>/", CollectionDetailView.as_view(), name="explore-collection-detail"),
    path("collections/<slug:slug>/documents/", CollectionDocumentsView.as_view(), name="explore-collection-documents"),
    path("collections/<slug:slug>/documents/<int:document_id>/", CollectionDocumentDetailView.as_view(), name="explore-collection-document-detail"),

    # =====================================================
    # Explore Moderation panel (IsDocumentsModerator-gated)
    # =====================================================
    path("mod/reports/", ModReportsView.as_view(), name="explore-mod-reports"),
    path("mod/reports/<int:report_id>/dismiss/", ModReportDismissView.as_view(), name="explore-mod-report-dismiss"),
    path("mod/reports/<int:report_id>/remove/", ModReportRemoveView.as_view(), name="explore-mod-report-remove"),
    path("mod/reports/<int:report_id>/warn/", ModReportWarnView.as_view(), name="explore-mod-report-warn"),
    path("mod/reports/<int:report_id>/suspend/", ModReportSuspendView.as_view(), name="explore-mod-report-suspend"),
    path("mod/reports/<int:report_id>/ban/", ModReportBanView.as_view(), name="explore-mod-report-ban"),

    path("mod/duplicates/", ModDuplicatesView.as_view(), name="explore-mod-duplicates"),
    path("mod/duplicates/<int:flag_id>/confirm/", ModDuplicateConfirmView.as_view(), name="explore-mod-duplicate-confirm"),
    path("mod/duplicates/<int:flag_id>/dismiss/", ModDuplicateDismissView.as_view(), name="explore-mod-duplicate-dismiss"),

    path("mod/uploaders/", ModUploadersView.as_view(), name="explore-mod-uploaders"),
    path("mod/uploaders/<uuid:user_id>/warn/", ModUploaderWarnView.as_view(), name="explore-mod-uploader-warn"),
    path("mod/uploaders/<uuid:user_id>/suspend/", ModUploaderSuspendView.as_view(), name="explore-mod-uploader-suspend"),
    path("mod/uploaders/<uuid:user_id>/ban/", ModUploaderBanView.as_view(), name="explore-mod-uploader-ban"),
    path("mod/uploaders/<uuid:user_id>/unban/", ModUploaderUnbanView.as_view(), name="explore-mod-uploader-unban"),

    path("mod/log/", ModLogView.as_view(), name="explore-mod-log"),
    path("mod/analytics/", ModAnalyticsView.as_view(), name="explore-mod-analytics"),

    # =====================================================
    # Community verification
    # (shiksha-backend/docs/EXPLORE_VERIFICATION.md)
    #
    # Every route 404s while GlobalSettings.explore_verification_enabled is
    # off — see verification_views._require_feature. The admin block is
    # superuser-or-ADMIN, NOT is_staff: content editors hold is_staff for CMS
    # work and must not also hold the switch that decides what the library
    # counts as checked.
    # =====================================================
    path("verification/", VerificationOverviewView.as_view(), name="explore-verification"),
    path("verification/queue/", VerificationQueueView.as_view(), name="explore-verification-queue"),
    path("verification/notifications/", VerificationNotificationsView.as_view(), name="explore-verification-notifications"),
    path("verification/notifications/read/", VerificationNotificationsReadView.as_view(), name="explore-verification-notifications-read"),
    path("verification/specializations/", SpecializationsView.as_view(), name="explore-verification-specializations"),
    path("verification/specializations/<int:spec_id>/", SpecializationDetailView.as_view(), name="explore-verification-specialization-detail"),
    path("verification/sets/<int:document_id>/", VerificationTargetView.as_view(), name="explore-verification-set"),
    path("verification/sets/<int:document_id>/review/", VerificationTargetView.as_view(), name="explore-verification-review"),
    path("verification/sets/<int:document_id>/version/", NewVersionView.as_view(), name="explore-verification-new-version"),

    path("verification/admin/settings/", AdminSettingsView.as_view(), name="explore-verification-admin-settings"),
    path("verification/admin/reviewers/", AdminReviewersView.as_view(), name="explore-verification-admin-reviewers"),
    path("verification/admin/reviewers/<int:spec_id>/<str:action>/", AdminReviewerActionView.as_view(), name="explore-verification-admin-reviewer-action"),
    path("verification/admin/attention/", AdminAttentionView.as_view(), name="explore-verification-admin-attention"),
    path("verification/admin/attention/<int:document_id>/<str:action>/", AdminAttentionActionView.as_view(), name="explore-verification-admin-attention-action"),
    path("verification/admin/sets/<int:document_id>/reviewers/", AdminEligibleReviewersView.as_view(), name="explore-verification-admin-eligible"),
]
