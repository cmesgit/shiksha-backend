import logging
from accounts.email_utils import send_gmail
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated, AllowAny, IsAdminUser
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.contrib.auth import authenticate
from django.shortcuts import redirect
from django.db import transaction

logger = logging.getLogger(__name__)

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
    TeacherListSerializer,
    ChangePasswordSerializer,
)

from .models import TeacherProfile, Profile , TeacherCourseApplication, TeacherSkillApplication

from .permissions import IsEmailVerified

# Indian states and districts data
from .indian_states_data import STATES_WITH_DISTRICTS


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

    def _get_api_base_url(self, request):
        """Return the correct API base URL based on the current host."""
        host = request.get_host()
        if "dev" in host or "localhost" in host or "127.0.0.1" in host:
            return "https://dev.api.shikshacom.com"
        return "https://api.shikshacom.com"

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            user = serializer.save()

            EmailVerificationToken.objects.filter(user=user).delete()
            token = EmailVerificationToken.generate(user)

        base_url = self._get_api_base_url(request)
        verify_link = (
            f"{base_url}/api/accounts/verify-email/?token={token.token}"
        )

        html = f"""
        <h2>Verify your email</h2>
        <p>Click the button below:</p>
        <a href="{verify_link}" style="padding:10px 15px;background:#2563eb;color:white;text-decoration:none;border-radius:5px;">
            Verify Email
        </a>
        """

        try:
            send_gmail(
                to=user.email,
                subject="Verify your email",
                message_text=f"Click to verify:\n{verify_link}",
                html=html,
            )
        except Exception as e:
            logger.error(f"Failed to send verification email to {user.email}: {e}")
            return Response(
                {"detail": "Signup successful, but we couldn't send the verification email. Please use 'Resend Verification' to get your email."},
                status=status.HTTP_201_CREATED,
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

    def _get_frontend_base_url(self, request):
        """Return the correct frontend URL based on the current host."""
        host = request.get_host()
        if "dev" in host:
            return "https://dev.shikshacom.com"
        return "https://shikshacom.com"

    def get(self, request):
        token_value = request.query_params.get("token")
        frontend_url = self._get_frontend_base_url(request)

        if not token_value:
            return redirect(f"{frontend_url}/email-verified?status=failed")

        try:
            token = EmailVerificationToken.objects.select_related("user").get(
                token=token_value,
                expires_at__gt=timezone.now(),
            )
        except EmailVerificationToken.DoesNotExist:
            log_auth_event(request, AuthEvent.EVENT_VERIFY_EMAIL_FAILED)
            return redirect(f"{frontend_url}/email-verified?status=failed")

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

        return redirect(f"{frontend_url}/email-verified?status=success")


# =====================================================
# RESEND VERIFICATION EMAIL
# =====================================================

class ResendVerificationEmailView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ResendVerificationRateThrottle]

    def _get_api_base_url(self, request):
        """Return the correct API base URL based on the current host."""
        host = request.get_host()
        if "dev" in host or "localhost" in host or "127.0.0.1" in host:
            return "https://dev.api.shikshacom.com"
        return "https://api.shikshacom.com"

    def post(self, request):
        email = request.data.get("email", "").strip().lower()

        user = User.objects.filter(email=email).first()
        if not user:
            raise ValidationError("User not found.")

        if user.is_verified:
            raise ValidationError("Email already verified.")

        EmailVerificationToken.objects.filter(user=user).delete()

        token = EmailVerificationToken.generate(user)

        base_url = self._get_api_base_url(request)
        verify_link = (
            f"{base_url}/api/accounts/verify-email/?token={token.token}"
        )

        html = f"""
        <h2>Verify your email</h2>
        <p>Click below:</p>
        <a href="{verify_link}" style="padding:10px 15px;background:#2563eb;color:white;text-decoration:none;border-radius:5px;">
            Verify Email
        </a>
        """

        try:
            send_gmail(
                to=user.email,
                subject="Verify your email",
                message_text=f"Click to verify:\n{verify_link}",
                html=html,
            )
        except Exception as e:
            logger.error(f"Failed to resend verification email to {user.email}: {e}")
            raise ValidationError("Failed to send verification email. Please try again later.")

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
# FORM FILLUP (REVAMPED)
# =====================================================

class FormFillupView(APIView):
    permission_classes = [IsAuthenticated, IsEmailVerified]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _is_teacher(self, user):
        return "TEACHER" in user.get_active_roles()

    def get(self, request):
        user = request.user
        is_teacher = self._is_teacher(user)
        profile = user.profile

        if is_teacher:
            tp = getattr(user, "teacher_profile", None)

            data = {
                "form_type": "teacher",
                "email": user.email,
                "username": user.username,

                # Personal info (from Profile)
                "first_name": profile.first_name or "",
                "last_name": profile.last_name or "",
                "phone": profile.phone or "",
                "gender": profile.gender or "",
                "date_of_birth": profile.date_of_birth,
                "profile_photo": profile.profile_photo.url if profile.profile_photo else None,

                # Address (from Profile)
                "state": profile.state or "",
                "district": profile.district or "",
                "city_town": profile.city_town or "",
                "pin_code": profile.pin_code or "",

                # Educational Qualifications
                "highest_degree": tp.highest_degree if tp else "",
                "field_of_study": tp.field_of_study if tp else "",
                "year_of_completion": tp.year_of_completion if tp else None,
                "teaching_certifications": tp.teaching_certifications if tp else [],
                "qualification_certificate": (
                    tp.qualification_certificate.url
                    if tp and tp.qualification_certificate else None
                ),

                # Teaching Experience
                "experience_range": tp.experience_range if tp else "",
                "employment_status": tp.employment_status if tp else "",
                "currently_employed": tp.currently_employed if tp else False,
                "current_institution": tp.current_institution if tp else "",
                "current_position": tp.current_position if tp else "",

                # Verification Documents
                "govt_id_type": tp.govt_id_type if tp else "",
                "id_number": tp.id_number if tp else "",
                "id_proof_front": (
                    tp.id_proof_front.url if tp and tp.id_proof_front else None
                ),
                "id_proof_back": (
                    tp.id_proof_back.url if tp and tp.id_proof_back else None
                ),

                # Course Applications
                "course_applications": [
                    {
                        "id": ca.id,
                        "subject": ca.subject,
                        "boards": ca.boards,
                        "classes": ca.classes,
                        "streams": ca.streams,
                    }
                    for ca in (tp.course_applications.all() if tp else [])
                ],

                # Skill Applications
                "skill_applications": [
                    {
                        "id": sa.id,
                        "skill_name": sa.skill_name,
                        "skill_description": sa.skill_description,
                        "skill_related_subject": sa.skill_related_subject,
                        "skill_supporting_file": (
                            sa.supporting_file.url
                            if sa.supporting_file else None
                        ),
                    }
                    for sa in (tp.skill_applications.all() if tp else [])
                ],

            }
        else:
            data = {
                "form_type": "student",
                "email": user.email,
                "username": user.username,

                # Personal info
                "first_name": profile.first_name or "",
                "last_name": profile.last_name or "",
                "phone": profile.phone or "",
                "gender": profile.gender or "",
                "date_of_birth": profile.date_of_birth,
                "profile_photo": profile.profile_photo.url if profile.profile_photo else None,

                # Address
                "state": profile.state or "",
                "district": profile.district or "",
                "city_town": profile.city_town or "",
                "pin_code": profile.pin_code or "",

                # Parent/Guardian
                "father_name": profile.father_name or "",
                "father_phone": profile.father_phone or "",
                "mother_name": profile.mother_name or "",
                "mother_phone": profile.mother_phone or "",
                "guardian_name": profile.guardian_name or "",
                "guardian_phone": profile.guardian_phone or "",
                "parent_guardian_email": profile.parent_guardian_email or "",

                # Academic Info
                "currently_studying": profile.currently_studying or "",
                "current_class": profile.current_class or "",
                "stream": profile.stream or "",
                "board": profile.board or "",
                "board_other": profile.board_other or "",
                "school_name": profile.school_name or "",
                "academic_year": profile.academic_year or "",
                "highest_education": profile.highest_education or "",
                "reason_not_studying": profile.reason_not_studying or "",
            }

        return Response(data)

    def put(self, request):
        user = request.user
        is_teacher = self._is_teacher(user)

        if is_teacher:
            serializer = TeacherFormFillupSerializer(
                data=request.data, context={"request": request}
            )
            serializer.is_valid(raise_exception=True)
            serializer.update(user, serializer.validated_data)
        else:
            profile = user.profile
            serializer = StudentFormFillupSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.update(profile, serializer.validated_data)

        return Response({"detail": "Profile updated successfully."})


# =====================================================
# TEACHER PROFILE API
# =====================================================

class TeacherProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        profile = user.profile
        tp = getattr(user, "teacher_profile", None)

        # Active courses & subjects via SubjectTeacher
        from courses.models import SubjectTeacher
        assignments = SubjectTeacher.objects.filter(
            teacher=user
        ).select_related("subject__course")

        active_courses = {}
        subjects = []
        for a in assignments:
            course = a.subject.course
            course_key = str(course.id)
            if course_key not in active_courses:
                active_courses[course_key] = {
                    "id": str(course.id),
                    "title": str(course),
                    "subjects": [],
                }
            active_courses[course_key]["subjects"].append(a.subject.name)

            subjects.append({
                "name": a.subject.name,
                "course": str(course),
            })

        data = {
            "name": f"{profile.first_name} {profile.last_name}".strip(),
            "gender": profile.gender or "",
            "photo": profile.profile_photo.url if profile.profile_photo else None,
            "bio": tp.bio if tp else "",
            "highest_degree": tp.get_highest_degree_display() if tp and tp.highest_degree else "",
            "field_of_study": tp.field_of_study if tp else "",
            "teaching_certifications": tp.teaching_certifications if tp else [],
            "experience_range": tp.get_experience_range_display() if tp and tp.experience_range else "",
            "employment_status": tp.get_employment_status_display() if tp and tp.employment_status else "",
            "rating": float(tp.rating) if tp and tp.rating else None,
            "is_approved": tp.is_approved if tp else False,
            "active_courses": list(active_courses.values()),
            "subjects": subjects,
            "course_applications": [
                {
                    "subject": ca.get_subject_display(),
                    "boards": ca.boards,
                    "classes": ca.classes,
                    "streams": ca.streams,
                }
                for ca in (tp.course_applications.all() if tp else [])
            ],
            "skill_applications": [
                {
                    "skill_name": sa.skill_name,
                    "skill_description": sa.skill_description,
                    "skill_related_subject": sa.get_skill_related_subject_display(),
                }
                for sa in (tp.skill_applications.all() if tp else [])
            ],
        }

        return Response(data)


# =====================================================
# STATES & DISTRICTS API
# =====================================================

class StatesListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        """Return list of all Indian states/UTs."""
        states = [{"name": s["name"]} for s in STATES_WITH_DISTRICTS]
        return Response(states)


class DistrictsListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, state_name):
        """Return districts for a given state."""
        for s in STATES_WITH_DISTRICTS:
            if s["name"].lower() == state_name.lower():
                return Response(s["districts"])
        return Response([], status=status.HTTP_404_NOT_FOUND)


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


# =====================================================
# TEACHER LIST (for private session request form)
# =====================================================

class TeacherListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = TeacherProfile.objects.filter(
            is_approved=True,
            user__user_roles__role__name="TEACHER",
            user__user_roles__is_active=True,
        ).select_related("user", "user__profile").distinct()

        subject = request.query_params.get("subject", "").strip()
        if subject:
            qs = qs.filter(subject_specialization__icontains=subject)

        data = []
        for tp in qs:
            profile = getattr(tp.user, "profile", None)
            name = ""
            if profile:
                if profile.first_name:
                    name = f"{profile.first_name} {profile.last_name}".strip()
                elif profile.full_name:
                    name = profile.full_name
            if not name:
                name = tp.user.get_full_name() or tp.user.username

            avatar = profile.avatar_value() if profile else None

            data.append({
                "id": str(tp.user.id),
                "name": name,
                "subject": tp.subject_specialization or tp.subject or "",
                "qualification": tp.qualification or "",
                "rating": float(tp.rating) if tp.rating else None,
                "avatar": avatar,
            })

        return Response(data)


# =====================================================
# VALIDATE STUDENT ID (for group session form)
# =====================================================

class ValidateStudentIdView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, student_id):
        try:
            profile = Profile.objects.select_related("user").get(
                student_id=student_id
            )
        except Profile.DoesNotExist:
            return Response({
                "valid": False,
                "name": None,
                "user_id": None,
                "student_id": student_id,
            })

        if not profile.user.has_role("STUDENT"):
            return Response({
                "valid": False,
                "name": None,
                "user_id": None,
                "student_id": student_id,
            })

        name = ""
        if profile.first_name:
            name = f"{profile.first_name} {profile.last_name}".strip()
        elif profile.full_name:
            name = profile.full_name
        else:
            name = profile.user.username

        return Response({
            "valid": True,
            "name": name,
            "user_id": str(profile.user.id),
            "student_id": student_id,
        })

# =====================================================
# CHANGE PASSWORD
# =====================================================

class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        old_password = serializer.validated_data["old_password"]
        new_password = serializer.validated_data["new_password"]

        if not user.check_password(old_password):
            raise ValidationError({"old_password": "Old password is incorrect."})

        user.set_password(new_password)
        user.save()

        return Response({"detail": "Password changed successfully."})
