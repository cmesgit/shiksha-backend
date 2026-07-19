from django.urls import path
from .views import ActivityFeedView, MarkActivityReadView, MarkAllReadView
from .admin_views import AdminTeacherActivityView

urlpatterns = [
    path("feed/",                    ActivityFeedView.as_view()),
    path("feed/read-all/",           MarkAllReadView.as_view()),
    path("feed/<uuid:pk>/read/",     MarkActivityReadView.as_view()),
    path("admin/teacher-activity/",  AdminTeacherActivityView.as_view()),
]
