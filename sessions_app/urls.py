from django.urls import path
from . import views
from . import group_session_views as gs_views
from . import instant_guest_views as guest_views
from . import live_files_views as lf_views
from . import remote_control_views as rc_views
from .collaboration import device_views
from .collaboration import grant_views
from .collaboration import views as collab_views
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
    path("<uuid:session_id>/files/", lf_views.private_session_files,
         name="private-session-files"),
    path("<uuid:session_id>/files/<int:file_id>/", lf_views.delete_private_session_file,
         name="private-session-file-delete"),

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
    path("group-sessions/guest/request/", guest_views.request_guest_join,
         name="instant-guest-request"),
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
    path("group-sessions/<uuid:session_id>/guest/status/", guest_views.guest_status,
         name="instant-guest-status"),
    path("group-sessions/<uuid:session_id>/guest/join/", guest_views.guest_join,
         name="instant-guest-join"),
    path("group-sessions/<uuid:session_id>/guest/<uuid:guest_id>/remove/",
         guest_views.remove_guest, name="instant-guest-remove"),
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

    # =========================================================
    # Shiksha Remote — collaboration control plane (RC-006).
    #
    # Prefix fixed by docs/architecture/shiksha-remote/api-contracts.md:
    # "/api/sessions/collaboration/v1/". Purely additive — no existing route,
    # join URL or model changes, and a binding is registration AFTER the
    # existing join, never a second login.
    #
    # 🔴 Every route below answers 404 while
    # GlobalSettings.collaboration_bindings_enabled is False, which is its
    # default. That is NOT live_remote_access_enabled, which defaults True
    # and gates the dormant legacy path in remote_control_views.py.
    #
    # The socket itself is wired in sessions_app/routing.py, which
    # config/asgi.py already includes — so RC-006b needed no asgi change.
    # =========================================================
    path("collaboration/v1/csrf/",
         collab_views.csrf_bootstrap,
         name="collaboration-csrf"),
    path("collaboration/v1/bindings/",
         collab_views.create_binding,
         name="collaboration-binding-create"),
    path("collaboration/v1/bindings/<str:binding_id>/heartbeat/",
         collab_views.heartbeat_binding,
         name="collaboration-binding-heartbeat"),
    path("collaboration/v1/bindings/<str:binding_id>/",
         collab_views.delete_binding,
         name="collaboration-binding-delete"),
    path("collaboration/v1/snapshot/",
         collab_views.snapshot,
         name="collaboration-snapshot"),
    path("collaboration/v1/ws-ticket/",
         collab_views.ws_ticket,
         name="collaboration-ws-ticket"),

    # Pairing (RC-007a). Two authentication planes:
    #   pairings/ and pairings/<id>/{poll,finalize}/ are DEVICE routes —
    #   DPoP / pairing-secret only, never a user cookie (api-contracts.md).
    #   pairings/confirm/ is the one browser route, and it deliberately
    #   cannot complete a pairing on its own (threat T02).
    path("collaboration/v1/pairings/",
         device_views.create_pairing,
         name="collaboration-pairing-create"),
    path("collaboration/v1/pairings/confirm/",
         device_views.confirm_pairing,
         name="collaboration-pairing-confirm"),
    path("collaboration/v1/pairings/<str:pairing_id>/poll/",
         device_views.poll_pairing,
         name="collaboration-pairing-poll"),
    path("collaboration/v1/pairings/<str:pairing_id>/finalize/",
         device_views.finalize_pairing,
         name="collaboration-pairing-finalize"),

    # Credential life (RC-007b). device-token/ and device-heartbeat/ are
    # DEVICE routes; devices/ and devices/<id>/ are browser routes scoped to
    # the owning account — there is no global device discovery.
    path("collaboration/v1/device-token/",
         device_views.device_token,
         name="collaboration-device-token"),
    path("collaboration/v1/devices/",
         device_views.list_devices,
         name="collaboration-device-list"),
    path("collaboration/v1/devices/<str:device_id>/",
         device_views.unlink_device,
         name="collaboration-device-unlink"),
    path("collaboration/v1/device-heartbeat/",
         device_views.device_heartbeat,
         name="collaboration-device-heartbeat"),

    # Control grants (RC-008 part 2d). Two authentication planes on one
    # resource: browser routes take cookie + CSRF + exact Origin, device
    # routes take a device token + DPoP proof and consult no cookie. The
    # three shared routes dispatch on the credential actually presented, so
    # a caller cannot choose which plane validates it.
    #
    # 🔴 These answer 404 unless BOTH collaboration_bindings_enabled and
    # collaboration_control_enabled are on; both ship False.
    path("collaboration/v1/grants/",
         grant_views.create_grant,
         name="collaboration-grant-create"),
    path("collaboration/v1/grants/<str:grant_id>/decision/",
         grant_views.decide_grant,
         name="collaboration-grant-decision"),
    path("collaboration/v1/grants/<str:grant_id>/activate/",
         grant_views.activate_grant,
         name="collaboration-grant-activate"),
    path("collaboration/v1/grants/<str:grant_id>/resume/",
         grant_views.resume_grant,
         name="collaboration-grant-resume"),
    path("collaboration/v1/grants/<str:grant_id>/resume-intent/",
         grant_views.resume_intent,
         name="collaboration-grant-resume-intent"),
    path("collaboration/v1/grants/<str:grant_id>/lease/",
         grant_views.renew_lease,
         name="collaboration-grant-lease"),
    path("collaboration/v1/grants/<str:grant_id>/pause/",
         grant_views.pause_grant,
         name="collaboration-grant-pause"),
    path("collaboration/v1/grants/<str:grant_id>/terminate/",
         grant_views.terminate_grant,
         name="collaboration-grant-terminate"),
    # Declared last: a bare <id>/ would otherwise swallow the action routes
    # above, since "decision" and the rest parse happily as <str:grant_id>.
    path("collaboration/v1/grants/<str:grant_id>/",
         grant_views.read_grant,
         name="collaboration-grant-read"),
]
