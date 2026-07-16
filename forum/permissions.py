from rest_framework.permissions import BasePermission


class IsForumModerator(BasePermission):
    """Mirrors accounts.permissions.IsAdmin's is_staff gate, but also accepts
    the MODERATOR role — staff/admins can always do anything a moderator can."""

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and (request.user.is_staff or request.user.has_role("MODERATOR"))
        )
