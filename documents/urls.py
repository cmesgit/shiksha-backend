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

urlpatterns = [
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
]
