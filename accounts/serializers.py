from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from rest_framework.exceptions import ValidationError
from django.db import transaction

from .models import User, Profile, Role, UserRole, TeacherProfile


# =====================================================
# PROFILE READ SERIALIZER (/me/)
# =====================================================

class ProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    avatar_type = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = Profile
        fields = [
            "full_name",
            "email",
            "student_id",
            "phone",
            "avatar_type",
            "avatar",
        ]

    def get_avatar_type(self, obj):
        return obj.avatar_type()

    def get_avatar(self, obj):
        return obj.avatar_value()


# =====================================================
# PROFILE UPDATE SERIALIZER
# =====================================================

class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = [
            "full_name",
            "phone",
            "avatar_image",
            "avatar_emoji",
        ]


# =====================================================
# UPDATE USER + PROFILE
# =====================================================

class UserUpdateSerializer(serializers.ModelSerializer):
    profile = ProfileUpdateSerializer(required=False)

    class Meta:
        model = User
        fields = ("username", "profile")

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", None)

        # Update user fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Update profile safely
        if profile_data:
            profile = instance.profile

            # Only one avatar type allowed
            if profile_data.get("avatar_image"):
                profile.avatar_emoji = None

            if profile_data.get("avatar_emoji"):
                profile.avatar_image = None

            for attr, value in profile_data.items():
                setattr(profile, attr, value)

            profile.save()

        return instance


# =====================================================
# USER /me/ SERIALIZER
# =====================================================

class UserMeSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(read_only=True)
    roles = serializers.SerializerMethodField()
    enrollments = serializers.SerializerMethodField()

    profile_complete = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "username",
            "profile",
            "roles",
            "enrollments",
            "profile_complete",
        )

    def get_profile_complete(self, obj):
        roles = obj.get_active_roles()
        if "TEACHER" in roles:
            tp = getattr(obj, "teacher_profile", None)
            return tp.is_complete if tp else False
        profile = getattr(obj, "profile", None)
        return profile.is_complete if profile else False

    def get_roles(self, obj):
        return list(
            obj.user_roles
            .filter(is_active=True)
            .values_list("role__name", flat=True)
        )

    def get_enrollments(self, obj):
        enrollments = (
            obj.enrollments
            .filter(status="ACTIVE")
            .select_related("course")
        )

        return [
            {
                "id": e.id,
                "course_title": e.course.title,
                "batch_code": e.batch_code,
            }
            for e in enrollments
        ]


# =====================================================
# SIGNUP SERIALIZER
# =====================================================

class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ("email", "username", "password")

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

    @transaction.atomic
    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data["email"],
            username=validated_data["username"],
            password=validated_data["password"],
        )

        # Ensure unverified by default
        user.is_verified = False
        user.save(update_fields=["is_verified"])

        # IMPORTANT: Roles must be seeded beforehand
        try:
            guest_role = Role.objects.get(name=Role.GUEST)
        except Role.DoesNotExist:
            raise ValidationError("Default role not configured.")

        UserRole.objects.create(
            user=user,
            role=guest_role,
            is_active=True,
            is_primary=True,
        )

        return user


# =====================================================
# STUDENT FORM FILLUP SERIALIZER
# =====================================================

class StudentFormFillupSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name")
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Profile
        fields = [
            "name",
            "email",
            "phone",
            "date_of_birth",
            "father_name",
            "father_phone",
            "mother_name",
            "guardian",
            "guardian_phone",
            "current_address",
            "permanent_address",
            "same_as_current",
        ]

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


# =====================================================
# TEACHER FORM FILLUP SERIALIZER
# =====================================================

class TeacherFormFillupSerializer(serializers.Serializer):
    # Shared fields (stored on Profile)
    name = serializers.CharField()
    phone = serializers.CharField()

    # Teacher-specific fields
    gender = serializers.ChoiceField(choices=TeacherProfile.GENDER_CHOICES)
    date_of_birth = serializers.DateField()
    father_name = serializers.CharField()
    father_phone = serializers.CharField(required=False, allow_blank=True)
    mother_name = serializers.CharField()
    mother_phone = serializers.CharField(required=False, allow_blank=True)
    current_address = serializers.CharField()
    permanent_address = serializers.CharField(required=False, allow_blank=True)
    same_as_current = serializers.BooleanField(default=False)
    highest_qualification = serializers.ChoiceField(
        choices=TeacherProfile.QUALIFICATION_CHOICES
    )
    other_qualification = serializers.CharField(required=False, allow_blank=True)
    subject_specialization = serializers.CharField()
    teaching_experience_years = serializers.IntegerField(default=0)
    previous_institution = serializers.CharField(required=False, allow_blank=True)

    def update(self, user, validated_data):
        # Update Profile (shared fields)
        profile = user.profile
        profile.full_name = validated_data["name"]
        profile.phone = validated_data["phone"]
        profile.save()

        # Update TeacherProfile
        tp, _ = TeacherProfile.objects.get_or_create(user=user)

        teacher_fields = [
            "gender", "date_of_birth", "father_name", "father_phone",
            "mother_name", "mother_phone", "current_address",
            "permanent_address", "same_as_current", "highest_qualification",
            "other_qualification", "subject_specialization",
            "teaching_experience_years", "previous_institution",
        ]

        for field in teacher_fields:
            if field in validated_data:
                setattr(tp, field, validated_data[field])

        tp.save()
        return user
