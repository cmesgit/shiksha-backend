# accounts/urls.py  ·  REFACTORED — single-password model
#
# REMOVED: path("context/teacher/password/", ...)
# There is no separate teacher password to manage.

from django.urls import path

from .rbac_views import (
    RoleListCreateView,
    RoleDetailView,
    RolePermissionsView,
    PermissionListView,
    RolesDirectoryView,
    UserRolesView,
    UserRoleDeleteView,
    ModActionsHistoryView,
)

from .auth_flow import (
    LoginView,
    MeView,
    ProfileSelectView,
    TeacherContextView,
    TeacherTrackSwitchView,
    ProfilePinView,
    ProfileListCreateView,
    ProfileDetailView,
    ProfileEmailLookupView,
    EmailCheckView,
)

from .agreement_views import (
    AdminAgreementListView, AdminAgreementDetailView, AdminAgreementSaveView,
    AdminAgreementVersionsView, AdminAgreementVersionDetailView,
    AdminAgreementRestoreView, PublicAgreementView, ReapplyAcademyView,
)
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
    path("signup/",  SignupView.as_view()),
    path("login/",   LoginView.as_view()),
    path("logout/",  LogoutView.as_view()),
    path("me/",      MeView.as_view()),

    path("email/check/", EmailCheckView.as_view()),

    path("profiles/select/",             ProfileSelectView.as_view()),
    path("profiles/lookup/",             ProfileEmailLookupView.as_view()),
    path("profiles/",                    ProfileListCreateView.as_view()),
    path("profiles/<uuid:profile_id>/",  ProfileDetailView.as_view()),
    path("profiles/pin/",                ProfilePinView.as_view()),

    # Teacher context — uses ACCOUNT password (no separate teacher password)
    path("context/teacher/", TeacherContextView.as_view()),
    path("context/teacher/track/", TeacherTrackSwitchView.as_view()),
    # context/teacher/password/ REMOVED — single-password model

    path("verify-email/",        VerifyEmailView.as_view()),
    path("resend-verification/", ResendVerificationEmailView.as_view()),
    path("refresh/",             RefreshView.as_view()),
    path("form-fillup/",         FormFillupView.as_view()),
    path("teacher/profile/",     TeacherProfileView.as_view()),
    path("student/profile/",     StudentProfileView.as_view()),
    path("change-password/",     ChangePasswordView.as_view()),

    path("password-reset/request/", PasswordResetRequestView.as_view()),
    path("password-reset/verify/",  PasswordResetVerifyView.as_view()),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view()),

    path("states/",                            StatesListView.as_view()),
    path("states/<str:state_name>/districts/", DistrictsListView.as_view()),

    path("teachers/",                          TeacherListView.as_view()),
    path("teachers/<uuid:user_id>/",           TeacherPublicProfileView.as_view()),
    path("student/<str:student_id>/validate/", ValidateStudentIdView.as_view()),

    path("admin/stats/",             AdminStatsView.as_view()),
    path("admin/users/",             AdminUserListView.as_view()),
    path("admin/users/<uuid:user_id>/", AdminUserDetailView.as_view()),
    path("admin/teacher-approvals/", AdminTeacherApprovalListView.as_view()),
    path("admin/teacher-approvals/<int:approval_id>/action/", AdminTeacherApprovalActionView.as_view()),
    # Agreement letters (admin editor + immutable version history)
    path("admin/agreements/",                          AdminAgreementListView.as_view()),
    path("admin/agreements/versions/<uuid:version_id>/",         AdminAgreementVersionDetailView.as_view()),
    path("admin/agreements/versions/<uuid:version_id>/restore/", AdminAgreementRestoreView.as_view()),
    path("admin/agreements/<slug:key>/",               AdminAgreementDetailView.as_view()),
    path("admin/agreements/<slug:key>/save/",          AdminAgreementSaveView.as_view()),
    path("admin/agreements/<slug:key>/versions/",      AdminAgreementVersionsView.as_view()),
    # Public: current agreement text for the signup screen
    path("agreements/<slug:key>/",                     PublicAgreementView.as_view()),
    path("teacher/reapply-academy/",                   ReapplyAcademyView.as_view()),

    # RBAC — roles, permissions, assignment, moderator action history
    path("admin/roles/",                            RoleListCreateView.as_view()),
    path("admin/roles/<int:role_id>/",              RoleDetailView.as_view()),
    path("admin/roles/<int:role_id>/permissions/",  RolePermissionsView.as_view()),
    path("admin/permissions/",                      PermissionListView.as_view()),
    path("admin/roles-directory/",                  RolesDirectoryView.as_view()),
    path("admin/users/<uuid:user_id>/roles/",       UserRolesView.as_view()),
    path("admin/users/<uuid:user_id>/roles/<str:role_name>/", UserRoleDeleteView.as_view()),
    path("admin/mod-actions/",                       ModActionsHistoryView.as_view()),
]
