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
  Case 6  EXISTING (has_teacher) + Teacher signup  → BLOCK
"""
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone

from .models import User, LearnerProfile, Role, UserRole, TeacherProfile


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
                if has_teacher:
                    raise ValidationError(
                        "This email already has a teacher account. Log in instead."
                    )
                if not data.get("teacher_type"):
                    raise ValidationError({"teacher_type": "Choose Guest expert or Faculty."})
                # has_student only → add teacher identity.
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

            if not (data.get("username") or "").strip():
                raise ValidationError({"username": "Username is required."})

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
            user = User.objects.create_user(
                email    = validated_data["email"],
                username = validated_data["username"],
                password = password,
            )
            user.is_verified = False
            user.save(update_fields=["is_verified"])
        else:
            user = existing_user

        if role == Role.TEACHER:
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

    def _setup_teacher(self, user, teacher_type):
        # Approval policy by track (matches the signup UI):
        #   GUEST  → listed immediately, no screening ("You're live!")
        #   FACULTY → inactive until an admin approves (admin review queue)
        # So a guest is approved/active at signup; a faculty applicant is not.
        is_guest = teacher_type == TeacherProfile.TYPE_GUEST

        # No teacher_password — single account password handles everything.
        tp = TeacherProfile(
            user=user,
            teacher_type=teacher_type,
            is_approved=is_guest,
        )
        tp.save()

        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        role, created = UserRole.objects.get_or_create(
            user = user,
            role = teacher_role,
            defaults={
                # Guests go live immediately; faculty await admin approval.
                "is_active":  is_guest,
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
                # Stamp auto-approval so guests never appear in the admin queue
                # (which filters is_active=False, approved_at__isnull=True).
                "approved_at": timezone.now() if is_guest else None,
            },
        )
        # Edge case: a TEACHER role row already existed for this account. If
        # this is a guest signup, make sure it's active/stamped.
        if not created and is_guest and not role.is_active:
            role.is_active   = True
            role.approved_at = role.approved_at or timezone.now()
            role.save(update_fields=["is_active", "approved_at"])

        # Teacher always gets a SELF learner profile too.
        if not user.learner_profiles.filter(
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
        ).exists():
            LearnerProfile.objects.create(
                account      = user,
                display_name = user.username,
                relationship = LearnerProfile.RELATIONSHIP_SELF,
                is_default   = not user.learner_profiles.filter(is_active=True).exists(),
            )
