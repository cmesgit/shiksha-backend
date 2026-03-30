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
]
