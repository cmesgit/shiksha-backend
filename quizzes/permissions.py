# PLACEMENT: backend/quizzes/permissions.py
#
# Gate for the new admin authoring API (standalone bank questions + the
# tag/rail taxonomy) that feeds the public Quiz Hub. Mirrors
# content/permissions.py's IsStudioEditor almost exactly — same caching
# rationale, same "the flag is a real gate, not just a nav toggle" intent —
# but with one deliberate difference: a non-admin must still get an ordinary
# 403 (that's IsAdmin's job, listed first in every view's permission_classes),
# while an admin hitting a switched-off Hub gets a 503 with a readable
# message, per this task's spec, not a 403 that looks like "you're not
# allowed" when the real answer is "this isn't live yet". DRF only ever
# raises 403 for a permission that returns False, so getting a 503 out of a
# permission class means RAISING an APIException with that status_code
# ourselves rather than returning False — see has_permission() below.

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission


class QuizHubDisabled(APIException):
    """Raised (not returned) from IsPublicQuizHubEnabled.has_permission() so
    the response is 503, not DRF's default 403 for a failed permission."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = (
        "The public Quiz Hub is switched off. Turn on "
        "GlobalSettings.public_quiz_hub_enabled to use this."
    )
    default_code = "quiz_hub_disabled"


class IsPublicQuizHubEnabled(BasePermission):
    """Gate on GlobalSettings.public_quiz_hub_enabled alone.

    Deliberately does NOT also check IsAuthenticated/IsAdmin — every view
    using this lists IsAuthenticated and IsAdmin as separate entries ahead of
    it in `permission_classes`, so a non-admin (or anonymous) caller is
    rejected by one of those with an ordinary 403 before this one ever runs.
    Folding the role check in here too would make "not staff" and "flag off"
    indistinguishable from the response alone.

    Cached exactly like IsStudioEditor: this runs on every request to every
    endpoint under quizzes/admin/bank/ and quizzes/admin/tags/, and
    GlobalSettings.load() is a get_or_create — a DB round trip (an INSERT on
    the very first call ever) we don't want paying on every request. A flip
    of the flag therefore takes up to CACHE_TTL seconds to reach this API,
    which is the same lag IsStudioEditor already accepts.
    """

    CACHE_KEY = "quizzes:public_quiz_hub_enabled"
    CACHE_TTL = 60

    def has_permission(self, request, view):
        if not self.hub_enabled():
            raise QuizHubDisabled()
        return True

    @classmethod
    def hub_enabled(cls):
        from django.core.cache import cache

        # `is None`, not falsy: False is a real cached answer, distinct from
        # "nothing cached yet".
        cached = cache.get(cls.CACHE_KEY)
        if cached is None:
            from global_settings.models import GlobalSettings

            cached = bool(GlobalSettings.load().public_quiz_hub_enabled)
            cache.set(cls.CACHE_KEY, cached, cls.CACHE_TTL)
        return cached
