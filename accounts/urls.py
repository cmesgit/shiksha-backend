from django.urls import path
from .views import (
    SignupView,
    LoginView,
    LogoutView,
    MeView,
    VerifyEmailView,
    ResendVerificationEmailView,
    RefreshView,
    FormFillupView,
    TeacherProfileView,
    StatesListView,
    DistrictsListView,
    TeacherListView,
    ValidateStudentIdView,
    ChangePasswordView,
    AdminStatsView,
    AdminUserListView,
    AdminUserDetailView,
    AdminTeacherApprovalListView,
    AdminTeacherApprovalActionView,
)

urlpatterns = [
    path("signup/", SignupView.as_view()),
    path("login/", LoginView.as_view()),
    path("logout/", LogoutView.as_view()),
    path("me/", MeView.as_view()),
    path("verify-email/", VerifyEmailView.as_view()),
    path("resend-verification/", ResendVerificationEmailView.as_view()),
    path("refresh/", RefreshView.as_view()),
    path("form-fillup/", FormFillupView.as_view()),
    path("teacher/profile/", TeacherProfileView.as_view()),
    path("change-password/", ChangePasswordView.as_view()),

    # --- Location data ---
    path("states/", StatesListView.as_view()),
    path("states/<str:state_name>/districts/", DistrictsListView.as_view()),

    # --- Private session support ---
    path("teachers/", TeacherListView.as_view()),
    path("student/<str:student_id>/validate/", ValidateStudentIdView.as_view()),

    # --- Admin ---
    path("admin/stats/", AdminStatsView.as_view()),
    path("admin/users/", AdminUserListView.as_view()),
    path("admin/users/<uuid:user_id>/", AdminUserDetailView.as_view()),
    path("admin/teacher-approvals/", AdminTeacherApprovalListView.as_view()),
    path("admin/teacher-approvals/<int:approval_id>/action/", AdminTeacherApprovalActionView.as_view()),
]
