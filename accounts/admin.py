from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Profile, Role, UserRole, TeacherProfile, TeacherCourseApplication, TeacherSkillApplication


# =========================
# USER ADMIN
# =========================

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    model = User

    list_display = (
        "email",
        "username",
        "is_verified",
        "is_staff",
        "is_active",
    )

    list_filter = (
        "is_verified",
        "is_staff",
        "is_active",
    )

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("username",)}),
        ("Verification", {"fields": ("is_verified", "verified_at")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_staff",
                    "is_active",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    ordering = ("email",)
    search_fields = ("email", "username")


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "first_name",
        "last_name",
        "phone",
        "student_id",
        "state",
        "currently_studying",
        "current_class",
        "date_of_birth",
        "is_complete",
    )
    search_fields = (
        "user__email",
        "first_name",
        "last_name",
        "full_name",
        "student_id",
        "phone",
    )
    list_filter = (
        "gender",
        "state",
        "currently_studying",
        "current_class",
        "board",
        "stream",
    )

    fieldsets = (
        ("Personal Info", {
            "fields": (
                "user", "first_name", "last_name", "full_name",
                "phone", "gender", "date_of_birth", "profile_photo",
                "student_id",
            )
        }),
        ("Avatar", {
            "fields": ("avatar_image", "avatar_emoji"),
            "classes": ("collapse",),
        }),
        ("Address", {
            "fields": ("state", "district", "city_town", "pin_code"),
        }),
        ("Parent/Guardian (Student)", {
            "fields": (
                "father_name", "father_phone",
                "mother_name", "mother_phone",
                "guardian_name", "guardian_phone",
                "parent_guardian_email",
            ),
            "classes": ("collapse",),
        }),
        ("Academic Info (Student)", {
            "fields": (
                "currently_studying", "current_class", "stream",
                "board", "board_other", "school_name", "academic_year",
                "highest_education", "reason_not_studying",
            ),
            "classes": ("collapse",),
        }),
        ("Legacy Fields", {
            "fields": (
                "current_address", "permanent_address", "same_as_current",
                "guardian",
            ),
            "classes": ("collapse",),
        }),
    )

    def is_complete(self, obj):
        return obj.is_complete
    is_complete.boolean = True


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name",)


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "is_active", "approved_by", "approved_at")


# =========================
# TEACHER PROFILE ADMIN
# =========================
class TeacherCourseApplicationInline(admin.TabularInline):
    model = TeacherCourseApplication
    extra = 0


class TeacherSkillApplicationInline(admin.TabularInline):
    model = TeacherSkillApplication
    extra = 0


@admin.register(TeacherProfile)
class TeacherProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "highest_degree",
        "field_of_study",
        "experience_range",
        "employment_status",
        "is_approved",
        "is_complete",
    )

    list_filter = (
        "is_approved",
        "highest_degree",
        "experience_range",
        "employment_status",
        "govt_id_type",
    )
    search_fields = (
        "user__email",
        "field_of_study",
        "id_number",
        "skill_name",
    )

    inlines = [TeacherCourseApplicationInline, TeacherSkillApplicationInline]

    fieldsets = (
        ("User", {
            "fields": ("user", "is_approved"),
        }),
        ("Legacy Display Fields", {
            "fields": ("qualification", "bio", "photo", "rating"),
            "classes": ("collapse",),
        }),
        ("Educational Qualifications", {
            "fields": (
                "highest_degree", "field_of_study", "year_of_completion",
                "teaching_certifications", "qualification_certificate",
            ),
        }),
        ("Teaching Experience", {
            "fields": (
                "experience_range", "employment_status",
                "currently_employed", "current_institution", "current_position",
            ),
        }),
        ("Verification Documents", {
            "fields": (
                "govt_id_type", "id_number",
                "id_proof_front", "id_proof_back",
            ),
        }),
        
        ("Course & Skill Applications (Legacy - see inlines below)", {
            "fields": ("subject", "boards", "classes", "streams",
                       "skill_name", "skill_description", "skill_related_subject",
                       "skill_supporting_image", "skill_supporting_video"),
            "classes": ("collapse",),
        }),


        ("Legacy Form Fillup Fields", {
            "fields": (
                "gender", "date_of_birth",
                "father_name", "father_phone",
                "mother_name", "mother_phone",
                "current_address", "permanent_address", "same_as_current",
                "highest_qualification", "other_qualification",
                "subject_specialization", "teaching_experience_years",
                "previous_institution",
            ),
            "classes": ("collapse",),
        }),
    )

    def is_complete(self, obj):
        return obj.is_complete
    is_complete.boolean = True
