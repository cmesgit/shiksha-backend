from rest_framework.permissions import BasePermission


class IsDocumentsModerator(BasePermission):
    """Grants access to the Explore-library moderation surfaces.

    A user qualifies if they are staff/admin, hold the RBAC permission
    ``documents.moderate``, or carry the legacy MODERATOR role (kept as a
    fallback — the seeded MODERATOR role holds ``documents.moderate`` so both
    paths converge). Mirrors forum.permissions.IsForumModerator.
    """

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and (
                u.is_staff
                or u.has_permission("documents.moderate")
                or u.has_role("MODERATOR")
            )
        )


def HasDocumentsPermission(codename):
    """Factory building a DRF permission class for a single RBAC codename.
    Staff always pass. Usage::

        permission_classes = [HasDocumentsPermission("documents.reports.view")]
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

    _HasPerm.__name__ = f"HasDocumentsPermission_{codename.replace('.', '_')}"
    return _HasPerm
