from django.urls import path
from .views import (
    EnrollmentRequestCreateView,
    MyEnrollmentRequestListView,
    AdminEnrollmentRequestListView,
    AdminEnrollmentRequestActionView,
    AdminBatchRosterView,
)
from .payment_views import PaymentConfigView, FreeEnrollView, SelectEnrollmentBatchView
from .admin_enrollment_views import (
    AdminEnrollmentListView, AdminEnrollmentActionView,
    AdminEnrollmentBulkBatchView,
)

urlpatterns = [
    # Payment mode (pluggable: free / manual_upi / razorpay)
    path("payment-config/", PaymentConfigView.as_view()),
    path("free-enroll/", FreeEnrollView.as_view()),
    path("select-batch/", SelectEnrollmentBatchView.as_view()),
    # Student
    path("requests/", EnrollmentRequestCreateView.as_view()),
    path("requests/mine/", MyEnrollmentRequestListView.as_view()),
    # Admin
    path("admin/requests/", AdminEnrollmentRequestListView.as_view()),
    path("admin/requests/<uuid:request_id>/action/", AdminEnrollmentRequestActionView.as_view()),
    path("admin/batch-roster/", AdminBatchRosterView.as_view()),
    # Admin — enrollment management (list + revoke/reactivate)
    path("admin/enrollments/", AdminEnrollmentListView.as_view()),
    path("admin/enrollments/<uuid:enrollment_id>/action/", AdminEnrollmentActionView.as_view()),
    path("admin/enrollments/bulk-batch/", AdminEnrollmentBulkBatchView.as_view()),
]
