from django.urls import path
from .views import DashboardView
from .admin_academy import AdminAcademyOptionsView, AdminAcademyResourcesView
from .admin_queue import AdminQueueView
from .admin_search import AdminSearchView
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
    # Everything waiting on an admin, across ten apps, counted in one call.
    # Same reasoning for the mount point as the two above — it belongs to none
    # of the apps it spans.
    path("admin/queue/", AdminQueueView.as_view()),
    # One search box across people, courses and the CMS. Same mount
    # reasoning as the two above — it belongs to no single app.
    path("admin/search/", AdminSearchView.as_view()),
]
