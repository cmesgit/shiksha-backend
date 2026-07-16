# PLACEMENT: shiksha-backend/forum/urls.py
#
# Notification paths remain aliased to the notifications app's Legacy* views
# (same URLs, same shapes). Everything else in this file is the redesigned
# forum surface. All routes are mounted under /api/forum/.

from django.urls import path
from .views import (
    ListTagsView,
    ListThreadsView,
    CreateThreadView,
    ThreadDetailView,
    DeleteThreadView,
    ListCommentsView,
    CreateCommentView,
    DeleteCommentView,
    TogglePostUpvoteView,
    ToggleCommentUpvoteView,
    AcceptAnswerView,
    PublicForumProfileView,
    UpdateForumProfileView,
    ListUserRepliesView,
    # redesign
    ListTopicsView,
    ListCategoriesView,
    CategoryDetailView,
    ListSpacesView,
    CreateSpaceView,
    SpaceDetailView,
    FollowSpaceView,
    FollowThreadView,
    FollowCategoryView,
    ToggleSaveView,
    ListSavedView,
    AnswerQueueView,
    SearchView,
    CreateReportView,
    ForumMeView,
)
from notifications.views import (
    LegacyListNotificationsView,
    LegacyMarkAllNotificationsReadView,
    LegacyMarkNotificationReadView,
)
from .moderation_views import (
    ModReportsView, ModReportDismissView, ModReportDeleteView, ModReportWarnView, ModReportBanView,
    ModReportSuspendView, ModReportLockView, ModReportUnlockView,
    ModAutoRejectedView, ModAutoRejectedDeleteView, ModAutoRejectedRestoreView, ModAutoRejectedBanAuthorView,
    ModUsersView, ModUserWarnView, ModUserBanView, ModUserSuspendView, ModUserUnbanView,
    ModThreadsView, ModThreadLockView, ModThreadUnlockView, ModThreadDeleteView, ModThreadRestoreView,
    ModCategoriesView, ModCategoryUpdateView, ModCategoryDeleteView, ModCategoryRestoreView,
    ModLogView, ModAnalyticsView,
)

urlpatterns = [
    # Current-user context + taxonomy
    path("me/", ForumMeView.as_view(), name="forum-me"),
    path("topics/", ListTopicsView.as_view(), name="forum-topics"),
    path("categories/", ListCategoriesView.as_view(), name="forum-categories"),
    path("categories/<str:category_id>/", CategoryDetailView.as_view(), name="forum-category-detail"),
    path("categories/<str:category_id>/follow/", FollowCategoryView.as_view(), name="forum-category-follow"),

    # Tags
    path("tags/", ListTagsView.as_view(), name="forum-tags"),

    # Spaces
    path("spaces/", ListSpacesView.as_view(), name="forum-spaces"),
    path("spaces/create/", CreateSpaceView.as_view(), name="forum-space-create"),
    path("spaces/<slug:slug>/", SpaceDetailView.as_view(), name="forum-space-detail"),
    path("spaces/<slug:slug>/follow/", FollowSpaceView.as_view(), name="forum-space-follow"),

    # Search / saved / answer queue / report
    path("search/", SearchView.as_view(), name="forum-search"),
    path("saved/", ListSavedView.as_view(), name="forum-saved"),
    path("answer-queue/", AnswerQueueView.as_view(), name="forum-answer-queue"),
    path("report/", CreateReportView.as_view(), name="forum-report"),

    # Threads
    path("threads/", ListThreadsView.as_view(), name="forum-threads"),
    path("threads/create/", CreateThreadView.as_view(), name="forum-create-thread"),
    path("threads/<int:thread_id>/", ThreadDetailView.as_view(), name="forum-thread-detail"),
    path("threads/<int:thread_id>/delete/", DeleteThreadView.as_view(), name="forum-delete-thread"),
    path("threads/<int:thread_id>/accept/<int:reply_id>/", AcceptAnswerView.as_view(), name="forum-accept-answer"),
    path("threads/<int:thread_id>/upvote/", TogglePostUpvoteView.as_view(), name="forum-toggle-post-upvote"),
    path("threads/<int:thread_id>/save/", ToggleSaveView.as_view(), name="forum-toggle-save"),
    path("threads/<int:thread_id>/follow/", FollowThreadView.as_view(), name="forum-follow-thread"),

    # Comments / answers
    path("threads/<int:thread_id>/comments/", ListCommentsView.as_view(), name="forum-comments"),
    path("threads/<int:thread_id>/comments/create/", CreateCommentView.as_view(), name="forum-create-comment"),
    path("comments/<int:comment_id>/delete/", DeleteCommentView.as_view(), name="forum-delete-comment"),
    path("comments/<int:comment_id>/upvote/", ToggleCommentUpvoteView.as_view(), name="forum-toggle-comment-upvote"),

    # Public profile
    path("profile/", UpdateForumProfileView.as_view(), name="forum-update-profile"),
    path("users/<str:username>/", PublicForumProfileView.as_view(), name="forum-public-profile"),
    path("users/<str:username>/replies/", ListUserRepliesView.as_view(), name="forum-user-replies"),

    # Notifications — LEGACY ALIASES (same URLs, same shapes, notifications table).
    path("notifications/", LegacyListNotificationsView.as_view(), name="forum-notifications"),
    path("notifications/read/", LegacyMarkAllNotificationsReadView.as_view(), name="forum-mark-all-read"),
    path("notifications/<int:notification_id>/read/", LegacyMarkNotificationReadView.as_view(), name="forum-mark-read"),

    # =====================================================
    # Moderator Panel (IsForumModerator-gated)
    # =====================================================
    path("mod/reports/", ModReportsView.as_view(), name="forum-mod-reports"),
    path("mod/reports/<int:report_id>/dismiss/", ModReportDismissView.as_view(), name="forum-mod-report-dismiss"),
    path("mod/reports/<int:report_id>/delete/", ModReportDeleteView.as_view(), name="forum-mod-report-delete"),
    path("mod/reports/<int:report_id>/warn/", ModReportWarnView.as_view(), name="forum-mod-report-warn"),
    path("mod/reports/<int:report_id>/ban/", ModReportBanView.as_view(), name="forum-mod-report-ban"),
    path("mod/reports/<int:report_id>/suspend/", ModReportSuspendView.as_view(), name="forum-mod-report-suspend"),
    path("mod/reports/<int:report_id>/lock/", ModReportLockView.as_view(), name="forum-mod-report-lock"),
    path("mod/reports/<int:report_id>/unlock/", ModReportUnlockView.as_view(), name="forum-mod-report-unlock"),

    path("mod/auto-rejected/", ModAutoRejectedView.as_view(), name="forum-mod-auto-rejected"),
    path("mod/auto-rejected/<int:submission_id>/delete/", ModAutoRejectedDeleteView.as_view(), name="forum-mod-auto-rejected-delete"),
    path("mod/auto-rejected/<int:submission_id>/restore/", ModAutoRejectedRestoreView.as_view(), name="forum-mod-auto-rejected-restore"),
    path("mod/auto-rejected/<int:submission_id>/ban-author/", ModAutoRejectedBanAuthorView.as_view(), name="forum-mod-auto-rejected-ban-author"),

    path("mod/users/", ModUsersView.as_view(), name="forum-mod-users"),
    path("mod/users/<uuid:user_id>/warn/", ModUserWarnView.as_view(), name="forum-mod-user-warn"),
    path("mod/users/<uuid:user_id>/ban/", ModUserBanView.as_view(), name="forum-mod-user-ban"),
    path("mod/users/<uuid:user_id>/suspend/", ModUserSuspendView.as_view(), name="forum-mod-user-suspend"),
    path("mod/users/<uuid:user_id>/unban/", ModUserUnbanView.as_view(), name="forum-mod-user-unban"),

    path("mod/threads/", ModThreadsView.as_view(), name="forum-mod-threads"),
    path("mod/threads/<int:thread_id>/lock/", ModThreadLockView.as_view(), name="forum-mod-thread-lock"),
    path("mod/threads/<int:thread_id>/unlock/", ModThreadUnlockView.as_view(), name="forum-mod-thread-unlock"),
    path("mod/threads/<int:thread_id>/delete/", ModThreadDeleteView.as_view(), name="forum-mod-thread-delete"),
    path("mod/threads/<int:thread_id>/restore/", ModThreadRestoreView.as_view(), name="forum-mod-thread-restore"),

    path("mod/categories/", ModCategoriesView.as_view(), name="forum-mod-categories"),
    path("mod/categories/<str:category_id>/update/", ModCategoryUpdateView.as_view(), name="forum-mod-category-update"),
    path("mod/categories/<str:category_id>/delete/", ModCategoryDeleteView.as_view(), name="forum-mod-category-delete"),
    path("mod/categories/<str:category_id>/restore/", ModCategoryRestoreView.as_view(), name="forum-mod-category-restore"),

    path("mod/log/", ModLogView.as_view(), name="forum-mod-log"),
    path("mod/analytics/", ModAnalyticsView.as_view(), name="forum-mod-analytics"),
]
