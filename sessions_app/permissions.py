from rest_framework.permissions import BasePermission


class IsTeacher(BasePermission):
    """Allow access only to users who have the TEACHER role."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.has_role("TEACHER")


class IsStudent(BasePermission):
    """Allow access only to users who have the STUDENT role."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.has_role("STUDENT")


class IsSessionParticipant(BasePermission):
    """Allow access to teachers or students who belong to this session."""

    def has_object_permission(self, request, view, obj):
        user = request.user
        if obj.teacher == user or obj.requested_by == user:
            return True
        return obj.participants.filter(user=user).exists()