"""
accounts/identity_views.py  ·  Adding a teacher identity from inside the app

    GET  /api/accounts/identities/teacher/   → what this account holds / can add
    POST /api/accounts/identities/teacher/   { track, expert_profile?, faculty_profile? }

WHY THIS REPLACES SIGNUP CASES 3–6
──────────────────────────────────
`SignupSerializer` handles "an existing account wants another identity" by
re-entering signup and re-proving the account password as ownership proof.
That is six of its eight branches, and all six exist only because the caller
might be a stranger.

Here the caller is already authenticated, so there is nothing to prove and no
password to collect. The person is signed in, on a screen inside the product,
choosing to start teaching. That is the whole difference, and it deletes the
branches.

REUSING THE PROVISIONING HELPERS
────────────────────────────────
`_setup_teacher` / `_add_teacher_track` and the `_provision_expert` /
`_provision_faculty` pair they call are methods on `SignupSerializer`, but
none of them touch serializer state — they operate purely on their arguments.
They are called here through a bare instance rather than copied, deliberately:
duplicating roughly 150 lines of ExpertProfile/faculty-field provisioning is
exactly how signup and add-identity would drift apart. When Phase 8 retires
the serializer, these helpers move here rather than being deleted.

WHAT IS STILL ASYMMETRIC, AND SHOULD BE
───────────────────────────────────────
Skill applications auto-approve; academy applications land PENDING for admin
review. That is employment versus a marketplace listing and is intentional.
The OTHER asymmetry — faculty being unable to add the skill track — was policy
only and was removed on 2026-09-06 (see TeacherProfile.can_apply_track).
"""
import logging

from django.db import transaction

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError

from .auth_flow import serialize_teacher
from .models import TeacherProfile
from .permissions import IsEmailVerified

logger = logging.getLogger(__name__)

_TRACKS = (TeacherProfile.TRACK_ACADEMY, TeacherProfile.TRACK_SKILL)


def _track_report(teacher):
    """What this account holds and what it may add — the shape the
    'Teach on ShikshaCom' screen needs to render itself."""
    if teacher is None:
        return {
            "has_teacher_identity": False,
            "tracks": {t: TeacherProfile.TRACK_LOCKED for t in _TRACKS},
            "can_add": {t: True for t in _TRACKS},
            "blocked_reason": {t: "" for t in _TRACKS},
        }
    return {
        "has_teacher_identity": True,
        "tracks": {t: teacher.track_status(t) for t in _TRACKS},
        "can_add": {t: teacher.can_apply_track(t) for t in _TRACKS},
        "blocked_reason": {t: teacher.track_add_block_reason(t) for t in _TRACKS},
    }


class TeacherIdentityView(APIView):
    """Start teaching, or add the track you don't hold yet."""

    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get(self, request):
        teacher = getattr(request.user, "teacher_profile", None)
        body = _track_report(teacher)
        body["teacher"] = serialize_teacher(teacher)
        return Response(body, status=status.HTTP_200_OK)

    def post(self, request):
        track = (request.data.get("track") or "").strip().lower()
        if track not in _TRACKS:
            raise ValidationError({
                "track": f"Choose one of: {', '.join(_TRACKS)}.",
                "code": "bad_track",
            })

        user = request.user
        teacher = getattr(user, "teacher_profile", None)

        if teacher is not None and not teacher.can_apply_track(track):
            return Response(
                {"detail": teacher.track_add_block_reason(track),
                 "code": "track_held"},
                status=status.HTTP_409_CONFLICT,
            )

        expert_payload  = request.data.get("expert_profile")
        faculty_payload = request.data.get("faculty_profile")

        # See the module docstring: reused, not copied, on purpose.
        from .signup_serializer import SignupSerializer
        helper = SignupSerializer()

        with transaction.atomic():
            if teacher is None:
                teacher_type = (
                    TeacherProfile.TYPE_FACULTY
                    if track == TeacherProfile.TRACK_ACADEMY
                    else TeacherProfile.TYPE_GUEST
                )
                helper._setup_teacher(
                    user, teacher_type, expert_payload, faculty_payload,
                )
            else:
                helper._add_teacher_track(
                    user, track, expert_payload, faculty_payload,
                )

        user.refresh_from_db()
        teacher = getattr(user, "teacher_profile", None)

        body = _track_report(teacher)
        body["teacher"] = serialize_teacher(teacher)
        body["track"] = track
        body["status"] = teacher.track_status(track) if teacher else None
        # Skill goes live at once; academy waits on a human.
        body["needs_review"] = body["status"] == TeacherProfile.TRACK_PENDING
        return Response(body, status=status.HTTP_201_CREATED)
