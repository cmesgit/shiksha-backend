"""Admin RBAC surface: roles, permissions, user↔role assignment, and a
read-only moderator action-history feed.

All endpoints are admin-only (`IsAdmin` == ``is_staff``) and mounted under
``/accounts/admin/``. Moderators use the forum's own panel; admins use these
to *govern* roles and *oversee* moderator activity without acting on content.
"""
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, NotFound

from .models import User, Role, UserRole, Permission, RolePermission
from .permissions import IsAdmin


# Built-in roles that must not be deleted (they carry code-level meaning).
BUILTIN_ROLES = {Role.STUDENT, Role.TEACHER, Role.ADMIN, Role.GUEST, Role.MODERATOR}
# Roles surfaced on the Assignments tab as governable "staff" roles.
STAFF_ROLES = [Role.ADMIN, Role.MODERATOR]


def _int_param(request, key, default, cap):
    try:
        return min(cap, max(1, int(request.query_params.get(key, default))))
    except (TypeError, ValueError):
        return default


def _role_dict(role, perm_counts=None, user_counts=None, include_perms=False):
    data = {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "is_builtin": role.name in BUILTIN_ROLES,
        "permission_count": (
            perm_counts.get(role.id, 0) if perm_counts is not None
            else role.role_permissions.count()
        ),
        "user_count": (
            user_counts.get(role.id, 0) if user_counts is not None
            else UserRole.objects.filter(role=role, is_active=True).count()
        ),
    }
    if include_perms:
        data["permissions"] = list(
            role.role_permissions.values_list("permission__codename", flat=True)
        )
    return data


def _user_mini(u):
    return {
        "id": str(u.id),
        "email": u.email,
        "username": u.username,
    }


# =====================================================
# Roles
# =====================================================
class RoleListCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        roles = list(Role.objects.all())
        perm_counts = dict(
            RolePermission.objects.values("role").order_by()
            .annotate(n=Count("id")).values_list("role", "n")
        )
        user_counts = dict(
            UserRole.objects.filter(is_active=True).values("role").order_by()
            .annotate(n=Count("id")).values_list("role", "n")
        )
        return Response([
            _role_dict(r, perm_counts, user_counts) for r in roles
        ])

    def post(self, request):
        name = (request.data.get("name") or "").strip().upper()
        if not name:
            raise ValidationError({"name": "Role name is required."})
        if len(name) > 20:
            raise ValidationError({"name": "Max 20 characters."})
        if Role.objects.filter(name=name).exists():
            raise ValidationError({"name": "A role with this name already exists."})
        role = Role.objects.create(
            name=name, description=(request.data.get("description") or "").strip()
        )
        # Optional initial permission set.
        codes = request.data.get("permissions")
        if isinstance(codes, list):
            _set_role_permissions(role, codes)
        return Response(_role_dict(role, include_perms=True), status=201)


class RoleDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def _get(self, role_id):
        role = Role.objects.filter(id=role_id).first()
        if not role:
            raise NotFound("Role not found.")
        return role

    def patch(self, request, role_id):
        role = self._get(role_id)
        if "description" in request.data:
            role.description = (request.data.get("description") or "").strip()
        if "name" in request.data and role.name not in BUILTIN_ROLES:
            name = (request.data.get("name") or "").strip().upper()
            if not name:
                raise ValidationError({"name": "Role name is required."})
            if Role.objects.filter(name=name).exclude(id=role.id).exists():
                raise ValidationError({"name": "A role with this name already exists."})
            role.name = name
        role.save()
        return Response(_role_dict(role, include_perms=True))

    def delete(self, request, role_id):
        role = self._get(role_id)
        if role.name in BUILTIN_ROLES:
            raise ValidationError("Built-in roles cannot be deleted.")
        role.delete()
        return Response(status=204)


# =====================================================
# Permissions
# =====================================================
class PermissionListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        grouped = {}
        for p in Permission.objects.all():
            grouped.setdefault(p.category, []).append({
                "codename": p.codename,
                "name": p.name,
                "description": p.description,
            })
        return Response([
            {"category": cat, "permissions": perms}
            for cat, perms in sorted(grouped.items())
        ])


def _set_role_permissions(role, codenames):
    """Replace a role's permission set with the given codenames (validated)."""
    codenames = [c for c in (codenames or []) if c]
    valid = dict(Permission.objects.values_list("codename", "id"))
    unknown = [c for c in codenames if c not in valid]
    if unknown:
        raise ValidationError({"permissions": f"Unknown permissions: {unknown}"})
    RolePermission.objects.filter(role=role).delete()
    RolePermission.objects.bulk_create([
        RolePermission(role=role, permission_id=valid[c]) for c in set(codenames)
    ])


class RolePermissionsView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def _get_role(self, role_id):
        role = Role.objects.filter(id=role_id).first()
        if not role:
            raise NotFound("Role not found.")
        return role

    def get(self, request, role_id):
        role = self._get_role(role_id)
        return Response({
            "role": role.name,
            "permissions": list(
                role.role_permissions.values_list("permission__codename", flat=True)
            ),
        })

    def put(self, request, role_id):
        role = self._get_role(role_id)
        _set_role_permissions(role, request.data.get("permissions", []))
        return Response(_role_dict(role, include_perms=True))


# =====================================================
# User ↔ Role assignment
# =====================================================
class UserRolesView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def _get_user(self, user_id):
        u = User.objects.filter(id=user_id).first()
        if not u:
            raise NotFound("User not found.")
        return u

    def get(self, request, user_id):
        u = self._get_user(user_id)
        rows = (
            UserRole.objects.filter(user=u)
            .select_related("role", "approved_by")
            .order_by("role__name")
        )
        return Response({
            "user": _user_mini(u),
            "roles": [{
                "role": r.role.name,
                "is_active": r.is_active,
                "approved_by": r.approved_by.username if r.approved_by else None,
                "approved_at": r.approved_at,
            } for r in rows],
        })

    def post(self, request, user_id):
        u = self._get_user(user_id)
        name = (request.data.get("role") or "").strip().upper()
        role = Role.objects.filter(name=name).first()
        if not role:
            raise ValidationError({"role": "Unknown role."})
        ur, _ = UserRole.objects.update_or_create(
            user=u, role=role,
            defaults={
                "is_active": True,
                "approved_by": request.user,
                "approved_at": timezone.now(),
            },
        )
        return Response({
            "role": role.name, "is_active": ur.is_active,
            "approved_by": request.user.username, "approved_at": ur.approved_at,
        }, status=201)


class UserRoleDeleteView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def delete(self, request, user_id, role_name):
        u = User.objects.filter(id=user_id).first()
        if not u:
            raise NotFound("User not found.")
        name = (role_name or "").strip().upper()
        # Guard against an admin revoking their own last ADMIN role (lock-out).
        if name == Role.ADMIN and str(request.user.id) == str(user_id):
            raise ValidationError("You cannot revoke your own ADMIN role.")
        ur = UserRole.objects.filter(user=u, role__name=name).first()
        if not ur:
            raise NotFound("User does not hold this role.")
        ur.is_active = False
        ur.save(update_fields=["is_active"])
        return Response(status=204)


class RolesDirectoryView(APIView):
    """Users grouped by governable staff role — powers the Assignments tab."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        out = []
        for name in STAFF_ROLES:
            rows = (
                UserRole.objects.filter(role__name=name, is_active=True)
                .select_related("user", "approved_by")
                .order_by("user__email")
            )
            out.append({
                "role": name,
                "users": [{
                    **_user_mini(r.user),
                    "approved_by": r.approved_by.username if r.approved_by else None,
                    "approved_at": r.approved_at,
                } for r in rows],
            })
        return Response(out)


# =====================================================
# Read-only moderator action history (admin oversight)
# =====================================================
class ModActionsHistoryView(APIView):
    """Paginated, filterable ModerationAction feed for admins. Distinct from
    the forum's own ``mod/log/`` (that one is moderator-gated). Read-only."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        # Imported lazily to avoid a forum→accounts import at module load.
        from forum.models import ModerationAction
        from forum.moderation_views import _LOG_META, _log_row_text

        qs = ModerationAction.objects.select_related(
            "moderator", "target_user"
        ).order_by("-created_at")

        action = (request.query_params.get("action") or "").strip()
        if action:
            qs = qs.filter(action=action)

        moderator = (request.query_params.get("moderator") or "").strip()
        if moderator:
            qs = qs.filter(
                Q(moderator__username__icontains=moderator)
                | Q(moderator__email__icontains=moderator)
            )

        since = request.query_params.get("since")
        if since:
            dt = parse_datetime(since) or parse_date(since)
            if dt:
                qs = qs.filter(created_at__gte=dt)
        until = request.query_params.get("until")
        if until:
            dt = parse_datetime(until) or parse_date(until)
            if dt:
                qs = qs.filter(created_at__lte=dt)

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 25, 100)
        total = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start:start + page_size])

        # Resolve GenericForeignKey targets in bulk (avoid per-row queries).
        by_ct = {}
        for a in rows:
            if a.content_type_id and a.object_id:
                by_ct.setdefault(a.content_type_id, []).append(a.object_id)
        targets = {}
        for ct_id, ids in by_ct.items():
            model = ContentType.objects.get(pk=ct_id).model_class()
            if model is None:
                continue
            for obj in model.objects.filter(pk__in=ids):
                targets[(ct_id, obj.pk)] = obj

        results = []
        for a in rows:
            target = targets.get((a.content_type_id, a.object_id))
            action_type, label = _LOG_META.get(a.action, ("ok", a.get_action_display()))
            results.append({
                "id": a.id,
                "action": a.action,
                "type": action_type,
                "label": label,
                "text": _log_row_text(a, target),
                "note": a.note,
                "moderator": a.moderator.username if a.moderator else "—",
                "target_user": a.target_user.username if a.target_user else None,
                "created_at": a.created_at,
            })
        return Response({"results": results, "count": total, "page": page, "page_size": page_size})
