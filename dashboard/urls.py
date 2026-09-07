from django.urls import path
from .views import DashboardView
from .admin_views import AdminAnalyticsView
from .teacher_resources import TeacherResourcesView

urlpatterns = [
    path("", DashboardView.as_view()),
    path("admin/analytics/", AdminAnalyticsView.as_view()),
    # The union of the four per-type teacher content lists. Mounted here rather
    # than in one of the four content apps because it belongs to none of them —
    # dashboard/ is already where cross-app teacher rollups live.
    path("teacher/resources/", TeacherResourcesView.as_view()),
]
