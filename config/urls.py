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
    # NEW: payment-mode / global settings admin API
    path("api/admin/", include("global_settings.urls")),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
