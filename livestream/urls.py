from django.urls import path
from .views import (
    join_live_session,
    create_live_session,
    cancel_live_session,
    pause_live_session,
    end_live_session,
    live_session_detail,
    live_session_status,
    livekit_webhook,
    StudentLiveSessionListView,
    TeacherLiveSessionListView,
)
from .admin_views import (
    admin_stream_list,
    admin_stream_detail,
    admin_stream_chat,
    admin_stream_end,
    admin_live_now,
    admin_recordings,
    admin_webhook_events,
    ingest_health,
)

urlpatterns = [
    path("student/sessions/", StudentLiveSessionListView.as_view()),
    path("teacher/sessions/", TeacherLiveSessionListView.as_view()),
    path("sessions/", create_live_session),
    path("sessions/<uuid:session_id>/join/", join_live_session),
    path("sessions/<uuid:session_id>/cancel/", cancel_live_session),
    path("sessions/<uuid:session_id>/pause/", pause_live_session),
    path("sessions/<uuid:session_id>/end/", end_live_session),
    path("sessions/<uuid:session_id>/detail/", live_session_detail),
    path("sessions/<uuid:session_id>/status/", live_session_status),
    path("sessions/<uuid:session_id>/health/", ingest_health),
    path("webhook/", livekit_webhook),

    # ── Admin console (is_staff) ──
    path("admin/streams/", admin_stream_list),
    path("admin/streams/<uuid:session_id>/", admin_stream_detail),
    path("admin/streams/<uuid:session_id>/chat/", admin_stream_chat),
    path("admin/streams/<uuid:session_id>/end/", admin_stream_end),
    path("admin/live-now/", admin_live_now),
    path("admin/recordings/", admin_recordings),
    path("admin/webhook-events/", admin_webhook_events),
]
