from django.urls import path
from .views import DashboardView
from .admin_views import AdminAnalyticsView

urlpatterns = [
    path("", DashboardView.as_view()),
    path("admin/analytics/", AdminAnalyticsView.as_view()),
]
