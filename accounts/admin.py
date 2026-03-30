from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Profile, Role, UserRole, TeacherProfile


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
    list_display = ("user", "full_name", "phone", "student_id", "date_of_birth", "is_complete")
    search_fields = ("user__email", "full_name", "student_id", "phone")
    list_filter = ("same_as_current",)

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

@admin.register(TeacherProfile)
class TeacherProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "gender",
        "highest_qualification",
        "subject_specialization",
        "teaching_experience_years",
        "is_approved",
        "is_complete",
    )

    list_filter = ("is_approved", "gender", "highest_qualification")
    search_fields = ("user__email", "subject_specialization", "father_name")

    def is_complete(self, obj):
        return obj.is_complete
    is_complete.boolean = True
