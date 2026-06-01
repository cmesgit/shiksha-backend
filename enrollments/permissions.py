from django.utils import timezone
from rest_framework.permissions import BasePermission

from .models import Enrollment, Subscription


class IsEnrolledInCourse(BasePermission):
    """Grants access only to users with an ACTIVE, non-expired subscription
    for the course identified by ``course_id`` in the URL kwargs.

    Originally this only checked Enrollment.status=ACTIVE, which left expired
    trial / paid users with full access. It now checks the Subscription table,
    which is the source of truth for time-bounded access.

    NOTE ON THE NO-`course_id` CASE:
      Returns False when the route has no ``course_id`` in scope (safer
      default — list routes that should be open to any logged-in user should
      use IsAuthenticated instead).
    """

    message = "Your subscription for this course has expired."

    allow_when_no_course = False

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False

        course_id = view.kwargs.get("course_id")
        if not course_id:
            return self.allow_when_no_course

        return Subscription.objects.filter(
            user=request.user,
            course__id=course_id,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=timezone.now(),
        ).exists()


class HasActiveSubscription(BasePermission):
    """Object-level permission for ACTION endpoints that don't carry a
    ``course_id`` in their URL kwargs (e.g. ``/quizzes/<id>/start/``).

    The view must populate ``view.get_course()`` returning the relevant Course
    instance, OR derive the course from the URL object itself and pass it
    explicitly via ``request.course`` before this check runs.

    Falls back to ``IsAuthenticated`` semantics if the view can't determine
    a course (deny).
    """

    message = "Your subscription for this course has expired."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False

        course = getattr(view, "_course_for_access_check", None) or getattr(request, "course", None)
        if course is None and hasattr(view, "get_course"):
            try:
                course = view.get_course()
            except Exception:
                return False
        if course is None:
            return False

        return Subscription.objects.filter(
            user=request.user,
            course=course,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=timezone.now(),
        ).exists()
