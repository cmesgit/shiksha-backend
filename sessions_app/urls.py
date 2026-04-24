from django.urls import path
from . import views
from . import study_group_views as sg_views
from .views import subject_teachers, subject_students  # 👈 add this

urlpatterns = [
    # --- Student ---
    path("request/", views.request_session, name="private-session-request"),
    path("student/", views.student_sessions, name="private-student-sessions"),
    path("<uuid:session_id>/cancel/", views.cancel_session,
         name="private-session-cancel"),
    path("<uuid:session_id>/confirm-reschedule/", views.confirm_reschedule,
         name="private-session-confirm-reschedule"),
    path("<uuid:session_id>/decline-reschedule/", views.decline_reschedule,
         name="private-session-decline-reschedule"),

    # --- Teacher ---
    path("teacher/sessions/", views.teacher_sessions,
         name="private-teacher-sessions"),
    path("teacher/requests/", views.teacher_requests,
         name="private-teacher-requests"),
    path("teacher/history/", views.teacher_history,
         name="private-teacher-history"),
    path("<uuid:session_id>/accept/", views.accept_request,
         name="private-session-accept"),
    path("<uuid:session_id>/decline/", views.decline_request,
         name="private-session-decline"),
    path("<uuid:session_id>/reschedule/", views.reschedule_request,
         name="private-session-reschedule"),
    path("<uuid:session_id>/start/", views.start_session,
         name="private-session-start"),
    path("<uuid:session_id>/end/", views.end_session, name="private-session-end"),
    path("<uuid:session_id>/teacher-cancel/", views.teacher_cancel_session,
         name="private-session-teacher-cancel"),

    # --- Shared ---
    path("<uuid:session_id>/", views.session_detail,
         name="private-session-detail"),
    path("<uuid:session_id>/join/", views.join_private_session,
         name="private-session-join"),

    # --- Chat ---
    path("<uuid:session_id>/chat/", views.session_chat_messages,
         name="private-session-chat"),
    path("<uuid:session_id>/chat/send/", views.send_chat_message,
         name="private-session-chat-send"),

    # ✅ ADD THIS HERE (clean)
    path("subjects/<uuid:subject_id>/teachers/", subject_teachers),
    path("subjects/<uuid:subject_id>/students/", subject_students),

    # =========================================================
    # Study Groups (separate namespace from private sessions)
    # =========================================================
    path("study-groups/my-subjects/", sg_views.my_course_subjects,
         name="study-group-my-subjects"),
    path("study-groups/create/", sg_views.create_study_group,
         name="study-group-create"),
    path("study-groups/mine/", sg_views.my_study_groups,
         name="study-group-mine"),
    path("study-groups/<uuid:session_id>/", sg_views.study_group_detail,
         name="study-group-detail"),
    path("study-groups/<uuid:session_id>/invite/", sg_views.invite_more,
         name="study-group-invite-more"),
    path("study-groups/<uuid:session_id>/reinvite/", sg_views.reinvite,
         name="study-group-reinvite"),
    path("study-groups/<uuid:session_id>/accept/", sg_views.accept_invite,
         name="study-group-accept"),
    path("study-groups/<uuid:session_id>/decline/", sg_views.decline_invite,
         name="study-group-decline"),
    # Un-accept — accepted invitee flips back to 'pending' before the
    # room opens (keeps their decline counter intact).
    path("study-groups/<uuid:session_id>/unaccept/", sg_views.unaccept_invite,
         name="study-group-unaccept"),
    path("study-groups/<uuid:session_id>/cancel/", sg_views.cancel_study_group,
         name="study-group-cancel"),
    path("study-groups/<uuid:session_id>/join/", sg_views.join_study_group,
         name="study-group-join"),
]
