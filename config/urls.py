# Place at: config/urls.py

from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.conf.urls.static import static
from django.conf import settings

from content.sitemaps import CONTENT_SITEMAPS
from .media_views import secure_media_view

from global_settings.views import PublicConfigView

urlpatterns = [
    path("admin/", admin.site.urls),
    # Authenticated gate for private media (see config/media_security.py) —
    # this is the only door in; nginx's own /media/ alias no longer serves
    # anything outside the explicit public sub-paths.
    path("api/media/secure/<path:name>", secure_media_view),
    path("sitemap.xml", sitemap, {"sitemaps": CONTENT_SITEMAPS},
         name="django.contrib.sitemaps.views.sitemap"),
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
    # Anonymous-readable flag allowlist for the public marketing site. Mounted
    # at the API root rather than under /api/admin/ so the path itself does not
    # imply an admin-gated resource. See PublicConfigView — it emits ONLY the
    # names in its PUBLIC_FLAGS tuple, never the settings serializer, which
    # carries payment credentials.
    path("api/public-config/", PublicConfigView.as_view()),
    path("api/forum/", include("forum.urls")),  # ← ADDED
    path("api/explore/", include("documents.urls")),  # ← ADDED (Explore document library)
    # Canonical notification API (bell + preferences). The legacy alias
    # /api/forum/notifications/ above keeps working; this snapshot was
    # missing both mounts below (likely server-side urls.py edits that
    # never landed back in the repo — reconcile on deploy).
    path("api/notifications/", include("notifications.urls")),
    path("api/counseling/", include("counseling.urls")),
    path("api/content/", include("content.urls")),  # ← ADDED (content CMS app)
    path("api/news/", include("news.urls")),  # ← ADDED (GNews proxy)
    path("api/scholarship/", include("scholarship.urls")),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
