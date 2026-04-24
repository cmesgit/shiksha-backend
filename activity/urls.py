from django.urls import path
from .views import ActivityFeedView, MarkActivityReadView, MarkAllReadView

urlpatterns = [
    path("feed/",                    ActivityFeedView.as_view()),
    path("feed/read-all/",           MarkAllReadView.as_view()),
    path("feed/<uuid:pk>/read/",     MarkActivityReadView.as_view()),
]
