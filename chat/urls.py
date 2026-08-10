# PLACEMENT: backend/backend/chat/urls.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/urls.py
from django.urls import path
from .views import (
    ConversationListView,
    StartDirectView,
    CourseRoomView,
    MessageListView,
    MarkReadView,
    DirectoryView,
    BlockListView,
    UnblockView,
    ProfileView,
    # Stage B — conversation management + reactions/delete
    PinConversationView,
    ArchiveConversationView,
    MuteConversationView,
    ReportView,
    ReactToMessageView,
    DeleteMessageView,
    # Stage C — attachments, course hub, search
    ConversationAttachmentUploadView,
    ConversationMembersView,
    ConversationSearchView,
    GlobalSearchView,
    CourseResourcesView,
    CourseAssignmentsForHubView,
    # Stage D — announcements, support tickets, admin console
    CourseAnnouncementsView,
    SupportTicketListCreateView,
    SupportTicketMessagesView,
    SupportTicketReplyView,
    SupportTicketCloseView,
    AdminReportsQueueView,
    AdminResolveReportView,
    AdminRemoveMessageView,
    AdminMessageSearchView,
    AdminSuspensionsView,
    AdminLiftSuspensionView,
    AdminBroadcastView,
    AdminSupportTicketsView,
    AdminAssignTicketView,
    AdminTicketStatusView,
    AdminLogsView,
    AdminConversationMessagesView,
    ChatModerationOverviewView,
    # Stage E — preferences
    CommPreferenceView,
)

urlpatterns = [
    path("conversations/", ConversationListView.as_view()),
    path("conversations/direct/", StartDirectView.as_view()),
    path("conversations/course/", CourseRoomView.as_view()),
    path("conversations/<uuid:conversation_id>/messages/", MessageListView.as_view()),
    path("conversations/<uuid:conversation_id>/read/", MarkReadView.as_view()),

    # start-a-new-chat directory + blocking
    path("directory/", DirectoryView.as_view()),
    path("profile/<str:kind>/<str:target_id>/", ProfileView.as_view()),
    path("blocks/", BlockListView.as_view()),
    path("blocks/remove/", UnblockView.as_view()),

    # Stage B — CC-006/007/010 conversation + message actions
    path("conversations/<uuid:conversation_id>/pin/", PinConversationView.as_view()),
    path("conversations/<uuid:conversation_id>/archive/", ArchiveConversationView.as_view()),
    path("conversations/<uuid:conversation_id>/mute/", MuteConversationView.as_view()),
    path("conversations/<uuid:conversation_id>/report/", ReportView.as_view()),
    path("messages/<uuid:message_id>/react/", ReactToMessageView.as_view()),
    path("messages/<uuid:message_id>/delete/", DeleteMessageView.as_view()),

    # Stage C — CC-012/014/016 attachments, members, search
    path("conversations/<uuid:conversation_id>/attachments/", ConversationAttachmentUploadView.as_view()),
    path("conversations/<uuid:conversation_id>/members/", ConversationMembersView.as_view()),
    path("conversations/<uuid:conversation_id>/search/", ConversationSearchView.as_view()),
    path("search/", GlobalSearchView.as_view()),

    # Stage C — CC-013 Course Hub composition (Resources / Assignments tabs)
    path("course/<uuid:course_id>/resources/", CourseResourcesView.as_view()),
    path("course/<uuid:course_id>/assignments/", CourseAssignmentsForHubView.as_view()),

    # Stage D — CC-015 Announcements
    path("course/<uuid:course_id>/announcements/", CourseAnnouncementsView.as_view()),

    # Stage D — CC-022 Academic Support tickets
    path("support/tickets/", SupportTicketListCreateView.as_view()),
    path("support/tickets/<uuid:ticket_id>/messages/", SupportTicketMessagesView.as_view()),
    path("support/tickets/<uuid:ticket_id>/reply/", SupportTicketReplyView.as_view()),
    path("support/tickets/<uuid:ticket_id>/close/", SupportTicketCloseView.as_view()),

    # Stage E — CC-020/021 per-identity communication preferences
    path("preferences/", CommPreferenceView.as_view()),

    # Stage D — CC-023 Administrator Console
    path("admin/reports/", AdminReportsQueueView.as_view()),
    path("admin/reports/<uuid:report_id>/resolve/", AdminResolveReportView.as_view()),
    path("admin/messages/", AdminMessageSearchView.as_view()),
    path("admin/messages/<uuid:message_id>/remove/", AdminRemoveMessageView.as_view()),
    path("admin/suspensions/", AdminSuspensionsView.as_view()),
    path("admin/suspensions/<str:identity_key>/lift/", AdminLiftSuspensionView.as_view()),
    path("admin/broadcast/", AdminBroadcastView.as_view()),
    path("admin/support/tickets/", AdminSupportTicketsView.as_view()),
    path("admin/support/tickets/<uuid:ticket_id>/assign/", AdminAssignTicketView.as_view()),
    path("admin/support/tickets/<uuid:ticket_id>/status/", AdminTicketStatusView.as_view()),
    path("admin/logs/", AdminLogsView.as_view()),
    path("admin/conversations/<uuid:conversation_id>/messages/", AdminConversationMessagesView.as_view()),
    path("admin/moderation-overview/", ChatModerationOverviewView.as_view()),
]
