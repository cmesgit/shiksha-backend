"""
accounts/signup_serializer.py  ·  REFACTORED — single-password model

One email = one container (User). That container holds:
  - Up to 5 LearnerProfiles  (learner side: the holder + dependents)
  - One TeacherProfile        (teacher side: always also has a SELF learner)

PASSWORD MODEL:
  ONE password. The ACCOUNT password (User.password) is the only password.
  - It authenticates the learner login.
  - It authenticates entering teacher context (TeacherContextView).
  - There is NO separate teacher_password field on TeacherProfile.

Signup cases:

  Case 1  NEW email + Student
          → create User(account password) + LearnerProfile(s) + STUDENT role

  Case 2  NEW email + Teacher
          → create User(account password), create TeacherProfile,
            TEACHER role (inactive until approved), and a SELF LearnerProfile
            so they can also learn.

  Case 3  EXISTING (has_teacher, no student) + Student signup
          → verify ACCOUNT password (ownership proof — same password they
            already use to log in), add LearnerProfile(s) + STUDENT role.

  Case 4  EXISTING (has_student, no teacher) + Teacher signup
          → verify ACCOUNT password (ownership proof), add TeacherProfile
            + TEACHER role (reuses existing SELF learner).

  Case 5  EXISTING (has_student) + Student signup  → BLOCK
  Case 6  EXISTING teacher + signup for the OTHER track → ADD TRACK
          A teacher assigned to one track (e.g. Skill/Guest) can apply for the
          track they're missing (e.g. Academy/Faculty). The new track is added
          to the SAME TeacherProfile:
            · adding Skill (guest)   → listed immediately (approved)
            · adding Academy (faculty) → pending admin review
          The track they already hold keeps working the whole time.
  Case 7  EXISTING teacher + signup for a track they already hold → BLOCK
"""
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone

from .models import User, LearnerProfile, Role, UserRole, TeacherProfile


def _generate_unique_username(email):
    """Derive a unique username from the email local-part, adding a numeric
    suffix on collision. Used when signup doesn't supply one."""
    import re
    base = re.sub(r"[^a-zA-Z0-9_.+-]", "", (email or "").split("@")[0]) or "user"
    base = base[:140]  # leave room for a suffix (max_length 150)
    candidate = base
    n = 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        candidate = f"{base}{n}"
    return candidate


class SignupProfileSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=100)
    relationship = serializers.ChoiceField(
        choices=[LearnerProfile.RELATIONSHIP_SELF, LearnerProfile.RELATIONSHIP_DEPENDENT],
        required=False,
    )
    # Optional 4–6 digit PIN to lock this profile. Blank / omitted = no PIN.
    pin = serializers.CharField(max_length=6, required=False, allow_blank=True)


class SignupSerializer(serializers.Serializer):
    email    = serializers.EmailField()
    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    role     = serializers.ChoiceField(choices=[Role.STUDENT, Role.TEACHER])

    # THE ONE password — used for new accounts and as ownership proof for
    # add-to-existing flows.
    password = serializers.CharField(write_only=True)

    # NOTE: teacher_password field REMOVED — single password model.

    # Student-only: learner profiles to create under this account.
    profiles = SignupProfileSerializer(many=True, required=False)

    # Teacher-only: which track they're applying through.
    teacher_type = serializers.ChoiceField(
        choices=[TeacherProfile.TYPE_GUEST, TeacherProfile.TYPE_FACULTY],
        required=False,
    )

    # ── internal flags set during validate() ──────────────────────────────
    # _mode: "create" | "add_student_to_teacher" | "add_teacher_to_student"
    # _existing_user: the User for add-to-existing modes

    def _account_state(self, email):
        try:
            user = User.objects.get(email__iexact=email)
            has_student = user.has_role(Role.STUDENT)
            has_teacher = hasattr(user, "teacher_profile")
            return user, has_student, has_teacher
        except User.DoesNotExist:
            return None, False, False

    # ── field validators ──────────────────────────────────────────────────

    def validate_email(self, value):
        return value.strip().lower()

    def validate_username(self, value):
        if value and User.objects.filter(username__iexact=value).exists():
            raise ValidationError("Username is already taken.")
        return value

    # ── cross-field validation (the core logic) ───────────────────────────

    def validate(self, data):
        email    = data["email"]
        role     = data["role"]
        password = data["password"]

        existing_user, has_student, has_teacher = self._account_state(email)

        if existing_user:
            if role == Role.STUDENT:
                if has_student:
                    raise ValidationError(
                        "This email already has learner profiles. Log in to manage them."
                    )
                # has_teacher only → add learner profiles.
                # Ownership proof = account password (same one they log in with).
                authed = authenticate(email=email, password=password)
                if not authed:
                    raise ValidationError(
                        {"password": "Incorrect password for this account."}
                    )
                data["_mode"]          = "add_student_to_teacher"
                data["_existing_user"] = existing_user

            elif role == Role.TEACHER:
                if not data.get("teacher_type"):
                    raise ValidationError(
                        {"teacher_type": "Choose Guest expert (skill) or Faculty (academy)."}
                    )
                target_track = TeacherProfile.track_for_type(data["teacher_type"])

                if has_teacher:
                    tp = existing_user.teacher_profile
                    current = tp.track_status(target_track)
                    # Already hold this track (live or in review) → nothing to add.
                    if current in (TeacherProfile.TRACK_PENDING, TeacherProfile.TRACK_APPROVED):
                        nice = "Academy (Faculty)" if target_track == TeacherProfile.TRACK_ACADEMY else "Skill (Guest expert)"
                        raise ValidationError(
                            f"You're already set up for {nice} on this account. Log in instead."
                        )
                    # Otherwise they're adding the track they're missing.
                    authed = authenticate(email=email, password=password)
                    if not authed:
                        raise ValidationError(
                            {"password": "Incorrect password for this account."}
                        )
                    data["_mode"]          = "add_teacher_track"
                    data["_existing_user"] = existing_user
                    data["_target_track"]  = target_track
                    return data

                # has_student only → add a brand-new teacher identity (one track).
                # Ownership proof = account password.
                authed = authenticate(email=email, password=password)
                if not authed:
                    raise ValidationError(
                        {"password": "Incorrect password for this account."}
                    )
                data["_mode"]          = "add_teacher_to_student"
                data["_existing_user"] = existing_user

        else:
            # ── brand new account ─────────────────────────────────────────
            data["_mode"] = "create"

            # Username is optional. If blank, we auto-generate a unique one in
            # create() from the email local-part. (Login is by email; the
            # username is just a unique internal handle — users shouldn't have
            # to invent one, and a profile/display name must NOT double as it.)

            try:
                validate_password(password)
            except Exception as e:
                raise ValidationError({"password": list(e.messages)})

            if role == Role.TEACHER:
                if not data.get("teacher_type"):
                    raise ValidationError({"teacher_type": "Choose Guest expert or Faculty."})

        # ── STUDENT identity guards (new + add-to-existing) ──────────────
        if role == Role.STUDENT:
            submitted = data.get("profiles", []) or []
            for i, p in enumerate(submitted):
                if not (p.get("display_name") or "").strip():
                    raise ValidationError(
                        {"profiles": f"Profile {i + 1}: a name is required."}
                    )
                pin = (p.get("pin") or "").strip()
                if pin and (not pin.isdigit() or not (4 <= len(pin) <= 6)):
                    raise ValidationError(
                        {"profiles": f"Profile {i + 1}: PIN must be 4-6 digits."}
                    )
            existing_count = (
                existing_user.learner_profiles.filter(is_active=True).count()
                if existing_user else 0
            )
            to_add = len(submitted) if submitted else 1
            if existing_count + to_add > 5:
                remaining = max(0, 5 - existing_count)
                raise ValidationError({
                    "profiles": (
                        "An account can hold at most 5 learner profiles. "
                        + (f"You can add {remaining} more."
                           if remaining else "This account is already at the limit.")
                    )
                })

        return data

    # ── creation ──────────────────────────────────────────────────────────

    @transaction.atomic
    def create(self, validated_data):
        mode          = validated_data.get("_mode", "create")
        existing_user = validated_data.get("_existing_user")
        role          = validated_data["role"]
        password      = validated_data["password"]

        if mode == "create":
            supplied = (validated_data.get("username") or "").strip()
            username = supplied or _generate_unique_username(validated_data["email"])
            user = User.objects.create_user(
                email    = validated_data["email"],
                username = username,
                password = password,
            )
            user.is_verified = False
            user.save(update_fields=["is_verified"])
        else:
            user = existing_user

        if role == Role.TEACHER:
            if mode == "add_teacher_track":
                self._add_teacher_track(user, validated_data["_target_track"])
            else:
                self._setup_teacher(user, validated_data["teacher_type"])
        else:
            self._setup_student(user, validated_data.get("profiles", []))

        return user

    # ── identity setup helpers ────────────────────────────────────────────

    def _setup_student(self, user, profiles):
        existing_count = user.learner_profiles.filter(is_active=True).count()
        has_self = user.learner_profiles.filter(
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
        ).exists()

        entries = profiles or [{
            "display_name": user.username,
            "relationship": LearnerProfile.RELATIONSHIP_SELF,
        }]

        for i, entry in enumerate(entries):
            rel = entry.get("relationship")
            if not rel:
                rel = (
                    LearnerProfile.RELATIONSHIP_SELF
                    if (i == 0 and not has_self)
                    else LearnerProfile.RELATIONSHIP_DEPENDENT
                )
            if rel == LearnerProfile.RELATIONSHIP_SELF and has_self:
                rel = LearnerProfile.RELATIONSHIP_DEPENDENT
            if rel == LearnerProfile.RELATIONSHIP_SELF:
                has_self = True

            lp = LearnerProfile(
                account      = user,
                display_name = entry["display_name"].strip(),
                relationship = rel,
                is_default   = (i == 0 and existing_count == 0),
            )
            pin = (entry.get("pin") or "").strip()
            if pin:
                lp.set_pin(pin)
            lp.save()

        student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
        UserRole.objects.get_or_create(
            user = user,
            role = student_role,
            defaults={
                "is_active":  True,
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
            },
        )

    # ── track approval policy ──────────────────────────────────────────────
    # Skill (Guest expert) is auto-listed the moment they apply; Academy
    # (Faculty) waits in the admin review queue. One place decides this so the
    # new-teacher and add-track paths can never drift apart.
    def _initial_status_for(self, track):
        return (
            TeacherProfile.TRACK_APPROVED
            if track == TeacherProfile.TRACK_SKILL
            else TeacherProfile.TRACK_PENDING
        )

    def _ensure_teacher_role(self, user, *, active):
        """Create/refresh the TEACHER UserRole. `active` mirrors whether the
        teacher already has a live (approved) track — an active role row is
        what lets them enter teacher mode; a pending faculty applicant stays
        inactive so they surface in the admin approval queue."""
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        role, created = UserRole.objects.get_or_create(
            user=user,
            role=teacher_role,
            defaults={
                "is_active":  active,
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
                "approved_at": timezone.now() if active else None,
            },
        )
        if not created and active and not role.is_active:
            role.is_active   = True
            role.approved_at = role.approved_at or timezone.now()
            role.save(update_fields=["is_active", "approved_at"])
        return role

    def _ensure_self_learner(self, user):
        """A teacher account always carries a SELF learner profile so the
        person can also learn / switch to the student side."""
        if not user.learner_profiles.filter(
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
        ).exists():
            LearnerProfile.objects.create(
                account      = user,
                display_name = user.username or user.email.split("@")[0],
                relationship = LearnerProfile.RELATIONSHIP_SELF,
                is_default   = not user.learner_profiles.filter(is_active=True).exists(),
            )

    def _setup_teacher(self, user, teacher_type):
        """Brand-new teacher identity, applying through a single track."""
        track  = TeacherProfile.track_for_type(teacher_type)
        status = self._initial_status_for(track)

        tp = TeacherProfile(user=user, teacher_type=teacher_type)
        tp.set_track_status(track, status)
        tp.sync_type_from_tracks()   # sets teacher_type + is_approved coherently
        tp.save()

        self._ensure_teacher_role(user, active=bool(tp.approved_tracks()))
        self._ensure_self_learner(user)

    def _add_teacher_track(self, user, track):
        """Existing teacher applying for the track they don't yet hold.
        The track they already have keeps working untouched."""
        tp = user.teacher_profile
        tp.set_track_status(track, self._initial_status_for(track))
        tp.sync_type_from_tracks()
        tp.save(update_fields=["academy_status", "skill_status",
                               "teacher_type", "is_approved"])

        # If the newly added track is live (skill), make sure the role is
        # active so they can enter that dashboard right away.
        self._ensure_teacher_role(user, active=bool(tp.approved_tracks()))
