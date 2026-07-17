# PLACEMENT: backend/content/permissions.py

from rest_framework.permissions import BasePermission


class IsContentEditor(BasePermission):
    """Staff-only gate for the CMS admin API — mirrors forum.permissions.IsForumModerator."""

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_staff
