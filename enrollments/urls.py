from django.urls import path

from .views import (
    EnrollmentRequestCreateView,
    MyEnrollmentRequestListView,
    AdminEnrollmentRequestListView,
    AdminEnrollmentRequestActionView,
)

urlpatterns = [
    # Student
    path("requests/", EnrollmentRequestCreateView.as_view()),
    path("requests/mine/", MyEnrollmentRequestListView.as_view()),

    # Admin
    path("admin/requests/", AdminEnrollmentRequestListView.as_view()),
    path("admin/requests/<uuid:request_id>/action/", AdminEnrollmentRequestActionView.as_view()),
]
