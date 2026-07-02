"""
accounts/auth_flow.py  ·  REFACTORED — single-password model
─────────────────────────────────────────────────────────────
ONE email · ONE password · profiles with PIN.

  Step 1  POST /api/accounts/login/              { email, password }
          → authenticates account, issues account-scoped cookie
          → returns profiles list + whether teacher identity exists
          → auto-selects if single profile + no teacher identity

  Step 2A POST /api/accounts/profiles/select/    { profile_id, pin? }
          → verifies profile belongs to account, checks PIN if set
          → issues LEARNER token with active_profile claim

  Step 2B POST /api/accounts/context/teacher/    { password }
          → verifies ACCOUNT password (same password as login)
          → issues TEACHER token

  Switch  Call Step 2A or 2B at any time — no re-login needed.
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

ACCESS_MAX_AGE  = 60 * 60            # 1 hour
REFRESH_MAX_AGE = 60 * 60 * 24 * 7  # 1 week

CTX_ACCOUNT = "account"
CTX_LEARNER = "learner"
CTX_TEACHER = "teacher"


# ─────────────────────────────────────────────────────────────────────────────
# Token & cookie helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_tokens(user, *, context, profile=None, active_track=None):
    refresh = RefreshToken.for_user(user)
    refresh["context"] = context
    if profile is not None:
        refresh["active_profile"] = str(profile.id)
    if active_track is not None:
        refresh["active_track"] = active_track
    return refresh


def set_auth_cookies(response, refresh):
    for key in ("access", "refresh"):
        response.delete_cookie(key, domain=settings.COOKIE_DOMAIN)
    response.set_cookie(
        key="access", value=str(refresh.access_token),
        httponly=True, secure=True, samesite="None",
        domain=settings.COOKIE_DOMAIN, max_age=ACCESS_MAX_AGE,
    )
    response.set_cookie(
        key="refresh", value=str(refresh),
        httponly=True, secure=True, samesite="None",
        domain=settings.COOKIE_DOMAIN, max_age=REFRESH_MAX_AGE,
    )
    return response


def get_active_profile(request):
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
        "id":               str(p.id),
        "display_name":     p.display_name,
        "relationship":     p.relationship,
        "is_default":       p.is_default,
        "requires_pin":     p.has_pin(),
        "avatar_type":      p.avatar_type(),
        "avatar":           p.avatar_value(),
        "profile_complete": p.is_complete,
        # Personal data — editable from Manage profile (all optional). Feeds the
        # faculty application form, which reads the same fields off this profile.
        "first_name":       p.first_name or "",
        "last_name":        p.last_name or "",
        "phone":            p.phone or "",
        "gender":           p.gender or "",
        "date_of_birth":    p.date_of_birth.isoformat() if p.date_of_birth else "",
        "state":            p.state or "",
        "district":         p.district or "",
        "city_town":        p.city_town or "",
        "pin_code":         p.pin_code or "",
        "profile_photo":    p.profile_photo.url if p.profile_photo else "",
    }


def serialize_teacher(teacher, *, active_track=None):
    """Single shape for the teacher identity, used by login, /me and the
    teacher-context switch. `tracks` carries the per-track status so the
    academy/skill-dev switch in both apps can render locked / pending /
    approved without extra round-trips."""
    if teacher is None:
        return None
    return {
        "type": teacher.teacher_type,          # legacy: GUEST | FACULTY | BOTH
        "tier": teacher.tier,
        "tracks": {
            "academy": teacher.academy_status,  # FACULTY track
            "skill":   teacher.skill_status,    # GUEST track
        },
        "academy_rejection_reason": teacher.academy_rejection_reason,
        "approved_tracks": teacher.approved_tracks(),
        "pending_tracks":  teacher.pending_tracks(),
        "active_track":    active_track,        # which dashboard is in context
    }


def _ensure_default_profile(user):
    profiles = list(user.learner_profiles.filter(is_active=True))
    if profiles:
        return profiles
    display_name = user.email.split("@")[0]
    lp = LearnerProfile.objects.create(
        account=user,
        display_name=display_name,
        relationship=LearnerProfile.RELATIONSHIP_SELF,
        is_default=True,
    )
    return [lp]


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — account login
# ─────────────────────────────────────────────────────────────────────────────

class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email    = (request.data.get("email")    or "").strip().lower()
        password =  request.data.get("password") or ""
        if not email or not password:
            raise ValidationError("Email and password are required.")

        user = authenticate(request, email=email, password=password)
        if not user:
            raise ValidationError("Invalid credentials.")
        if not user.is_verified:
            raise ValidationError("Email not verified.")

        profiles    = _ensure_default_profile(user)
        teacher     = getattr(user, "teacher_profile", None)
        # A teacher identity in ANY state (even a faculty application still in
        # review) means we show the picker, so the person sees their status and
        # can pick the learner side or an approved teaching track.
        has_teacher_identity = teacher is not None

        # Auto-select: single PIN-free profile, no teacher identity at all.
        if len(profiles) == 1 and not has_teacher_identity:
            profile = profiles[0]
            if not profile.has_pin():
                refresh = build_tokens(user, context=CTX_LEARNER, profile=profile)
                body = {
                    "context":      CTX_LEARNER,
                    "profile":      serialize_profile_card(profile),
                    "profiles":     [serialize_profile_card(profile)],
                    "teacher":      None,
                    "auto_selected": True,
                }
                return set_auth_cookies(Response(body, status=status.HTTP_200_OK), refresh)

        # Multiple profiles or a teacher identity → return account token, let
        # the frontend show the profile picker.
        refresh = build_tokens(user, context=CTX_ACCOUNT)
        body = {
            "context":  CTX_ACCOUNT,
            "profiles": [serialize_profile_card(p) for p in profiles],
            "teacher":  serialize_teacher(teacher),
        }
        return set_auth_cookies(Response(body, status=status.HTTP_200_OK), refresh)


# ─────────────────────────────────────────────────────────────────────────────
# Step 2A — select / switch learner profile
# ─────────────────────────────────────────────────────────────────────────────

class ProfileSelectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile_id = request.data.get("profile_id")
        pin        = request.data.get("pin")
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


# ─────────────────────────────────────────────────────────────────────────────
# Step 2B — enter teacher context (uses ACCOUNT password — no separate teacher pw)
# ─────────────────────────────────────────────────────────────────────────────

class TeacherContextView(APIView):
    """
    POST { password, track? }
    Uses the ACCOUNT password. No separate teacher password exists.

    `track` is optional and one of "academy" | "skill". It chooses which
    dashboard to enter for teachers approved on both tracks; when omitted we
    default to academy if approved, otherwise skill. The chosen track rides in
    the token as `active_track` so the dashboard switch knows where it is.

    200  { context: "teacher", teacher: { …, active_track } }
    409  { code: "no_teacher" }
    403  { code: "not_approved" }            # no approved track yet
    403  { code: "track_locked" }            # asked for a track they don't hold
    403  { code: "track_pending" }           # asked for a track still in review
    400  { code: "bad_password" }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        teacher = getattr(request.user, "teacher_profile", None)

        if not teacher:
            return Response(
                {"code": "no_teacher",
                 "detail": "This account has no teacher identity. Sign up as a teacher first."},
                status=status.HTTP_409_CONFLICT,
            )

        approved = teacher.approved_tracks()
        if not (approved and request.user.has_role(Role.TEACHER)):
            return Response(
                {"code": "not_approved", "detail": "Your teacher account is awaiting approval."},
                status=status.HTTP_403_FORBIDDEN,
            )

        password = request.data.get("password") or ""
        if not request.user.check_password(password):
            return Response(
                {"code": "bad_password", "password": "Incorrect password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve which dashboard to enter.
        track = request.data.get("track") or None
        if track:
            if track not in (teacher.TRACK_ACADEMY, teacher.TRACK_SKILL):
                raise ValidationError({"track": "Unknown track."})
            st = teacher.track_status(track)
            if st == teacher.TRACK_PENDING:
                return Response(
                    {"code": "track_pending",
                     "detail": "That track is still in admin review."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if st != teacher.TRACK_APPROVED:
                return Response(
                    {"code": "track_locked",
                     "detail": "You haven't been assigned to that track yet."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        else:
            # Default: prefer academy when approved, else the first approved.
            track = teacher.TRACK_ACADEMY if teacher.TRACK_ACADEMY in approved else approved[0]

        refresh = build_tokens(request.user, context=CTX_TEACHER, active_track=track)
        body = {
            "context": CTX_TEACHER,
            "teacher": serialize_teacher(teacher, active_track=track),
        }
        return set_auth_cookies(Response(body, status=status.HTTP_200_OK), refresh)


# ─────────────────────────────────────────────────────────────────────────────
# Profile PIN management
# ─────────────────────────────────────────────────────────────────────────────

class ProfilePinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile_id = request.data.get("profile_id")
        new_pin    = request.data.get("pin")

        profile = (
            LearnerProfile.objects
            .filter(id=profile_id, account=request.user)
            .first()
        )
        if not profile:
            raise PermissionDenied("Profile not found for this account.")

        if new_pin and (not str(new_pin).isdigit() or not (4 <= len(str(new_pin)) <= 6)):
            raise ValidationError({"pin": "PIN must be 4–6 digits."})

        profile.set_pin(str(new_pin) if new_pin else "")
        profile.save(update_fields=["pin"])
        return Response({"profile_id": str(profile.id), "requires_pin": profile.has_pin()})


# ─────────────────────────────────────────────────────────────────────────────
# /me/ — context-aware, reload-safe
# ─────────────────────────────────────────────────────────────────────────────

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
    data["avatar"]      = profile.avatar_value()
    return data


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user    = request.user
        token   = getattr(request, "auth", None)
        context = token.get("context") if token else None
        active_track = token.get("active_track") if token else None

        active   = get_active_profile(request)
        profiles = list(user.learner_profiles.filter(is_active=True))
        teacher  = getattr(user, "teacher_profile", None)

        return Response({
            "id":             str(user.id),
            "email":          user.email,
            "username":       user.username,
            "context":        context,
            "roles":          user.get_active_roles(),
            "active_profile": serialize_profile_card(active) if active else None,
            "profiles":       [serialize_profile_card(p) for p in profiles],
            "teacher":        serialize_teacher(teacher, active_track=active_track),
            "profile":          _legacy_profile_dict(active),
            "profile_complete": active.is_complete if active else False,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Learner Profile CRUD
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework.parsers import MultiPartParser, FormParser, JSONParser


class ProfileListCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

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

        if relationship == LearnerProfile.RELATIONSHIP_SELF:
            if request.user.learner_profiles.filter(
                relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
            ).exists():
                raise ValidationError({"relationship": "An account can only have one SELF profile."})

        if request.user.learner_profiles.filter(is_active=True).count() >= 5:
            raise ValidationError("Maximum of 5 profiles per account.")

        pin = data.get("pin", "")
        if pin and (not str(pin).isdigit() or not (4 <= len(str(pin)) <= 6)):
            raise ValidationError({"pin": "PIN must be 4–6 digits."})

        profile = LearnerProfile(
            account=request.user,
            display_name=display_name,
            relationship=relationship,
            is_default=not request.user.learner_profiles.filter(is_active=True).exists(),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name",  ""),
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
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

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
        data    = request.data

        if "display_name" in data:
            dn = data["display_name"].strip()
            if not dn:
                raise ValidationError({"display_name": "Display name cannot be empty."})
            profile.display_name = dn

        for f in ("first_name", "last_name"):
            if f in data:
                setattr(profile, f, data[f])

        # Optional personal data (Manage profile). None of these is required;
        # an empty value clears the field. These are the same fields the faculty
        # application form reads, so editing them here keeps that form in sync.
        for f in ("phone", "state", "district", "city_town", "pin_code"):
            if f in data:
                val = data.get(f)
                setattr(profile, f, val.strip() if isinstance(val, str) else (val or ""))

        if "gender" in data:
            g = (data.get("gender") or "").strip()
            allowed = {c[0] for c in LearnerProfile.GENDER_CHOICES}
            if g and g not in allowed:
                raise ValidationError({"gender": "Invalid choice."})
            profile.gender = g

        if "date_of_birth" in data:
            dob = (data.get("date_of_birth") or "").strip()
            if dob:
                from datetime import date
                try:
                    y, m, d = (int(x) for x in dob.split("-"))
                    profile.date_of_birth = date(y, m, d)
                except Exception:
                    raise ValidationError({"date_of_birth": "Use YYYY-MM-DD."})
            else:
                profile.date_of_birth = None

        if "profile_photo" in request.FILES:
            profile.profile_photo = request.FILES["profile_photo"]

        if "pin" in data:
            new_pin = data["pin"]
            if new_pin and (not str(new_pin).isdigit() or not (4 <= len(str(new_pin)) <= 6)):
                raise ValidationError({"pin": "PIN must be 4–6 digits."})
            profile.set_pin(str(new_pin) if new_pin else "")

        if "avatar_emoji" in data:
            profile.avatar_emoji = data["avatar_emoji"]
            profile.avatar_image = None
        if "avatar_image" in request.FILES:
            profile.avatar_image = request.FILES["avatar_image"]
            profile.avatar_emoji = ""

        profile.save()
        return Response(serialize_profile_card(profile))

    def delete(self, request, profile_id):
        profile      = self._get_profile(request, profile_id)
        active_count = request.user.learner_profiles.filter(is_active=True).count()
        if active_count <= 1:
            raise ValidationError("Cannot delete the only profile on this account.")

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

        profile.is_active  = False
        profile.is_default = False
        profile.save(update_fields=["is_active", "is_default"])
        return Response({"detail": "Profile removed."}, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# Unauthenticated helpers
# ─────────────────────────────────────────────────────────────────────────────

class ProfileEmailLookupView(APIView):
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


class EmailCheckView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from .models import User, Role, TeacherProfile
        email = (request.data.get("email") or "").strip().lower()
        empty = {"exists": False, "has_student": False, "has_teacher": False, "is_verified": False}
        if not email:
            return Response(empty)
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response(empty)
        teacher = getattr(user, "teacher_profile", None)
        return Response({
            "exists":        True,
            "has_student":   user.has_role(Role.STUDENT),
            "has_teacher":   teacher is not None,
            # Track-level status drives the signup branch: a teacher can add the
            # track they don't yet hold, but not one already live/in review.
            "teacher_type":  teacher.teacher_type if teacher else None,
            "academy_status": teacher.academy_status if teacher else "locked",
            "academy_rejection_reason": (teacher.academy_rejection_reason if teacher else ""),
            "skill_status":   teacher.skill_status if teacher else "locked",
            # Explicit add-eligibility so the frontend never re-derives the
            # asymmetric Faculty/Guest rule. For a non-teacher both are True
            # (they'd be creating a fresh teacher identity on one track).
            "can_add_academy": (
                teacher.can_apply_track(TeacherProfile.TRACK_ACADEMY)
                if teacher else True
            ),
            "can_add_skill": (
                teacher.can_apply_track(TeacherProfile.TRACK_SKILL)
                if teacher else True
            ),
            "is_verified":   user.is_verified,
        })
