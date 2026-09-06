"""
accounts/oauth_views.py  ·  Google sign-in endpoint

    POST /api/accounts/oauth/google/   { credential, terms_accepted? }

Returns the SAME body `LoginView` returns, so the frontend's existing routing
(`context` → learner / account / teacher, the profile picker, the PIN prompt)
works unchanged. The only additions are `needs_password` and `created`.

THREE WAYS THIS RESOLVES
────────────────────────
1. Known `sub`            → sign in. The normal case.
2. Unknown `sub`, VERIFIED account on that email → link and sign in.
3. Neither                → 404 `no_account` carrying the email and name, so
                            the frontend can show a one-checkbox consent step
                            and POST straight back with `terms_accepted`.

WHY LINKING ONLY EVER TOUCHES A *VERIFIED* ACCOUNT
──────────────────────────────────────────────────
Linking by email to an UNVERIFIED account is an account-takeover vector, and
a cheap one:

    1. Attacker registers alice@gmail.com with a password they choose.
       Nothing stops this — registration does not require proving the mailbox.
    2. The account sits unverified. The attacker cannot log into it
       (`LoginView` refuses on `is_verified`), so it looks harmless.
    3. Real Alice clicks "Continue with Google".
    4. If we linked by email, Alice is now signed into the ATTACKER'S account
       — an account whose password the attacker knows and can log in with the
       moment Google verification flips `is_verified` to True.

So an unverified shell is never linked to. It is instead RECLAIMED: deleted,
and a fresh Google-owned account created in its place. That is safe precisely
because an unverified account can never have been logged into, so it holds no
user-generated content — only the email, and Google has just proved that the
person in front of us owns it. Whoever controls the mailbox wins the address,
which is the correct outcome.

ON `needs_password`
───────────────────
A Google-created account has `set_unusable_password()`, so the three
password-gated operations (PIN reset, profile deletion, set-password) are
unreachable for it. `needs_password` reports that so the frontend can prompt
at a sensible moment. It is deliberately INFORMATIONAL, not a hard gate —
the original plan forced a password immediately after the first Google
sign-in, which was written before teacher mode moved to a PIN. With that
change the password guards only rare, destructive actions, and interrupting
every new Google user for something they may never do defeats the point of
one-click sign-in. Prompt at the point of need instead.
"""
import logging

from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from .auth_flow import issue_login_session, set_auth_cookies
from .models import GoogleIdentity, LearnerProfile, Role, User, UserRole, CURRENT_TERMS_VERSION
from .oauth_google import GoogleAuthError, verify_google_id_token
from .signup_serializer import _generate_unique_username
from .throttles import OAuthRateThrottle

logger = logging.getLogger(__name__)


def _google_enabled():
    from global_settings.models import GlobalSettings
    return GlobalSettings.load().google_oauth_enabled


def _create_account_from_google(claims):
    """A brand-new account owned by a Google identity.

    Created ALREADY VERIFIED: `verify_google_id_token` refuses anything whose
    `email_verified` claim is false, so by this point Google has asserted
    ownership of the mailbox — which is exactly what our own verification mail
    would have established, only stronger.
    """
    email = claims["email"]

    user = User.objects.create_user(
        email=email,
        username=_generate_unique_username(email),
        password=None,
    )
    user.set_unusable_password()
    user.is_verified            = True
    user.verified_at            = timezone.now()
    user.accepted_terms_version = CURRENT_TERMS_VERSION
    user.terms_accepted_at      = timezone.now()
    user.save(update_fields=[
        "password", "is_verified", "verified_at",
        "accepted_terms_version", "terms_accepted_at",
    ])

    LearnerProfile.objects.create(
        account      = user,
        display_name = claims.get("name") or user.username,
        relationship = LearnerProfile.RELATIONSHIP_SELF,
        is_default   = True,
    )

    student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
    UserRole.objects.get_or_create(
        user=user, role=student_role,
        defaults={"is_active": True, "is_primary": True,
                  "approved_at": timezone.now()},
    )
    return user


class GoogleSignInView(APIView):
    permission_classes = [AllowAny]
    throttle_classes   = [OAuthRateThrottle]

    def post(self, request):
        if not _google_enabled():
            return Response(
                {"detail": "Google sign-in is not available.", "code": "disabled"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            claims = verify_google_id_token(request.data.get("credential"))
        except GoogleAuthError as e:
            code = status.HTTP_503_SERVICE_UNAVAILABLE if e.code == "not_configured" \
                else status.HTTP_400_BAD_REQUEST
            return Response({"detail": e.detail, "code": e.code}, status=code)

        created = False

        with transaction.atomic():
            identity = (
                GoogleIdentity.objects
                .select_related("user")
                .filter(sub=claims["sub"])
                .first()
            )

            if identity:
                user = identity.user
            else:
                user = User.objects.filter(
                    email__iexact=claims["email"], is_verified=True,
                ).first()

                if user is None:
                    if not request.data.get("terms_accepted"):
                        # No account and no consent yet — hand the frontend
                        # what it needs to render the one-checkbox step.
                        return Response(
                            {"code": "no_account",
                             "email": claims["email"],
                             "name": claims.get("name", ""),
                             "detail": "No account exists for this Google address."},
                            status=status.HTTP_404_NOT_FOUND,
                        )
                    # Reclaim any unverified shell on this address first — see
                    # the module docstring for why this is safe and necessary.
                    User.objects.filter(
                        email__iexact=claims["email"], is_verified=False,
                    ).delete()
                    user = _create_account_from_google(claims)
                    created = True

                identity = GoogleIdentity.objects.create(
                    user=user, sub=claims["sub"], email_at_link=claims["email"],
                )

            identity.last_login_at = timezone.now()
            identity.save(update_fields=["last_login_at"])

        body, refresh = issue_login_session(user, request)
        body["needs_password"] = not user.has_usable_password()
        body["created"] = created

        return set_auth_cookies(
            Response(body, status=status.HTTP_200_OK), refresh
        )
