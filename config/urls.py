# Place at: config/urls.py

from django.contrib import admin
from django.urls import path, include
from django.conf.urls.static import static
from django.conf import settings

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/accounts/", include("accounts.urls")),
    path("api/courses/", include("courses.urls")),
    path("api/assignments/", include("assignments.urls")),
    path("api/", include("quizzes.urls")),
    path("api/livestream/", include("livestream.urls")),
    path("api/sessions/", include("sessions_app.urls")),
    path("api/dashboard/", include("dashboard.urls")),
    path("api/activity/", include("activity.urls")),
    path("api/materials/", include("materials.urls")),
    path("api/enrollments/", include("enrollments.urls")),
    path("api/payments/", include("payments.urls")),
    path("api/skill/", include("skills.urls")),
    path("api/chat/", include("chat.urls")),
    path("api/admin/", include("global_settings.urls")),
    path("api/forum/", include("forum.urls")),  # ← ADDED
    # Canonical notification API (bell + preferences). The legacy alias
    # /api/forum/notifications/ above keeps working; this snapshot was
    # missing both mounts below (likely server-side urls.py edits that
    # never landed back in the repo — reconcile on deploy).
    path("api/notifications/", include("notifications.urls")),
    path("api/counseling/", include("counseling.urls")),
    path("api/content/", include("content.urls")),  # ← ADDED (content CMS app)
    path("api/news/", include("news.urls")),  # ← ADDED (GNews proxy)
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
