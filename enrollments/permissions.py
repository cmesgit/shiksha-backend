from rest_framework.permissions import BasePermission

from accounts.auth_flow import get_active_profile
from .models import Enrollment


class IsEnrolledInCourse(BasePermission):
    """Allow access only if the ACTIVE learner profile holds an active
    enrollment in the course named by the URL (`course_id`)."""

    def has_permission(self, request, view):
        course_id = view.kwargs.get("course_id")
        if not course_id:
            return True

        learner = get_active_profile(request)
        if learner is None:
            return False

        return Enrollment.objects.filter(
            learner_profile=learner,
            course__id=course_id,
            status="ACTIVE",
        ).exists()
