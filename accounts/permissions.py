from rest_framework.permissions import BasePermission
from rest_framework.exceptions import PermissionDenied

# The JWT `context` claim value that means "acting inside the teacher
# dashboard". Minted by accounts.auth_flow.TeacherContextView after the Step-2B
# account-password gate; kept in sync with accounts.auth_flow.CTX_TEACHER.
CTX_TEACHER = "teacher"


def _in_teacher_context(request):
    """True iff the caller is an authenticated TEACHER whose token is in
    TEACHER context. `has_role` alone is NOT enough: on a teacher's account a
    learner-context token (e.g. a child on a shared device) also passes the
    role check, so teacher endpoints must additionally require the context
    claim that the Step-2B password gate sets."""
    user = request.user
    if not (user and user.is_authenticated and user.has_role("TEACHER")):
        return False
    token = getattr(request, "auth", None)
    return bool(token) and token.get("context") == CTX_TEACHER


class IsTeacherContext(BasePermission):
    """Role TEACHER *and* an active teacher-context token. Use on teacher-only
    class-based views (replaces a bare IsTeacher)."""

    message = "Switch to your teacher profile to access this."

    def has_permission(self, request, view):
        return _in_teacher_context(request)


def require_teacher_context(request):
    """Inline guard for views that gate teachers in the method body via
    has_role. Raises 403 unless the caller is a teacher in teacher context.
    Drop-in replacement for `if not user.has_role("TEACHER"): raise/return 403`."""
    if not _in_teacher_context(request):
        raise PermissionDenied("Switch to your teacher profile to do this.")


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
