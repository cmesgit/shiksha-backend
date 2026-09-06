"""
accounts/registration.py  ·  Account-first signup

    POST /api/accounts/register/   { email, password, terms_accepted }

Three fields. No role, no teacher_type, no profiles[]. Everyone who registers
is a learner; teaching is a capability added later from inside the product
(see design_handoff_account_model/BUILD_GUIDE.md, Phase 2).

WHY THIS EXISTS ALONGSIDE SignupView
────────────────────────────────────
`SignupSerializer` carries eight branching cases, and six of them exist only
because signup can be re-entered by an account that already exists — they
re-prove the account password as ownership proof before adding an identity.
Once identity-adding moves inside the product the caller is already
authenticated and there is nothing to prove, so those cases become dead.

This module is deliberately NOT a refactor of that serializer. It is a second,
minimal path added beside it, so the live signup route keeps working untouched
while the new one is built and verified. Phase 8 deletes the old one; until
then both are real.

Consequence worth knowing: the verification-email body below is duplicated
from `SignupView`. That duplication is intentional and temporary — deduping it
would mean editing the live signup path in a phase whose whole point is not to.
It dies with the old view in Phase 8.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.contrib.auth.password_validation import validate_password

from rest_framework import serializers, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import ValidationError

from .email_utils import send_gmail
from .models import (
    CURRENT_TERMS_VERSION,
    EmailVerificationToken,
    LearnerProfile,
    Role,
    User,
    UserRole,
)
from .signup_serializer import _generate_unique_username
from .throttles import SignupRateThrottle

logger = logging.getLogger(__name__)


def send_verification_email(user, verify_link):
    """Send the verification mail. Raises on transport failure — the caller
    decides whether that is fatal (it is not; see RegisterView)."""
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


class RegisterSerializer(serializers.Serializer):
    """email + password + terms. That is the whole contract."""

    email          = serializers.EmailField()
    password       = serializers.CharField(write_only=True)
    terms_accepted = serializers.BooleanField(default=False)

    def validate_email(self, value):
        return (value or "").strip().lower()

    def validate(self, data):
        email = data["email"]

        if not data.get("terms_accepted"):
            raise ValidationError(
                {"terms_accepted": "You must accept the Terms of Use to continue.",
                 "code": "terms_required"}
            )

        # Password strength is checked here and not on the field, so the error
        # surfaces with the same shape as every other cross-field failure.
        validate_password(data["password"])

        existing = User.objects.filter(email__iexact=email).first()
        if existing:
            if existing.is_verified:
                raise ValidationError(
                    {"email": "An account with this email already exists. Log in instead.",
                     "code": "email_taken"}
                )
            # Unverified and still inside the token window: don't silently
            # replace it (that would invalidate a link already in their inbox)
            # and don't create a duplicate. Point them at Resend.
            raise ValidationError(
                {"email": "This email is already registered but not verified. "
                          "Check your inbox, or request a new verification email.",
                 "code": "email_unverified"}
            )

        return data

    def create(self, validated_data):
        email = validated_data["email"]

        user = User.objects.create_user(
            email    = email,
            username = _generate_unique_username(email),
            password = validated_data["password"],
        )
        user.is_verified            = False
        user.accepted_terms_version = CURRENT_TERMS_VERSION
        user.terms_accepted_at      = timezone.now()
        user.save(update_fields=[
            "is_verified", "accepted_terms_version", "terms_accepted_at",
        ])

        # One SELF profile, marked default. `_ensure_default_profile` in
        # auth_flow is only a backstop for accounts that somehow have none —
        # every creation path is expected to make its own.
        LearnerProfile.objects.create(
            account      = user,
            display_name = user.username,
            relationship = LearnerProfile.RELATIONSHIP_SELF,
            is_default   = True,
        )

        student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
        UserRole.objects.get_or_create(
            user=user,
            role=student_role,
            defaults={
                "is_active":   True,
                "is_primary":  True,
                "approved_at": timezone.now(),
            },
        )

        return user


class RegisterView(APIView):
    """Create an account. Learner-only; teaching is added later."""

    permission_classes = [AllowAny]
    throttle_classes   = [SignupRateThrottle]

    def post(self, request):
        # Free the email if a previous unverified signup was abandoned. Matches
        # the 24h token expiry, so a real duplicate is still rejected by the
        # serializer. Same rule SignupView applies.
        email = (request.data.get("email") or "").strip().lower()
        if email:
            User.objects.filter(
                email__iexact=email,
                is_verified=False,
                date_joined__lt=timezone.now() - timedelta(hours=24),
            ).delete()

        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            user = serializer.save()
            EmailVerificationToken.objects.filter(user=user).delete()
            token = EmailVerificationToken.generate(user)

        # Import here: accounts.views imports this module's siblings, and a
        # module-level import of it would close an import cycle.
        from .views import _api_base_url

        verify_link = (
            f"{_api_base_url(request)}/api/accounts/verify-email/?token={token.token}"
        )

        try:
            send_verification_email(user, verify_link)
        except Exception as e:
            # The account is real and the token is valid — a mail outage must
            # not lose the signup. Resend is a separate endpoint.
            logger.error(f"Failed to send verification email to {user.email}: {e}")
            return Response(
                {"detail": "Account created, but we couldn't send the verification "
                           "email. Please use 'Resend Verification'.",
                 "email_sent": False},
                status=status.HTTP_201_CREATED,
            )

        return Response(
            {"detail": "Account created. Check your email to verify and sign in.",
             "email_sent": True},
            status=status.HTTP_201_CREATED,
        )
