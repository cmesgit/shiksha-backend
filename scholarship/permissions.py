from rest_framework.permissions import BasePermission

from accounts.auth_flow import get_active_profile


class HasActiveLearnerProfile(BasePermission):
    """The exam is sat by a specific child profile, not the account itself —
    require a learner profile to be selected before touching
    eligibility/exam endpoints (guardian-verification endpoints don't need
    this; they act on the account/parent directly)."""

    message = "Select a learner profile before continuing."

    def has_permission(self, request, view):
        return get_active_profile(request) is not None


class IsOwnExamSession(BasePermission):
    """Object-level check: the active learner profile must match the
    session's owner. Prevents one profile from reading/answering another
    profile's exam by guessing a session id."""

    def has_object_permission(self, request, view, obj):
        learner = get_active_profile(request)
        session = obj if hasattr(obj, "learner_profile") else obj.session
        return learner is not None and session.learner_profile_id == learner.id
