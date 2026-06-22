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
from .payment_views import PaymentConfigView, FreeEnrollView

urlpatterns = [
    # Payment mode (pluggable: free / manual_upi / razorpay)
    path("payment-config/", PaymentConfigView.as_view()),
    path("free-enroll/", FreeEnrollView.as_view()),
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
