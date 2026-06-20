"""
accounts/signup_serializer.py  (REVISED — separate teacher password)

One email = one container (User). That container holds:
  - Up to 5 LearnerProfiles  (learner side: the holder + dependents)
  - One TeacherProfile        (teacher side: always also has a SELF learner)

PASSWORD MODEL (new):
  - The ACCOUNT password (User.password) authenticates the LEARNER door
    (account login + learner profile selection).
  - The TEACHER password (TeacherProfile.teacher_password) authenticates the
    TEACHER door (entering teacher context). It is independent.

Signup rules:

  Case 1  NEW email + Student
          → create User(account password) + LearnerProfile(s) + STUDENT role
  Case 2  NEW email + Teacher
          → create User, set ACCOUNT password = teacher password (only password
            they have), create TeacherProfile(teacher_password), TEACHER role,
            and a SELF LearnerProfile so they can also learn.
            A later learner signup (Case 3) can set a distinct account password.
  Case 3  EXISTING (has_teacher, no student) + Student signup
          → verify TEACHER password (ownership) + set/confirm account password,
            add LearnerProfile(s) + STUDENT role.
  Case 4  EXISTING (has_student, no teacher) + Teacher signup
          → verify ACCOUNT password (ownership) + set a NEW teacher password,
            add TeacherProfile + TEACHER role (+ reuse existing SELF learner).
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
    role         = serializers.ChoiceField(choices=[Role.STUDENT, Role.TEACHER])

    # The LEARNER / account password (used for STUDENT signups, and as the
    # ownership proof when an EXISTING student adds a teacher identity).
    password     = serializers.CharField(write_only=True)

    # The TEACHER password. Required for TEACHER signups. For a brand-new
    # teacher this is ALSO written as the account password (their only one).
    # When an existing student adds teacher, `password` proves ownership and
    # `teacher_password` sets the new teacher door.
    teacher_password = serializers.CharField(write_only=True, required=False, allow_blank=True)

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
        teacher_password = data.get("teacher_password") or ""

        existing_user, has_student, has_teacher = self._account_state(email)

        if existing_user:
            if role == Role.STUDENT:
                if has_student:
                    raise ValidationError(
                        "This email already has learner profiles. Log in to manage them."
                    )
                # has_teacher only → add learner profiles.
                # Ownership proof = TEACHER password (the only one they have).
                teacher = existing_user.teacher_profile
                if not teacher.check_teacher_password(password):
                    raise ValidationError(
                        {"password": "Incorrect teacher password for this account."}
                    )
                # The account password is currently the same as the teacher's
                # (brand-new teacher reused it). Allow setting a distinct learner
                # password here if provided in `teacher_password` slot is N/A;
                # we keep the existing account password unless a new one is sent
                # via a dedicated field. For now reuse account password as-is.
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
                # Ownership proof = ACCOUNT password. Then SET a new teacher pw.
                authed = authenticate(email=email, password=password)
                if not authed:
                    raise ValidationError(
                        {"password": "Incorrect account password for this account."}
                    )
                if not teacher_password:
                    raise ValidationError(
                        {"teacher_password": "Set a teacher password."}
                    )
                try:
                    validate_password(teacher_password)
                except Exception as e:
                    raise ValidationError({"teacher_password": list(e.messages)})
                data["_mode"]          = "add_teacher_to_student"
                data["_existing_user"] = existing_user

        else:
            # ── brand new account ─────────────────────────────────────────
            data["_mode"] = "create"

            if not (data.get("username") or "").strip():
                raise ValidationError({"username": "Username is required."})

            if role == Role.TEACHER:
                if not data.get("teacher_type"):
                    raise ValidationError({"teacher_type": "Choose Guest expert or Faculty."})
                # Brand-new teacher: the teacher password IS their only password.
                # The Signup form should send the chosen teacher password in
                # BOTH `password` and `teacher_password` (or just `password` and
                # we mirror it). We standardise: use `password` as the secret and
                # ALSO store it as the teacher password.
                try:
                    validate_password(password)
                except Exception as e:
                    raise ValidationError({"password": list(e.messages)})
            else:
                # Brand-new student account password strength.
                try:
                    validate_password(password)
                except Exception as e:
                    raise ValidationError({"password": list(e.messages)})

        # ── STUDENT identity guards (new + add-to-existing) ──
        if role == Role.STUDENT:
            submitted = data.get("profiles", []) or []
            for i, p in enumerate(submitted):
                if not (p.get("display_name") or "").strip():
                    raise ValidationError(
                        {"profiles": f"Profile {i + 1}: a name is required."}
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
        teacher_password = validated_data.get("teacher_password") or ""

        if mode == "create":
            user = User.objects.create_user(
                email    = validated_data["email"],
                username = validated_data["username"],
                password = password,   # account password (also teacher's, for now)
            )
            user.is_verified = False
            user.save(update_fields=["is_verified"])
        else:
            user = existing_user

        if role == Role.TEACHER:
            # Brand-new teacher → teacher password mirrors the account password.
            # Existing student adding teacher → use the explicitly set teacher pw.
            tp_secret = teacher_password if mode == "add_teacher_to_student" else password
            self._setup_teacher(user, validated_data["teacher_type"], tp_secret)
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

            LearnerProfile.objects.create(
                account      = user,
                display_name = entry["display_name"].strip(),
                relationship = rel,
                is_default   = (i == 0 and existing_count == 0),
            )

        student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
        UserRole.objects.get_or_create(
            user = user,
            role = student_role,
            defaults={
                "is_active":  True,
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
            },
        )

    def _setup_teacher(self, user, teacher_type, teacher_secret):
        tp = TeacherProfile(user=user, teacher_type=teacher_type)
        tp.set_teacher_password(teacher_secret)
        tp.save()

        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        UserRole.objects.get_or_create(
            user = user,
            role = teacher_role,
            defaults={
                "is_active":  False,   # inactive until admin approves
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
            },
        )

        # Teacher always needs a SELF learner profile (they can learn too).
        if not user.learner_profiles.filter(
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
        ).exists():
            LearnerProfile.objects.create(
                account      = user,
                display_name = user.username,
                relationship = LearnerProfile.RELATIONSHIP_SELF,
                is_default   = not user.learner_profiles.filter(is_active=True).exists(),
            )
