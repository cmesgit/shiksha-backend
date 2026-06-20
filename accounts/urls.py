from django.urls import path

# Auth-flow views
from .auth_flow import (
    LoginView,
    MeView,
    ProfileSelectView,
    TeacherContextView,
    ProfilePinView,
    ProfileListCreateView,
    ProfileDetailView,
    ProfileEmailLookupView,
    EmailCheckView,               # NEW: email state check for signup gate
)

# Everything else stays in views.py
from .views import (
    SignupView,
    LogoutView,
    VerifyEmailView,
    ResendVerificationEmailView,
    RefreshView,
    FormFillupView,
    TeacherProfileView,
    StudentProfileView,
    StatesListView,
    DistrictsListView,
    TeacherListView,
    TeacherPublicProfileView,
    ValidateStudentIdView,
    ChangePasswordView,
    PasswordResetRequestView,
    PasswordResetVerifyView,
    PasswordResetConfirmView,
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

    # --- Email state check (unauthenticated, for signup) ---
    path("email/check/", EmailCheckView.as_view()),

    # --- Multi-profile login (step 2 + switching) ---
    path("profiles/select/", ProfileSelectView.as_view()),
    path("profiles/lookup/", ProfileEmailLookupView.as_view()),
    path("profiles/", ProfileListCreateView.as_view()),
    path("profiles/<uuid:profile_id>/", ProfileDetailView.as_view()),
    path("context/teacher/", TeacherContextView.as_view()),
    path("profiles/pin/", ProfilePinView.as_view()),

    path("verify-email/", VerifyEmailView.as_view()),
    path("resend-verification/", ResendVerificationEmailView.as_view()),
    path("refresh/", RefreshView.as_view()),
    path("form-fillup/", FormFillupView.as_view()),
    path("teacher/profile/", TeacherProfileView.as_view()),
    path("student/profile/", StudentProfileView.as_view()),
    path("change-password/", ChangePasswordView.as_view()),

    # --- Password reset (code-based, unauthenticated) ---
    path("password-reset/request/", PasswordResetRequestView.as_view()),
    path("password-reset/verify/", PasswordResetVerifyView.as_view()),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view()),

    # --- Location data ---
    path("states/", StatesListView.as_view()),
    path("states/<str:state_name>/districts/", DistrictsListView.as_view()),

    # --- Private session support ---
    path("teachers/", TeacherListView.as_view()),
    path("teachers/<uuid:user_id>/", TeacherPublicProfileView.as_view()),
    path("student/<str:student_id>/validate/", ValidateStudentIdView.as_view()),

    # --- Admin ---
    path("admin/stats/", AdminStatsView.as_view()),
    path("admin/users/", AdminUserListView.as_view()),
    path("admin/users/<uuid:user_id>/", AdminUserDetailView.as_view()),
    path("admin/teacher-approvals/", AdminTeacherApprovalListView.as_view()),
    path("admin/teacher-approvals/<int:approval_id>/action/", AdminTeacherApprovalActionView.as_view()),
]
