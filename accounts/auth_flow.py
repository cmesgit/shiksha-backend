"""
accounts/auth_flow.py
─────────────────────
Two-step login for the lighter multi-profile model:

  Step 1  POST /api/accounts/login/        {email, password}
          -> authenticates the ACCOUNT, sets an account-scoped cookie,
             returns the list of learner profiles + whether a teacher
             identity exists. No learner endpoints work yet.

  Step 2  POST /api/accounts/profiles/select/   {profile_id, pin?}
          -> verifies the profile belongs to the account, checks the PIN
             if one is set, then issues a LEARNER token carrying the
             `active_profile` claim. This is also the "switch profile" call.

  Teacher POST /api/accounts/context/teacher/
          -> for an approved teacher, issues a token with context="teacher"
             (no active_profile). The teach<->learn switch is just another
             call to this or to profiles/select/ — no re-login.

Read the active profile anywhere with `get_active_profile(request)`.

These views are written to drop into your existing accounts app; they
reuse your cookie names ("access"/"refresh"), settings.COOKIE_DOMAIN, and
the same cookie flags as your current LoginView. Wire them in urls.py and
retire the JWT-issuing tail of the old LoginView.
"""
from django.conf import settings
from django.contrib.auth import authenticate

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework_simplejwt.tokens import RefreshToken

from .models import LearnerProfile, Role

ACCESS_MAX_AGE = 60 * 60                 # 1 hour
REFRESH_MAX_AGE = 60 * 60 * 24 * 7       # 1 week

CTX_ACCOUNT = "account"
CTX_LEARNER = "learner"
CTX_TEACHER = "teacher"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def build_tokens(user, *, context, profile=None):
    """Issue a refresh/access pair stamped with the active context."""
    refresh = RefreshToken.for_user(user)
    refresh["context"] = context
    if profile is not None:
        refresh["active_profile"] = str(profile.id)
    return refresh


def set_auth_cookies(response, refresh):
    for key in ("access", "refresh"):
        response.delete_cookie(key, domain=settings.COOKIE_DOMAIN)

    response.set_cookie(
        key="access",
        value=str(refresh.access_token),
        httponly=True, secure=True, samesite="None",
        domain=settings.COOKIE_DOMAIN, max_age=ACCESS_MAX_AGE,
    )
    response.set_cookie(
        key="refresh",
        value=str(refresh),
        httponly=True, secure=True, samesite="None",
        domain=settings.COOKIE_DOMAIN, max_age=REFRESH_MAX_AGE,
    )
    return response


def get_active_profile(request):
    """
    Resolve the LearnerProfile from the validated token's `active_profile`
    claim. Returns None if the request is in account/teacher context.
    """
    token = getattr(request, "auth", None)
    if token is None:
        return None
    pid = token.get("active_profile")
    if not pid:
        return None
    return (
        LearnerProfile.objects
        .filter(id=pid, account=request.user, is_active=True)
        .first()
    )


def serialize_profile_card(p):
    return {
        "id": str(p.id),
        "display_name": p.display_name,
        "relationship": p.relationship,
        "is_default": p.is_default,
        "requires_pin": p.has_pin(),
        "avatar_type": p.avatar_type(),
        "avatar": p.avatar_value(),
        "profile_complete": p.is_complete,
    }


def _ensure_default_profile(user):
    """
    Auto-create a SELF LearnerProfile for accounts that don't have one yet.
    Runs inside LoginView so every user who logs in gets at least one profile
    to pick from. Safe to call multiple times — only creates if missing.
    """
    profiles = list(user.learner_profiles.filter(is_active=True))
    if profiles:
        return profiles

    # Derive a sensible default display name from the account email.
    display_name = user.email.split("@")[0]

    lp = LearnerProfile.objects.create(
        account=user,
        display_name=display_name,
        relationship=LearnerProfile.RELATIONSHIP_SELF,
        is_default=True,
    )
    return [lp]


# ---------------------------------------------------------------------------
# Step 1 — account login
# ---------------------------------------------------------------------------

class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password")
        if not email or not password:
            raise ValidationError("Email and password are required.")

        user = authenticate(request, email=email, password=password)
        if not user:
            raise ValidationError("Invalid credentials.")
        if not user.is_verified:
            raise ValidationError("Email not verified.")

        # Auto-create a default SELF profile if the account has none yet.
        profiles = _ensure_default_profile(user)

        teacher = getattr(user, "teacher_profile", None)
        has_teacher = bool(teacher and teacher.is_approved)

        refresh = build_tokens(user, context=CTX_ACCOUNT)
        body = {
            "context": CTX_ACCOUNT,
            "profiles": [serialize_profile_card(p) for p in profiles],
            "teacher": {
                "type": teacher.teacher_type,
                "tier": teacher.tier,
            } if has_teacher else None,
        }
        return set_auth_cookies(Response(body, status=status.HTTP_200_OK), refresh)


# ---------------------------------------------------------------------------
# Step 2 — pick / switch a learner profile
# ---------------------------------------------------------------------------

class ProfileSelectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile_id = request.data.get("profile_id")
        pin = request.data.get("pin")
        if not profile_id:
            raise ValidationError("profile_id is required.")

        profile = (
            LearnerProfile.objects
            .filter(id=profile_id, account=request.user, is_active=True)
            .first()
        )
        if not profile:
            raise PermissionDenied("Profile not found for this account.")

        if profile.has_pin() and not profile.check_pin(pin or ""):
            raise ValidationError({"pin": "Incorrect PIN."})

        refresh = build_tokens(request.user, context=CTX_LEARNER, profile=profile)
        body = {"context": CTX_LEARNER, "profile": serialize_profile_card(profile)}
        return set_auth_cookies(Response(body, status=status.HTTP_200_OK), refresh)


# ---------------------------------------------------------------------------
# Teacher context (teach <-> learn switch, no re-login)
# ---------------------------------------------------------------------------

class TeacherContextView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        teacher = getattr(request.user, "teacher_profile", None)
        if not (teacher and teacher.is_approved and request.user.has_role(Role.TEACHER)):
            raise PermissionDenied("No approved teacher identity on this account.")

        refresh = build_tokens(request.user, context=CTX_TEACHER)
        body = {
            "context": CTX_TEACHER,
            "teacher": {"type": teacher.teacher_type, "tier": teacher.tier},
        }
        return set_auth_cookies(Response(body, status=status.HTTP_200_OK), refresh)


# ---------------------------------------------------------------------------
# Manage a profile's switch-PIN
# ---------------------------------------------------------------------------

class ProfilePinView(APIView):
    """Set or clear the PIN on one of the account's profiles."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile_id = request.data.get("profile_id")
        new_pin = request.data.get("pin")  # falsy clears it

        profile = (
            LearnerProfile.objects
            .filter(id=profile_id, account=request.user)
            .first()
        )
        if not profile:
            raise PermissionDenied("Profile not found for this account.")

        if new_pin and (not str(new_pin).isdigit() or not (4 <= len(str(new_pin)) <= 6)):
            raise ValidationError({"pin": "PIN must be 4-6 digits."})

        profile.set_pin(str(new_pin) if new_pin else "")
        profile.save(update_fields=["pin"])
        return Response({"profile_id": str(profile.id), "requires_pin": profile.has_pin()})


# ---------------------------------------------------------------------------
# Context-aware /me/  (reload-safe; replaces the old MeView)
# ---------------------------------------------------------------------------

# Fields the existing frontend (Enroll.jsx, dashboards) read off `profile`.
_LEGACY_PROFILE_FIELDS = [
    "first_name", "last_name", "full_name", "phone",
    "current_class", "board", "school_name",
    "father_name", "mother_name", "guardian_name",
]


def _legacy_profile_dict(profile):
    if profile is None:
        return None
    data = {f: getattr(profile, f, "") for f in _LEGACY_PROFILE_FIELDS}
    data["avatar_type"] = profile.avatar_type()
    data["avatar"] = profile.avatar_value()
    return data


class MeView(APIView):
    """
    Returns the account identity plus the ACTIVE context resolved from the
    JWT, so a page reload restores which profile / teacher mode is live.

    Point `me/` at this view in urls.py (replacing the old MeView).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        token = getattr(request, "auth", None)
        context = token.get("context") if token else None

        active = get_active_profile(request)
        profiles = list(user.learner_profiles.filter(is_active=True))
        teacher = getattr(user, "teacher_profile", None)
        has_teacher = bool(teacher and teacher.is_approved)

        return Response({
            "id": str(user.id),
            "email": user.email,
            "username": user.username,
            "context": context,
            "roles": user.get_active_roles(),
            "active_profile": serialize_profile_card(active) if active else None,
            "profiles": [serialize_profile_card(p) for p in profiles],
            "teacher": {
                "type": teacher.teacher_type,
                "tier": teacher.tier,
            } if has_teacher else None,
            # Legacy shape for components that still read `user.profile`.
            "profile": _legacy_profile_dict(active),
            "profile_complete": active.is_complete if active else False,
        })
# ---------------------------------------------------------------------------
# Learner Profile CRUD  (append to auth_flow.py)
# ---------------------------------------------------------------------------
# GET    /api/accounts/profiles/          — list all active profiles
# POST   /api/accounts/profiles/          — create a new profile
# PATCH  /api/accounts/profiles/<id>/     — update a profile
# DELETE /api/accounts/profiles/<id>/     — deactivate a profile

from rest_framework.parsers import MultiPartParser, FormParser, JSONParser


class ProfileListCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        profiles = request.user.learner_profiles.filter(is_active=True)
        return Response([serialize_profile_card(p) for p in profiles])

    def post(self, request):
        data = request.data

        display_name = (data.get("display_name") or "").strip()
        if not display_name:
            raise ValidationError({"display_name": "Display name is required."})
        if len(display_name) > 100:
            raise ValidationError({"display_name": "Max 100 characters."})

        relationship = data.get("relationship", LearnerProfile.RELATIONSHIP_DEPENDENT)
        if relationship not in (LearnerProfile.RELATIONSHIP_SELF, LearnerProfile.RELATIONSHIP_DEPENDENT):
            raise ValidationError({"relationship": "Must be SELF or DEPENDENT."})

        # Only one SELF profile per account
        if relationship == LearnerProfile.RELATIONSHIP_SELF:
            if request.user.learner_profiles.filter(
                relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
            ).exists():
                raise ValidationError({"relationship": "An account can only have one SELF profile."})

        # Cap at 5 active profiles
        if request.user.learner_profiles.filter(is_active=True).count() >= 5:
            raise ValidationError("Maximum of 5 profiles per account.")

        pin = data.get("pin", "")
        if pin and (not str(pin).isdigit() or not (4 <= len(str(pin)) <= 6)):
            raise ValidationError({"pin": "PIN must be 4-6 digits."})

        profile = LearnerProfile(
            account=request.user,
            display_name=display_name,
            relationship=relationship,
            is_default=not request.user.learner_profiles.filter(is_active=True).exists(),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
        )
        if pin:
            profile.set_pin(str(pin))

        if "avatar_emoji" in data:
            profile.avatar_emoji = data["avatar_emoji"]
        if "avatar_image" in request.FILES:
            profile.avatar_image = request.FILES["avatar_image"]

        profile.save()
        return Response(serialize_profile_card(profile), status=status.HTTP_201_CREATED)


class ProfileDetailView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _get_profile(self, request, profile_id):
        profile = (
            LearnerProfile.objects
            .filter(id=profile_id, account=request.user, is_active=True)
            .first()
        )
        if not profile:
            raise ValidationError("Profile not found.")
        return profile

    def patch(self, request, profile_id):
        profile = self._get_profile(request, profile_id)
        data = request.data

        if "display_name" in data:
            display_name = data["display_name"].strip()
            if not display_name:
                raise ValidationError({"display_name": "Display name cannot be empty."})
            profile.display_name = display_name

        if "first_name" in data:
            profile.first_name = data["first_name"]
        if "last_name" in data:
            profile.last_name = data["last_name"]

        if "pin" in data:
            new_pin = data["pin"]
            if new_pin and (not str(new_pin).isdigit() or not (4 <= len(str(new_pin)) <= 6)):
                raise ValidationError({"pin": "PIN must be 4-6 digits."})
            profile.set_pin(str(new_pin) if new_pin else "")

        if "avatar_emoji" in data:
            profile.avatar_emoji = data["avatar_emoji"]
            profile.avatar_image = None  # clear image if emoji set

        if "avatar_image" in request.FILES:
            profile.avatar_image = request.FILES["avatar_image"]
            profile.avatar_emoji = ""   # clear emoji if image set

        profile.save()
        return Response(serialize_profile_card(profile))

    def delete(self, request, profile_id):
        profile = self._get_profile(request, profile_id)

        # Must keep at least one profile
        active_count = request.user.learner_profiles.filter(is_active=True).count()
        if active_count <= 1:
            raise ValidationError("Cannot delete the only profile on this account.")

        # If deleting the default, promote the next oldest
        if profile.is_default:
            next_profile = (
                request.user.learner_profiles
                .filter(is_active=True)
                .exclude(id=profile.id)
                .order_by("created_at")
                .first()
            )
            if next_profile:
                next_profile.is_default = True
                next_profile.save(update_fields=["is_default"])

        profile.is_active = False
        profile.is_default = False
        profile.save(update_fields=["is_active", "is_default"])

        return Response({"detail": "Profile removed."}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Email-first profile lookup (unauthenticated — display names only)
# ---------------------------------------------------------------------------

class ProfileEmailLookupView(APIView):
    """
    POST { email } → { profiles: [{display_name, relationship}], has_teacher }

    Lets the frontend show a profile-picker BEFORE the password is entered
    (matching the design's li-s-profiles step).  Returns 200 with empty
    profiles for unknown emails to avoid leaking registration status.
    Only display names are returned — no IDs or sensitive fields.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from .models import User
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"profiles": [], "has_teacher": False})

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response({"profiles": [], "has_teacher": False})

        profiles = list(
            user.learner_profiles
            .filter(is_active=True)
            .order_by("-is_default", "created_at")
        )
        teacher = getattr(user, "teacher_profile", None)
        return Response({
            "profiles": [
                {"display_name": p.display_name, "relationship": p.relationship}
                for p in profiles
            ],
            "has_teacher": bool(teacher and teacher.is_approved),
        })


# ---------------------------------------------------------------------------
# Email state check (unauthenticated — for signup gate logic)
# ---------------------------------------------------------------------------

class EmailCheckView(APIView):
    """
    POST { email } → { exists, has_student, has_teacher, is_verified }

    Called by Signup.jsx at STEP_BASIC before the user proceeds.
    Lets the frontend decide:
      - New email           → normal signup
      - has_student only    → offer to add teacher identity (verify password inline)
      - has_teacher only    → offer to add learner profiles (verify password inline)
      - has_student + teacher → both exist, block and redirect to login
      - has_student + student signup → block ("already has learner profiles")
      - has_teacher + teacher signup → block ("already has teacher account")

    Always returns 200 — unknown emails return exists=False.
    Does NOT expose passwords, tokens, or profile IDs.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from .models import User, Role
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({
                "exists": False,
                "has_student": False,
                "has_teacher": False,
                "is_verified": False,
            })

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response({
                "exists": False,
                "has_student": False,
                "has_teacher": False,
                "is_verified": False,
            })

        return Response({
            "exists":      True,
            "has_student": user.has_role(Role.STUDENT),
            "has_teacher": hasattr(user, "teacher_profile"),
            "is_verified": user.is_verified,
        })
