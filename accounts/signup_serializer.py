"""
accounts/signup_serializer.py

Rebuilt signup for the multi-profile + two-teacher-track model.

INTEGRATION
-----------
Replace the old `SignupSerializer` class in accounts/serializers.py with the
class below (or import it there), and make sure the model import line in
serializers.py includes LearnerProfile:

    from .models import (
        User, Profile, LearnerProfile, Role, UserRole, TeacherProfile,
        TeacherCourseApplication, TeacherSkillApplication,
    )

The existing SignupView needs no change — it still calls
`SignupSerializer(data=request.data)` and emails the verification link.

PAYLOAD
-------
Student:
    { "email", "username", "password", "role": "STUDENT",
      "profiles": [ { "display_name": "Rohan", "relationship": "DEPENDENT" }, ... ] }
    (profiles optional; if omitted, one default profile is created)

Teacher:
    { "email", "username", "password", "role": "TEACHER",
      "teacher_type": "GUEST" | "FACULTY" }
"""
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction

from .models import User, LearnerProfile, Role, UserRole, TeacherProfile


class SignupProfileSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=100)
    relationship = serializers.ChoiceField(
        choices=[LearnerProfile.RELATIONSHIP_SELF, LearnerProfile.RELATIONSHIP_DEPENDENT],
        required=False,
    )


class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True)
    role = serializers.ChoiceField(choices=[Role.STUDENT, Role.TEACHER])

    # Student-only: the learner profiles to create under this account.
    profiles = SignupProfileSerializer(many=True, required=False)

    # Teacher-only: which track they're applying through.
    teacher_type = serializers.ChoiceField(
        choices=[TeacherProfile.TYPE_GUEST, TeacherProfile.TYPE_FACULTY],
        required=False,
    )

    # ---- field validation ----

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise ValidationError("Email is already registered.")
        return value

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise ValidationError("Username is already taken.")
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate(self, data):
        if data["role"] == Role.TEACHER:
            if not data.get("teacher_type"):
                raise ValidationError(
                    {"teacher_type": "Choose Guest expert or Faculty."}
                )
        else:  # STUDENT
            for i, p in enumerate(data.get("profiles", [])):
                if not p.get("display_name", "").strip():
                    raise ValidationError(
                        {"profiles": f"Profile {i + 1}: a name is required."}
                    )
        return data

    # ---- creation ----

    @transaction.atomic
    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data["email"],
            username=validated_data["username"],
            password=validated_data["password"],
        )
        user.is_verified = False
        user.save(update_fields=["is_verified"])

        if validated_data["role"] == Role.TEACHER:
            self._setup_teacher(user, validated_data["teacher_type"])
        else:
            self._setup_student(user, validated_data.get("profiles", []))

        return user

    def _setup_student(self, user, profiles):
        entries = profiles or [{"display_name": user.username, "relationship": LearnerProfile.RELATIONSHIP_SELF}]

        for i, entry in enumerate(entries):
            relationship = entry.get("relationship") or (
                LearnerProfile.RELATIONSHIP_SELF if i == 0
                else LearnerProfile.RELATIONSHIP_DEPENDENT
            )
            LearnerProfile.objects.create(
                account=user,
                display_name=entry["display_name"].strip(),
                relationship=relationship,
                is_default=(i == 0),
            )

        student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
        UserRole.objects.create(
            user=user, role=student_role, is_active=True, is_primary=True
        )

    def _setup_teacher(self, user, teacher_type):
        # The teacher identity, pending screening.
        TeacherProfile.objects.create(user=user, teacher_type=teacher_type)

        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        UserRole.objects.create(
            user=user, role=teacher_role, is_active=False, is_primary=True
        )

        # Teachers learn too: give them a default learner profile up front so
        # the teach<->learn switch works the moment they're verified.
        LearnerProfile.objects.create(
            account=user,
            display_name=user.username,
            relationship=LearnerProfile.RELATIONSHIP_SELF,
            is_default=True,
        )
