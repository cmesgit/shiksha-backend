from django.urls import path
from .views import DashboardView
from .admin_academy import AdminAcademyOptionsView, AdminAcademyResourcesView
from .admin_views import AdminAnalyticsView
from .teacher_resources import TeacherResourcesView

urlpatterns = [
    path("", DashboardView.as_view()),
    path("admin/analytics/", AdminAnalyticsView.as_view()),
    # The union of the four per-type teacher content lists. Mounted here rather
    # than in one of the four content apps because it belongs to none of them —
    # dashboard/ is already where cross-app teacher rollups live.
    path("teacher/resources/", TeacherResourcesView.as_view()),
    # The same list, unscoped, for the admin console — plus the option tree its
    # create forms pick from. Same reasoning for the mount point: this spans
    # courses, materials, assignments, quizzes and recordings, so it belongs to
    # none of them.
    path("admin/academy/resources/", AdminAcademyResourcesView.as_view()),
    path("admin/academy/options/", AdminAcademyOptionsView.as_view()),
]
