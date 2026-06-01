from django.urls import path
from .views import (
    EnrollmentRequestCreateView,
    MyEnrollmentRequestListView,
    TrialStatusView,
    StartTrialView,
    AdminEnrollmentRequestListView,
    AdminEnrollmentRequestActionView,
    AdminBatchRosterView,
)
urlpatterns = [
    # Student
    path("requests/", EnrollmentRequestCreateView.as_view()),
    path("requests/mine/", MyEnrollmentRequestListView.as_view()),
    path("courses/<uuid:course_id>/trial-status/", TrialStatusView.as_view()),
    path("courses/<uuid:course_id>/start-trial/", StartTrialView.as_view()),
    # Admin
    path("admin/requests/", AdminEnrollmentRequestListView.as_view()),
    path("admin/requests/<uuid:request_id>/action/", AdminEnrollmentRequestActionView.as_view()),
    path("admin/batch-roster/", AdminBatchRosterView.as_view()),
]
