"""
accounts/signup_serializer.py

One email = one container (User). That container can hold:
  - Up to 5 LearnerProfiles  (student signup, any mix of SELF + DEPENDENT)
  - One TeacherProfile        (teacher signup, also auto-gets a SELF LearnerProfile)

Both can coexist under the same email. Signup rules:

  Case 1  NEW email + Student   → create User + LearnerProfile(s) + STUDENT role
  Case 2  NEW email + Teacher   → create User + TeacherProfile + TEACHER role + SELF LearnerProfile
  Case 3  EXISTING (has_teacher, no student) + Student signup
          → verify password, add LearnerProfile(s) + STUDENT role to existing User
  Case 4  EXISTING (has_student, no teacher) + Teacher signup
          → verify password, add TeacherProfile + TEACHER role to existing User
  Case 5  EXISTING (has_student) + Student signup  → BLOCK
  Case 6  EXISTING (has_teacher) + Teacher signup  → BLOCK
"""
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate
from django.db import transaction

from .models import User, LearnerProfile, Role, UserRole, TeacherProfile


class SignupProfileSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=100)
    relationship = serializers.ChoiceField(
        choices=[LearnerProfile.RELATIONSHIP_SELF, LearnerProfile.RELATIONSHIP_DEPENDENT],
        required=False,
    )


class SignupSerializer(serializers.Serializer):
    email        = serializers.EmailField()
    username     = serializers.CharField(max_length=150, required=False, allow_blank=True)
    password     = serializers.CharField(write_only=True)
    role         = serializers.ChoiceField(choices=[Role.STUDENT, Role.TEACHER])

    # Student-only: learner profiles to create under this account.
    profiles = SignupProfileSerializer(many=True, required=False)

    # Teacher-only: which track they're applying through.
    teacher_type = serializers.ChoiceField(
        choices=[TeacherProfile.TYPE_GUEST, TeacherProfile.TYPE_FACULTY],
        required=False,
    )

    # ── internal flags set during validate() ──────────────────────────────
    # _mode:
    #   "create"               → brand new User
    #   "add_student_to_teacher" → existing teacher-only account, adding learner
    #   "add_teacher_to_student" → existing student-only account, adding teacher
    # _existing_user: the User object for add-to-existing modes

    def _account_state(self, email):
        """Returns (user|None, has_student_role, has_teacher_profile)."""
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
            # ── verify ownership: wrong password = wrong person ────────────
            authed = authenticate(email=email, password=password)
            if not authed:
                raise ValidationError({"password": "Incorrect password for this account."})

            if role == Role.STUDENT:
                if has_student:
                    raise ValidationError(
                        "This email already has learner profiles. Log in to manage them."
                    )
                # has_teacher only → allowed: add learner profiles to this container
                data["_mode"]          = "add_student_to_teacher"
                data["_existing_user"] = existing_user

            elif role == Role.TEACHER:
                if has_teacher:
                    raise ValidationError(
                        "This email already has a teacher account. Log in instead."
                    )
                if not data.get("teacher_type"):
                    raise ValidationError({"teacher_type": "Choose Guest expert or Faculty."})
                # has_student only → allowed: add teacher identity to this container
                data["_mode"]          = "add_teacher_to_student"
                data["_existing_user"] = existing_user

        else:
            # ── brand new account ─────────────────────────────────────────
            data["_mode"] = "create"

            # Validate password strength only for new accounts —
            # for existing accounts the password is used for verification only.
            try:
                validate_password(password)
            except Exception as e:
                raise ValidationError({"password": list(e.messages)})

            if not (data.get("username") or "").strip():
                raise ValidationError({"username": "Username is required."})

            if role == Role.TEACHER:
                if not data.get("teacher_type"):
                    raise ValidationError({"teacher_type": "Choose Guest expert or Faculty."})
            else:  # STUDENT
                for i, p in enumerate(data.get("profiles", [])):
                    if not p.get("display_name", "").strip():
                        raise ValidationError(
                            {"profiles": f"Profile {i + 1}: a name is required."}
                        )

        return data

    # ── creation ──────────────────────────────────────────────────────────

    @transaction.atomic
    def create(self, validated_data):
        mode          = validated_data.get("_mode", "create")
        existing_user = validated_data.get("_existing_user")

        if mode == "create":
            user = User.objects.create_user(
                email    = validated_data["email"],
                username = validated_data["username"],
                password = validated_data["password"],
            )
            user.is_verified = False
            user.save(update_fields=["is_verified"])
        else:
            # Adding to an existing verified account — do not recreate the User.
            user = existing_user

        if validated_data["role"] == Role.TEACHER:
            self._setup_teacher(user, validated_data["teacher_type"])
        else:
            self._setup_student(user, validated_data.get("profiles", []))

        return user

    # ── identity setup helpers ────────────────────────────────────────────

    def _setup_student(self, user, profiles):
        """
        Create LearnerProfiles and assign STUDENT role.

        For add-to-existing (teacher account gains learner profiles):
          - Teacher already has a SELF LearnerProfile.
          - New profiles are added. Any that would be SELF are downgraded
            to DEPENDENT because one SELF already exists.
          - is_default stays with the existing profile.
        """
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
                # Auto-assign: first entry of a new account → SELF; rest → DEPENDENT.
                rel = (
                    LearnerProfile.RELATIONSHIP_SELF
                    if (i == 0 and not has_self)
                    else LearnerProfile.RELATIONSHIP_DEPENDENT
                )

            # Guard the one-SELF-per-account rule.
            if rel == LearnerProfile.RELATIONSHIP_SELF and has_self:
                rel = LearnerProfile.RELATIONSHIP_DEPENDENT

            if rel == LearnerProfile.RELATIONSHIP_SELF:
                has_self = True  # mark for subsequent entries in this batch

            LearnerProfile.objects.create(
                account      = user,
                display_name = entry["display_name"].strip(),
                relationship = rel,
                # Only the very first profile on an account becomes the default.
                is_default   = (i == 0 and existing_count == 0),
            )

        student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
        UserRole.objects.get_or_create(
            user = user,
            role = student_role,
            defaults={
                "is_active":  True,
                # is_primary only if no other primary role exists yet.
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
            },
        )

    def _setup_teacher(self, user, teacher_type):
        """
        Create TeacherProfile and assign TEACHER role (pending approval).
        Also ensures the teacher has a SELF LearnerProfile for learner mode.
        """
        TeacherProfile.objects.create(user=user, teacher_type=teacher_type)

        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        UserRole.objects.get_or_create(
            user = user,
            role = teacher_role,
            defaults={
                "is_active":  False,   # inactive until admin approves
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
            },
        )

        # Every teacher needs a learner identity for the teach↔learn switch.
        # Only create one if a SELF profile doesn't already exist on the account
        # (i.e. they also signed up as a student — their existing SELF is reused).
        if not user.learner_profiles.filter(
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
        ).exists():
            LearnerProfile.objects.create(
                account      = user,
                display_name = user.username,
                relationship = LearnerProfile.RELATIONSHIP_SELF,
                is_default   = not user.learner_profiles.filter(is_active=True).exists(),
            )
