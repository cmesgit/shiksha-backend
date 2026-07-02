import json

from datetime import date
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from rest_framework.exceptions import ValidationError
from django.db import transaction
from .signup_serializer import SignupSerializer
from .models import User, LearnerProfile, Role, UserRole, TeacherProfile, TeacherCourseApplication, TeacherSkillApplication


def default_learner(user):
    """Return the account's default LearnerProfile (the SELF holder), or None.

    Replaces the old one-to-one ``user.profile`` lookup now that personal data
    lives on LearnerProfile. Prefers the default; falls back to the first active
    SELF profile, then any active profile.
    """
    if user is None:
        return None
    return user.default_learner_profile()


# =====================================================
# PROFILE READ SERIALIZER (/me/)
# =====================================================

class ProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="account.email", read_only=True)
    avatar_type = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = LearnerProfile
        fields = [
            "first_name",
            "last_name",
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
        value = obj.avatar_value()
        if value and obj.avatar_image:
            request = self.context.get("request")
            if request is not None:
                return request.build_absolute_uri(value)
        return value


# =====================================================
# PROFILE UPDATE SERIALIZER
# =====================================================

class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LearnerProfile
        fields = [
            "full_name",
            "first_name",
            "last_name",
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
            profile = default_learner(instance)
        if profile_data and profile:

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
    profile = serializers.SerializerMethodField()
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

    def get_profile(self, obj):
        lp = default_learner(obj)
        return ProfileSerializer(lp, context=self.context).data if lp else None

    def get_profile_complete(self, obj):
        roles = obj.get_active_roles()
        if "TEACHER" in roles:
            tp = getattr(obj, "teacher_profile", None)
            return tp.is_complete if tp else False
        profile = default_learner(obj)
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

#removed
# =====================================================
# STUDENT FORM FILLUP SERIALIZER (REVAMPED)
# =====================================================

class StudentFormFillupSerializer(serializers.Serializer):
    # --- Personal Info ---
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=20)
    gender = serializers.ChoiceField(
        choices=LearnerProfile.GENDER_CHOICES, required=False, allow_blank=True
    )
    date_of_birth = serializers.DateField()
    profile_photo = serializers.ImageField(required=False, allow_null=True)

    # --- Address ---
    state = serializers.CharField(max_length=100)
    district = serializers.CharField(max_length=100)
    city_town = serializers.CharField(max_length=150)
    pin_code = serializers.CharField(max_length=10, required=False, allow_blank=True)

    # --- Parent/Guardian ---
    father_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    father_phone = serializers.CharField(max_length=15, required=False, allow_blank=True)
    mother_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    mother_phone = serializers.CharField(max_length=15, required=False, allow_blank=True)
    guardian_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    guardian_phone = serializers.CharField(max_length=15, required=False, allow_blank=True)
    parent_guardian_email = serializers.EmailField(required=False, allow_blank=True)

    # --- Academic Info ---
    currently_studying = serializers.ChoiceField(choices=LearnerProfile.CURRENTLY_STUDYING_CHOICES)

    # If currently studying = yes
    current_class = serializers.ChoiceField(
        choices=LearnerProfile.CLASS_CHOICES, required=False, allow_blank=True
    )
    stream = serializers.ChoiceField(
        choices=LearnerProfile.STREAM_CHOICES, required=False, allow_blank=True
    )
    board = serializers.ChoiceField(
        choices=LearnerProfile.BOARD_CHOICES, required=False, allow_blank=True
    )
    board_other = serializers.CharField(max_length=150, required=False, allow_blank=True)
    school_name = serializers.CharField(max_length=250, required=False, allow_blank=True)
    academic_year = serializers.CharField(max_length=20, required=False, allow_blank=True)

    # If currently studying = no
    highest_education = serializers.ChoiceField(
        choices=LearnerProfile.HIGHEST_EDUCATION_CHOICES, required=False, allow_blank=True
    )
    reason_not_studying = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )

    def validate(self, data):
        # At least ONE complete parent/guardian contact (name + phone)
        has_contact = (
            (data.get("father_name") and data.get("father_phone"))
            or (data.get("mother_name") and data.get("mother_phone"))
            or (data.get("guardian_name") and data.get("guardian_phone"))
        )
        if not has_contact:
            raise ValidationError({
                "parent_guardian": "At least one complete parent/guardian contact (name + phone) is required."
            })

        # Conditional academic validation
        currently_studying = data.get("currently_studying")

        if currently_studying == "yes":
            if not data.get("current_class"):
                raise ValidationError({"current_class": "Class is required when currently studying."})
            if not data.get("board"):
                raise ValidationError({"board": "Board is required when currently studying."})
            if data.get("board") == "other" and not data.get("board_other"):
                raise ValidationError({"board_other": "Please specify your board."})

            # Stream required for class 11-12
            current_class = data.get("current_class", "")
            if current_class in ("11", "12") and not data.get("stream"):
                raise ValidationError({"stream": "Stream is required for Class 11-12."})

            # Auto-populate academic year
            today = date.today()
            if today.month >= 4:
                data["academic_year"] = f"{today.year}-{today.year + 1}"
            else:
                data["academic_year"] = f"{today.year - 1}-{today.year}"

        elif currently_studying == "no":
            if not data.get("highest_education"):
                raise ValidationError({
                    "highest_education": "Highest education is required when not currently studying."
                })

        return data

    def update(self, profile, validated_data):
        # Remove profile_photo if not provided (don't clear existing)
        photo = validated_data.pop("profile_photo", None)

        for attr, value in validated_data.items():
            setattr(profile, attr, value)

        if photo:
            profile.profile_photo = photo

        profile.save()
        return profile


# =====================================================
# TEACHER FORM FILLUP SERIALIZER (REVAMPED)
# =====================================================

class TeacherFormFillupSerializer(serializers.Serializer):
    # --- Personal Info (stored on Profile) ---
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=20)
    gender = serializers.ChoiceField(
        choices=LearnerProfile.GENDER_CHOICES, required=False, allow_blank=True
    )
    date_of_birth = serializers.DateField()
    profile_photo = serializers.ImageField(required=False, allow_null=True)

    # --- Address (stored on Profile) ---
    state = serializers.CharField(max_length=100)
    district = serializers.CharField(max_length=100)
    city_town = serializers.CharField(max_length=150)
    pin_code = serializers.CharField(max_length=10, required=False, allow_blank=True)

    # --- Educational Qualifications ---
    highest_degree = serializers.ChoiceField(choices=TeacherProfile.HIGHEST_DEGREE_CHOICES)
    field_of_study = serializers.CharField(max_length=200)
    year_of_completion = serializers.IntegerField(min_value=1970, max_value=2026)
    teaching_certifications = serializers.ListField(
        child=serializers.CharField(max_length=20),
        required=False,
        allow_empty=True,
    )
    qualification_certificate = serializers.FileField(required=False, allow_null=True)

    # --- Teaching Experience ---
    experience_range = serializers.ChoiceField(choices=TeacherProfile.EXPERIENCE_CHOICES)
    employment_status = serializers.ChoiceField(choices=TeacherProfile.EMPLOYMENT_STATUS_CHOICES)
    currently_employed = serializers.BooleanField(default=False)
    current_institution = serializers.CharField(
        max_length=250, required=False, allow_blank=True
    )
    current_position = serializers.CharField(
        max_length=150, required=False, allow_blank=True
    )

    # --- Verification Documents ---
    govt_id_type = serializers.ChoiceField(choices=TeacherProfile.GOVT_ID_TYPE_CHOICES)
    id_number = serializers.CharField(max_length=50)
    id_proof_front = serializers.FileField(required=True)
    id_proof_back = serializers.FileField(required=False, allow_null=True)
    signed_agreement = serializers.FileField(required=False, allow_null=True)

        # --- Course Applications (JSON string) ---
    course_applications = serializers.CharField(required=False, default="[]")

    # --- Skill Applications (JSON string) ---
    skill_applications = serializers.CharField(required=False, default="[]")

    def validate_qualification_certificate(self, value):
        if value and value.size > 5 * 1024 * 1024:
            raise ValidationError("Qualification certificate must be under 5MB.")
        return value

    def validate_id_proof_front(self, value):
        if value and value.size > 5 * 1024 * 1024:
            raise ValidationError("ID proof must be under 5MB.")
        return value

    def validate_id_proof_back(self, value):
        if value and value.size > 5 * 1024 * 1024:
            raise ValidationError("ID proof must be under 5MB.")
        return value

    def validate_signed_agreement(self, value):
        if value and value.size > 10 * 1024 * 1024:
            raise ValidationError("Signed agreement must be under 10MB.")
        return value

    def validate_course_applications(self, value):
        try:
            data = json.loads(value) if isinstance(value, str) else value
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("Invalid JSON for course applications.")
        if not isinstance(data, list):
            raise ValidationError("Course applications must be a list.")
        valid_subjects = [c[0] for c in TeacherProfile.SUBJECT_CHOICES]
        valid_boards = [c[0] for c in TeacherProfile.BOARD_CHOICES]
        valid_classes = [c[0] for c in TeacherProfile.CLASS_CHOICES]
        valid_streams = [c[0] for c in TeacherProfile.STREAM_CHOICES]
        for i, entry in enumerate(data):
            if not entry.get("subject"):
                raise ValidationError(f"Entry {i+1}: Subject is required.")
            if entry["subject"] not in valid_subjects:
                raise ValidationError(f"Entry {i+1}: Invalid subject.")
            boards = entry.get("boards", [])  # optional — the faculty design omits boards
            for b in boards:
                if b not in valid_boards:
                    raise ValidationError(f"Entry {i+1}: Invalid board '{b}'.")
            classes = entry.get("classes", [])
            if not classes:
                raise ValidationError(f"Entry {i+1}: At least one class is required.")
            for c in classes:
                if c not in valid_classes:
                    raise ValidationError(f"Entry {i+1}: Invalid class '{c}'.")
            streams = entry.get("streams", [])
            for st in streams:
                if st not in valid_streams:
                    raise ValidationError(f"Entry {i+1}: Invalid stream '{st}'.")
        return data

    def validate_skill_applications(self, value):
        try:
            data = json.loads(value) if isinstance(value, str) else value
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("Invalid JSON for skill applications.")
        if not isinstance(data, list):
            raise ValidationError("Skill applications must be a list.")
        valid_subjects = [c[0] for c in TeacherProfile.SUBJECT_CHOICES]
        for i, entry in enumerate(data):
            if not entry.get("skill_name", "").strip():
                raise ValidationError(f"Skill {i+1}: Skill name is required.")
            if not entry.get("skill_description", "").strip():
                raise ValidationError(f"Skill {i+1}: Skill description is required.")
            subj = entry.get("skill_related_subject", "")
            if not subj:
                raise ValidationError(f"Skill {i+1}: Related subject is required.")
            if subj not in valid_subjects:
                raise ValidationError(f"Skill {i+1}: Invalid subject.")
        return data


    def validate(self, data):
        # course_applications and skill_applications are already validated
        # by their field-level validators above
        return data


    def update(self, user, validated_data):
        # --- Update Profile (personal + address) ---
        profile = user.default_learner_profile()

        profile_fields = [
            "first_name", "last_name", "phone", "gender",
            "date_of_birth", "state", "district", "city_town", "pin_code",
        ]

        photo = validated_data.pop("profile_photo", None)
        if photo:
            profile.profile_photo = photo

        for field in profile_fields:
            if field in validated_data:
                setattr(profile, field, validated_data[field])
        profile.save()

        # --- Update TeacherProfile ---
        tp, _ = TeacherProfile.objects.get_or_create(user=user)

        teacher_fields = [
            # Educational qualifications
            "highest_degree", "field_of_study", "year_of_completion",
            "teaching_certifications",
            # Teaching experience
            "experience_range", "employment_status", "currently_employed",
            "current_institution", "current_position",
            # Verification
            "govt_id_type", "id_number",
        ]

        for field in teacher_fields:
            if field in validated_data:
                setattr(tp, field, validated_data[field])

        # File fields on TeacherProfile
        for field in ["qualification_certificate", "id_proof_front", "id_proof_back", "signed_agreement"]:
            value = validated_data.get(field)
            if value:
                setattr(tp, field, value)

        tp.save()

        # --- Replace Course Applications ---
        course_apps = validated_data.get("course_applications", [])
        tp.course_applications.all().delete()
        for entry in course_apps:
            TeacherCourseApplication.objects.create(
                teacher_profile=tp,
                subject=entry["subject"],
                boards=entry.get("boards", []),
                classes=entry.get("classes", []),
                streams=entry.get("streams", []),
            )

        # --- Replace Skill Applications ---
        skill_apps = validated_data.get("skill_applications", [])
        request = self.context.get("request")
        tp.skill_applications.all().delete()
        for i, entry in enumerate(skill_apps):
            skill = TeacherSkillApplication.objects.create(
                teacher_profile=tp,
                skill_name=entry["skill_name"],
                skill_description=entry["skill_description"],
                skill_related_subject=entry["skill_related_subject"],
            )
            file_key = f"skill_file_{i}"
            if request and file_key in request.FILES:
                skill.supporting_file = request.FILES[file_key]
                skill.save()

        return user



# =====================================================
# TEACHER LIST SERIALIZER
# =====================================================

class TeacherListSerializer(serializers.Serializer):
    """
    Returns teacher info for the student request form.
    Reads from User + Profile + TeacherProfile.
    """
    id = serializers.UUIDField(source="user.id")
    name = serializers.SerializerMethodField()
    subject = serializers.CharField(source="subject_specialization", default="")
    qualification = serializers.CharField(default="")
    rating = serializers.DecimalField(max_digits=3, decimal_places=2, default=None)
    avatar = serializers.SerializerMethodField()

    def get_name(self, obj):
        profile = default_learner(obj.user)
        if profile:
            if profile.first_name:
                return f"{profile.first_name} {profile.last_name}".strip()
            if profile.full_name:
                return profile.full_name
        return obj.user.get_full_name() or obj.user.username

    def get_avatar(self, obj):
        profile = default_learner(obj.user)
        if profile:
            return profile.avatar_value()
        return None


# =====================================================
# STUDENT VALIDATION SERIALIZER (for group session form)
# =====================================================

class StudentValidationSerializer(serializers.Serializer):
    """Returns basic info when validating a student ID."""
    valid = serializers.BooleanField()
    name = serializers.CharField()
    user_id = serializers.UUIDField()
    student_id = serializers.CharField()


# =====================================================
# CHANGE PASSWORD SERIALIZER
# =====================================================

class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True)

    def validate_new_password(self, value):
        validate_password(value)
        return value


# =====================================================
# PASSWORD RESET SERIALIZERS
# =====================================================

class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)


class PasswordResetVerifySerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    code = serializers.CharField(required=True, min_length=6, max_length=6)


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    ticket = serializers.UUIDField(required=True)
    new_password = serializers.CharField(required=True)

    def validate_new_password(self, value):
        validate_password(value)
        return value


# =====================================================
# ADMIN SERIALIZERS
# =====================================================

class AdminUserListSerializer(serializers.ModelSerializer):
    profile = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()

    def get_profile(self, obj):
        lp = default_learner(obj)
        return ProfileSerializer(lp, context=self.context).data if lp else None

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "username",
            "profile",
            "roles",
            "is_active",
            "is_verified",
            "date_joined",
        )

    def get_roles(self, obj):
        return list(
            obj.user_roles
            .filter(is_active=True)
            .values_list("role__name", flat=True)
        )


class AdminEnrollmentSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    course_title = serializers.CharField(source="course.title")
    batch_code = serializers.CharField(allow_null=True)
    status = serializers.CharField()
    enrolled_at = serializers.DateTimeField()


class AdminUserDetailSerializer(AdminUserListSerializer):
    enrollments = AdminEnrollmentSerializer(many=True, read_only=True)
    last_login = serializers.DateTimeField(read_only=True)

    class Meta(AdminUserListSerializer.Meta):
        fields = AdminUserListSerializer.Meta.fields + ("last_login", "enrollments")


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("is_active", "is_verified")


class TeacherApprovalSerializer(serializers.ModelSerializer):
    user_id = serializers.UUIDField(source="user.id", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()
    requested_at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = UserRole
        fields = ("id", "user_id", "user_email", "user_name", "requested_at")

    def get_user_name(self, obj):
        profile = default_learner(obj.user)
        if profile and profile.full_name:
            return profile.full_name
        return obj.user.username or obj.user.email


class TeacherTrackApprovalSerializer(serializers.ModelSerializer):
    """Track-aware approval row, keyed by TeacherProfile id.

    The admin queue only ever shows the academy (Faculty) track, since the
    skill (Guest) track auto-lists with no review. `id` is the TeacherProfile
    primary key, which the action endpoint accepts.
    """
    user_id = serializers.UUIDField(source="user.id", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()
    requested_at = serializers.DateTimeField(source="created_at", read_only=True)
    track = serializers.SerializerMethodField()
    track_label = serializers.SerializerMethodField()

    # ── Detail + documents (for the admin "View" modal + agreement/ID) ──
    teacher_type = serializers.CharField(read_only=True)
    highest_degree = serializers.CharField(read_only=True)
    field_of_study = serializers.CharField(read_only=True)
    year_of_completion = serializers.IntegerField(read_only=True)
    experience_range = serializers.SerializerMethodField()
    subjects = serializers.SerializerMethodField()
    id_number = serializers.CharField(read_only=True)
    documents = serializers.SerializerMethodField()

    class Meta:
        from .models import TeacherProfile
        model = TeacherProfile
        fields = (
            "id", "user_id", "user_email", "user_name",
            "requested_at", "track", "track_label",
            "teacher_type", "highest_degree", "field_of_study",
            "year_of_completion", "experience_range", "subjects",
            "id_number", "documents",
        )

    def get_user_name(self, obj):
        profile = default_learner(obj.user)
        if profile and profile.full_name:
            return profile.full_name
        return obj.user.username or obj.user.email

    def get_track(self, obj):
        # Academy is the only track that pends; report it explicitly.
        return "academy"

    def get_track_label(self, obj):
        return "Academy (Faculty)"

    def get_experience_range(self, obj):
        return getattr(obj, "experience_range", "") or ""

    def get_subjects(self, obj):
        # Best-effort: pull subjects from the latest course application if present.
        app = getattr(obj, "course_applications", None)
        if app is not None:
            latest = app.order_by("-created_at").first() if hasattr(app, "order_by") else None
            if latest:
                return getattr(latest, "subject", "") or ""
        return getattr(obj, "field_of_study", "") or ""

    def _url(self, request, filefield):
        if not filefield:
            return None
        url = filefield.url
        return request.build_absolute_uri(url) if request else url

    def get_documents(self, obj):
        request = self.context.get("request")
        return {
            "signed_agreement":          self._url(request, obj.signed_agreement),
            "id_proof_front":            self._url(request, obj.id_proof_front),
            "id_proof_back":             self._url(request, obj.id_proof_back),
            "qualification_certificate": self._url(request, obj.qualification_certificate),
        }
