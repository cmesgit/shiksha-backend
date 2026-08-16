from django.urls import path
from . import views
from . import group_session_views as gs_views
from . import live_files_views as lf_views
from . import remote_control_views as rc_views
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

    # --- Review + Notes ---
    path("<uuid:session_id>/review/", views.submit_private_session_review,
         name="private-session-review"),
    path("<uuid:session_id>/notes/", views.private_session_notes,
         name="private-session-notes"),

    # ✅ ADD THIS HERE (clean)
    path("subjects/<uuid:subject_id>/teachers/", subject_teachers),
    path("subjects/<uuid:subject_id>/students/", subject_students),

    # =========================================================
    # Group Sessions (separate namespace from private sessions)
    # =========================================================
    path("group-sessions/my-subjects/", gs_views.my_course_subjects,
         name="group-session-my-subjects"),
    path("group-sessions/create/", gs_views.create_group_session,
         name="group-session-create"),
    path("group-sessions/mine/", gs_views.my_group_sessions,
         name="group-session-mine"),
    # Bulk "Clear History" — must be declared BEFORE the <uuid:session_id>/
    # detail route so URL dispatch doesn't try to parse "history" as a UUID
    # and return a 404.
    path("group-sessions/history/clear/",
         gs_views.clear_my_group_session_history,
         name="group-session-history-clear"),

    # --- Instant Meeting + lookup-by-code + host controls ---
    # Declared BEFORE the <uuid:session_id>/ detail route so URL dispatch
    # doesn't try to parse "instant" / "join-by-code" as a UUID.
    path("group-sessions/instant/", gs_views.instant_create,
         name="group-session-instant-create"),
    path("group-sessions/join-by-code/", gs_views.join_by_code,
         name="group-session-join-by-code"),
    path("admin/group-sessions/", gs_views.admin_group_session_list,
         name="admin-group-session-list"),

    path("group-sessions/<uuid:session_id>/", gs_views.group_session_detail,
         name="group-session-detail"),
    path("group-sessions/<uuid:session_id>/hide/",
         gs_views.hide_group_session_for_me,
         name="group-session-hide"),
    path("group-sessions/<uuid:session_id>/invite/", gs_views.invite_more,
         name="group-session-invite-more"),
    path("group-sessions/<uuid:session_id>/reinvite/", gs_views.reinvite,
         name="group-session-reinvite"),
    path("group-sessions/<uuid:session_id>/accept/", gs_views.accept_invite,
         name="group-session-accept"),
    path("group-sessions/<uuid:session_id>/decline/", gs_views.decline_invite,
         name="group-session-decline"),
    # Un-accept — accepted invitee flips back to 'pending' before the
    # room opens (keeps their decline counter intact).
    path("group-sessions/<uuid:session_id>/unaccept/", gs_views.unaccept_invite,
         name="group-session-unaccept"),
    path("group-sessions/<uuid:session_id>/cancel/", gs_views.cancel_group_session,
         name="group-session-cancel"),
    path("group-sessions/<uuid:session_id>/join/", gs_views.join_group_session,
         name="group-session-join"),
    path("group-sessions/<uuid:session_id>/end/", gs_views.end_group_session,
         name="group-session-end"),
    path("group-sessions/<uuid:session_id>/admit-mode/", gs_views.set_admit_mode,
         name="group-session-admit-mode"),

    # --- Knock-to-join (admit_mode="lobby") ---
    path("group-sessions/<uuid:session_id>/my-join-status/", gs_views.my_join_status,
         name="group-session-my-join-status"),
    path("group-sessions/<uuid:session_id>/join-requests/", gs_views.list_join_requests,
         name="group-session-join-requests"),
    path("group-sessions/<uuid:session_id>/join-requests/<uuid:request_id>/admit/",
         gs_views.admit_join_request, name="group-session-join-request-admit"),
    path("group-sessions/<uuid:session_id>/join-requests/<uuid:request_id>/deny/",
         gs_views.deny_join_request, name="group-session-join-request-deny"),

    # --- Group-session chat ---
    # Mirrors the private-session chat endpoints. WS path lives in
    # routing.py at /ws/group-session/<id>/chat/.
    path("group-sessions/<uuid:session_id>/chat/",
         gs_views.group_session_chat_messages,
         name="group-session-chat"),
    path("group-sessions/<uuid:session_id>/chat/send/",
         gs_views.send_group_session_chat_message,
         name="group-session-chat-send"),

    # --- Group-session notes ---
    path("group-sessions/<uuid:session_id>/notes/",
         gs_views.group_session_note,
         name="group-session-notes"),

    # =========================================================
    # Live sessions — Phase 1 (design_handoff_live_sessions)
    # =========================================================
    path("group-sessions/<uuid:session_id>/preflight/",
         gs_views.group_session_preflight,
         name="group-session-preflight"),
    path("group-sessions/<uuid:session_id>/extend/",
         gs_views.extend_group_session,
         name="group-session-extend"),
    path("group-sessions/<uuid:session_id>/files/",
         lf_views.session_files,
         name="group-session-files"),
    path("group-sessions/<uuid:session_id>/files/<int:file_id>/",
         lf_views.delete_session_file,
         name="group-session-file-delete"),
    path("group-sessions/<uuid:session_id>/remote-control/request/",
         rc_views.request_remote_control,
         name="group-session-remote-control-request"),
    path("group-sessions/<uuid:session_id>/remote-control/respond/",
         rc_views.respond_remote_control,
         name="group-session-remote-control-respond"),
    path("group-sessions/<uuid:session_id>/remote-control/revoke/",
         rc_views.revoke_remote_control,
         name="group-session-remote-control-revoke"),

    # =========================================================
    # Live sessions — Phase 5 (design_handoff_live_sessions) — summary +
    # review. Additive: neither existed before this (see
    # group_session_summary's own docstring for why the handoff's assumption
    # that a review endpoint already existed was wrong).
    # =========================================================
    path("group-sessions/<uuid:session_id>/summary/",
         gs_views.group_session_summary,
         name="group-session-summary"),
    path("group-sessions/<uuid:session_id>/review/",
         gs_views.submit_group_session_review,
         name="group-session-review"),
]
