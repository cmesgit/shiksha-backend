from rest_framework.permissions import BasePermission


class IsForumModerator(BasePermission):
    """Grants access to forum moderation surfaces.

    A user qualifies if they are staff/admin, hold the RBAC permission
    ``forum.moderate``, or still carry the legacy MODERATOR role (kept as a
    safety fallback during the RBAC rollout — the seeded MODERATOR role holds
    ``forum.moderate`` so both paths converge).
    """

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and (
                u.is_staff
                or u.has_permission("forum.moderate")
                or u.has_role("MODERATOR")
            )
        )


def HasForumPermission(codename):
    """Factory building a DRF permission class for a single RBAC codename.

    Staff always pass. Usage::

        permission_classes = [HasForumPermission("forum.reports.view")]
    """

    class _HasPerm(BasePermission):
        message = f"Missing required permission: {codename}"

        def has_permission(self, request, view):
            u = request.user
            return bool(
                u
                and u.is_authenticated
                and (u.is_staff or u.has_permission(codename))
            )

    _HasPerm.__name__ = f"HasForumPermission_{codename.replace('.', '_')}"
    return _HasPerm
