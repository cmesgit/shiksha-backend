from accounts.email_utils import send_gmail
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated, AllowAny, IsAdminUser
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.contrib.auth import authenticate
from django.shortcuts import redirect

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError

from django.db.models import Prefetch

from enrollments.models import Enrollment

from accounts.audit import log_auth_event
from accounts.models import (
    AuthEvent,
    User,
    EmailVerificationToken,
    Role,
    UserRole,
)

from accounts.throttles import (
    LoginRateThrottle,
    ResendVerificationRateThrottle,
)

from .serializers import (
    SignupSerializer,
    UserMeSerializer,
    StudentFormFillupSerializer,
    TeacherFormFillupSerializer,
)

from .models import TeacherProfile

from .permissions import IsEmailVerified


# =====================================================
# VERIFIED USERS ONLY
# =====================================================

class MeView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        user = (
            User.objects
            .select_related("profile")
            .prefetch_related(
                Prefetch(
                    "enrollments",
                    queryset=Enrollment.objects
                    .filter(status="ACTIVE")
                    .select_related("course")
                ),
                "user_roles__role"
            )
            .get(id=request.user.id)
        )

        return Response(UserMeSerializer(user).data)


# =====================================================
# SIGNUP — PUBLIC
# =====================================================


class SignupView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()

        # 🔥 DELETE OLD TOKENS (important)
        EmailVerificationToken.objects.filter(user=user).delete()

        token = EmailVerificationToken.generate(user)

        verify_link = (
            f"https://api.shikshacom.com/api/accounts/verify-email/?token={token.token}"
        )

        html = f"""
        <h2>Verify your email</h2>
        <p>Click the button below:</p>
        <a href="{verify_link}" style="padding:10px 15px;background:#2563eb;color:white;text-decoration:none;border-radius:5px;">
            Verify Email
        </a>
        """

        send_gmail(
            to=user.email,
            subject="Verify your email",
            message_text=f"Click to verify:\n{verify_link}",
            html=html,
        )

        return Response(
            {"detail": "Signup successful. Please verify your email."},
            status=status.HTTP_201_CREATED,
        )

# =====================================================
# LOGIN — JWT ISSUED ONLY IF VERIFIED
# =====================================================


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        email = request.data.get("email", "").strip().lower()
        password = request.data.get("password")

        if not email or not password:
            raise ValidationError("Email and password are required.")

        user = authenticate(request, email=email, password=password)

        if not user:
            log_auth_event(request, AuthEvent.EVENT_LOGIN_FAILED)
            raise ValidationError("Invalid credentials.")

        if not user.is_verified:
            log_auth_event(
                request,
                AuthEvent.EVENT_LOGIN_BLOCKED_UNVERIFIED,
                user=user,
            )
            raise ValidationError("Email not verified.")

        refresh = RefreshToken.for_user(user)

        response = Response(
            {"user": UserMeSerializer(user).data},
            status=status.HTTP_200_OK,
        )

        # Clear old cookies
        response.delete_cookie("access", domain=".shikshacom.com")
        response.delete_cookie("refresh", domain=".shikshacom.com")

        response.set_cookie(
            key="access",
            value=str(refresh.access_token),
            httponly=True,
            secure=True,
            samesite="None",
            domain=".shikshacom.com",
            max_age=6000,
        )

        response.set_cookie(
            key="refresh",
            value=str(refresh),
            httponly=True,
            secure=True,
            samesite="None",
            domain=".shikshacom.com",
            max_age=60 * 60 * 24 * 7,
        )

        log_auth_event(request, AuthEvent.EVENT_LOGIN_SUCCESS, user=user)

        return response


# =====================================================
# EMAIL VERIFICATION
# =====================================================

class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        token_value = request.query_params.get("token")

        if not token_value:
            return redirect("https://shikshacom.com/email-verified?status=failed")

        try:
            token = EmailVerificationToken.objects.select_related("user").get(
                token=token_value,
                expires_at__gt=timezone.now(),
            )
        except EmailVerificationToken.DoesNotExist:
            log_auth_event(request, AuthEvent.EVENT_VERIFY_EMAIL_FAILED)
            return redirect("https://shikshacom.com/email-verified?status=failed")

        user = token.user

        if not user.is_verified:
            user.is_verified = True
            user.verified_at = timezone.now()
            user.save(update_fields=["is_verified", "verified_at"])

        token.delete()

        log_auth_event(
            request,
            AuthEvent.EVENT_VERIFY_EMAIL_SUCCESS,
            user=user,
        )

        return redirect("https://shikshacom.com/email-verified?status=success")


# =====================================================
# RESEND VERIFICATION EMAIL
# =====================================================

class ResendVerificationEmailView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ResendVerificationRateThrottle]

    def post(self, request):
        email = request.data.get("email", "").strip().lower()

        user = User.objects.filter(email=email).first()
        if not user:
            raise ValidationError("User not found.")

        if user.is_verified:
            raise ValidationError("Email already verified.")

        # 🔥 DELETE OLD TOKENS
        EmailVerificationToken.objects.filter(user=user).delete()

        token = EmailVerificationToken.generate(user)

        verify_link = (
            f"https://api.shikshacom.com/api/accounts/verify-email/?token={token.token}"
        )

        html = f"""
        <h2>Verify your email</h2>
        <p>Click below:</p>
        <a href="{verify_link}">Verify Email</a>
        """

        send_gmail(
            to=user.email,
            subject="Verify your email",
            message_text=f"Click to verify:\n{verify_link}",
            html=html,
        )

        log_auth_event(
            request,
            AuthEvent.EVENT_RESEND_VERIFICATION,
            user=user,
        )

        return Response({"detail": "Verification email resent."})

# =====================================================
# REQUEST TEACHER ROLE
# =====================================================


class RequestTeacherRoleView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request):
        user = request.user

        if user.has_role(Role.TEACHER):
            raise ValidationError("You are already a teacher.")

        teacher_role = Role.objects.get(name=Role.TEACHER)

        if UserRole.objects.filter(user=user, role=teacher_role).exists():
            raise ValidationError("Teacher role already requested.")

        UserRole.objects.create(
            user=user,
            role=teacher_role,
            is_active=False,
        )

        return Response(
            {"detail": "Teacher role request submitted."},
            status=status.HTTP_201_CREATED,
        )


# =====================================================
# APPROVE TEACHER ROLE (Superuser Works Here)
# =====================================================

class ApproveTeacherRoleView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        user_id = request.data.get("user_id")
        if not user_id:
            raise ValidationError("user_id is required.")

        teacher_role = Role.objects.get(name=Role.TEACHER)

        try:
            user_role = UserRole.objects.get(
                user__id=user_id,
                role=teacher_role,
                is_active=False,
            )
        except UserRole.DoesNotExist:
            raise ValidationError("No pending teacher request found.")

        user_role.approve(admin_user=request.user)

        return Response(
            {"detail": "Teacher role approved."},
            status=status.HTTP_200_OK,
        )


# =====================================================
# FORM FILLUP
# =====================================================

class FormFillupView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def _is_teacher(self, user):
        return "TEACHER" in user.get_active_roles()

    def get(self, request):
        user = request.user
        is_teacher = self._is_teacher(user)

        if is_teacher:
            profile = user.profile
            tp = getattr(user, "teacher_profile", None)

            data = {
                "form_type": "teacher",
                "name": profile.full_name or "",
                "email": user.email,
                "phone": profile.phone or "",
                "gender": tp.gender if tp else "",
                "date_of_birth": tp.date_of_birth if tp else None,
                "father_name": tp.father_name if tp else "",
                "father_phone": tp.father_phone if tp else "",
                "mother_name": tp.mother_name if tp else "",
                "mother_phone": tp.mother_phone if tp else "",
                "current_address": tp.current_address if tp else "",
                "permanent_address": tp.permanent_address if tp else "",
                "same_as_current": tp.same_as_current if tp else False,
                "highest_qualification": tp.highest_qualification if tp else "",
                "other_qualification": tp.other_qualification if tp else "",
                "subject_specialization": tp.subject_specialization if tp else "",
                "teaching_experience_years": tp.teaching_experience_years if tp else 0,
                "previous_institution": tp.previous_institution if tp else "",
            }
        else:
            profile = user.profile
            data = {
                "form_type": "student",
                "name": profile.full_name or "",
                "email": user.email,
                "phone": profile.phone or "",
                "date_of_birth": profile.date_of_birth,
                "father_name": profile.father_name or "",
                "father_phone": profile.father_phone or "",
                "mother_name": profile.mother_name or "",
                "guardian": profile.guardian or "",
                "guardian_phone": profile.guardian_phone or "",
                "current_address": profile.current_address or "",
                "permanent_address": profile.permanent_address or "",
                "same_as_current": profile.same_as_current,
            }

        return Response(data)

    def put(self, request):
        user = request.user
        is_teacher = self._is_teacher(user)

        if is_teacher:
            serializer = TeacherFormFillupSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.update(user, serializer.validated_data)
        else:
            profile = user.profile
            serializer = StudentFormFillupSerializer(
                profile, data=request.data, partial=False
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()

        return Response({"detail": "Profile updated successfully."})


# =====================================================
# LOGOUT
# =====================================================

class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        response = Response({"detail": "Logged out."})

        response.delete_cookie("access", domain=".shikshacom.com")
        response.delete_cookie("refresh", domain=".shikshacom.com")

        return response


# =====================================================
# PROFILE COMPLETE PERMISSION
# =====================================================

class IsProfileComplete(BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        return hasattr(request.user, "profile") and request.user.profile.is_complete


# =====================================================
# REFRESH TOKEN
# =====================================================

class RefreshView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token = request.COOKIES.get("refresh")

        if not refresh_token:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            token = RefreshToken(refresh_token)
            user = User.objects.get(id=token["user_id"])

            new_refresh = RefreshToken.for_user(user)

            response = Response({"detail": "refreshed"})

            response.set_cookie(
                key="access",
                value=str(new_refresh.access_token),
                httponly=True,
                secure=True,
                samesite="None",
                domain=".shikshacom.com",
                max_age=600,
            )

            response.set_cookie(
                key="refresh",
                value=str(new_refresh),
                httponly=True,
                secure=True,
                samesite="None",
                domain=".shikshacom.com",
                max_age=60 * 60 * 24 * 7,
            )

            return response

        except (TokenError, User.DoesNotExist):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
