from rest_framework.permissions import BasePermission


class IsEmailVerified(BasePermission):
    message = "Email is not verified."

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.is_verified
        )


class IsStudent(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.has_role("STUDENT")
        )


class IsTeacher(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.has_role("TEACHER")
        )


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.is_staff
        )
