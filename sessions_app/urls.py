from django.urls import path
from . import views
from .views import subject_teachers  # 👈 add this

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
]
