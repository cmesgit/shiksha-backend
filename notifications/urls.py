# PLACEMENT: backend/backend/notifications/urls.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/urls.py
#
# Mount in config/urls.py:
#     path("api/notifications/", include("notifications.urls")),

from django.urls import path

from .views import (
    ListNotificationsView,
    MarkAllNotificationsReadView,
    MarkNotificationReadView,
    PreferencesView,
    UnreadCountView,
)

urlpatterns = [
    path("", ListNotificationsView.as_view(), name="notifications-list"),
    path("preferences/", PreferencesView.as_view(), name="notifications-preferences"),
    path("read/", MarkAllNotificationsReadView.as_view(), name="notifications-mark-all-read"),
    path("<int:notification_id>/read/", MarkNotificationReadView.as_view(), name="notifications-mark-read"),
    path("unread-count/", UnreadCountView.as_view(), name="notifications-unread-count"),
]
