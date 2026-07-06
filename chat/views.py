# PLACEMENT: backend/backend/chat/views.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/views.py
"""
chat/views.py — REST endpoints (history, conversation list, starting chats,
the people directory used to start a NEW chat, and block / unblock).

Live delivery is over the websocket (see consumers.py); REST covers the rest.
Both paths funnel sends through services.post_message_checked(), so policy,
moderation, and blocking are enforced identically no matter how a message is
posted. StartDirectView additionally gates on policy.can_start_dm() (Phase 3
§10) before a 1:1 conversation is even created — a check-then-create shape,
the same one CourseRoomView already uses for room membership.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework import status
from django.utils import timezone
from django.db.models import Q

from accounts.models import LearnerProfile, TeacherProfile
from .models import Conversation, Participant, Message
from . import services
from . import policy


def _require_identity(request):
    kind, obj = services.active_identity_from_request(request)
    if not kind:
        raise PermissionDenied(
            "Select a learner profile or enter teacher mode before using chat."
        )
    return kind, obj


def _my_conversations(kind, obj):
    if kind == Participant.KIND_LEARNER:
        part_q = Q(participants__kind=kind, participants__learner_profile=obj)
    else:
        part_q = Q(participants__kind=kind, participants__teacher_profile=obj)
    return Conversation.objects.filter(part_q).distinct()


class ConversationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        kind, obj = _require_identity(request)
        convs = _my_conversations(kind, obj).prefetch_related("participants", "messages")
        out = []
        for c in convs:
            me = services.participant_for(c, kind, obj)
            out.append(services.serialize_conversation(c, me))
        # Newest activity first.
        out.sort(key=lambda c: c.get("last_message_at") or "", reverse=True)
        return Response(out)


class StartDirectView(APIView):
    """
    POST { target_kind: "TEACHER"|"LEARNER", target_id }
    Starts (or returns) a 1:1 conversation between the active identity and
    the target identity.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        kind, obj = _require_identity(request)
        target_kind = request.data.get("target_kind")
        target_id = request.data.get("target_id")
        if target_kind not in (Participant.KIND_LEARNER, Participant.KIND_TEACHER):
            raise ValidationError({"target_kind": "Must be LEARNER or TEACHER."})

        if target_kind == Participant.KIND_TEACHER:
            # Accept either a TeacherProfile id OR a User id. The Academy
            # teacher pages (Teachers/TeacherDetail) key off user.id, while the
            # skill cards & directory pass teacher_profile.id — both resolve here.
            target = TeacherProfile.objects.filter(id=target_id).first()
            if not target:
                target = TeacherProfile.objects.filter(user__id=target_id).first()
        else:
            target = LearnerProfile.objects.filter(id=target_id, is_active=True).first()
        if not target:
            raise ValidationError({"target_id": "Target not found."})

        # Don't allow starting a thread with yourself.
        if kind == target_kind and str(getattr(obj, "id", "")) == str(target_id):
            raise ValidationError({"target_id": "You cannot message yourself."})

        # M3 (Phase 3 §10): the DM matrix — same check-then-create shape as
        # CourseRoomView's can_join_course_room check below.
        allowed, reason = policy.can_start_dm(kind, obj, target_kind, target)
        if not allowed:
            raise PermissionDenied(reason)

        conv = services.ensure_direct(kind, obj, target_kind, target)
        me = services.participant_for(conv, kind, obj)
        return Response(
            services.serialize_conversation(conv, me),
            status=status.HTTP_201_CREATED,
        )


class CourseRoomView(APIView):
    """
    POST { course_id, title? }  — join (or create) the course's group room.
    Membership check is delegated to the caller's app context; here we simply
    attach the active identity as a participant.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        kind, obj = _require_identity(request)
        course_id = request.data.get("course_id")
        if not course_id:
            raise ValidationError({"course_id": "Required."})
        # Only enrolled learners and the course's subject teachers may join.
        if not services.can_join_course_room(kind, obj, course_id):
            raise PermissionDenied(
                "This class chat is only for students enrolled in the course and "
                "its teachers."
            )
        conv = services.ensure_course_room(course_id, request.data.get("title", ""))
        services._attach_participant(conv, kind, obj)
        me = services.participant_for(conv, kind, obj)
        return Response(services.serialize_conversation(conv, me))


class MessageListView(APIView):
    """GET /chat/conversations/<id>/messages/?before=<iso>&limit=50"""
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        kind, obj = _require_identity(request)
        conv = Conversation.objects.filter(id=conversation_id).first()
        if not conv:
            raise ValidationError("Conversation not found.")
        me = services.participant_for(conv, kind, obj)
        if not me:
            raise PermissionDenied("You are not a participant in this conversation.")

        qs = conv.messages.all()
        before = request.query_params.get("before")
        if before:
            qs = qs.filter(created_at__lt=before)
        try:
            limit = min(int(request.query_params.get("limit", 50)), 100)
        except ValueError:
            limit = 50
        msgs = list(qs.order_by("-created_at")[:limit])[::-1]
        return Response([services.serialize_message(m) for m in msgs])


class MarkReadView(APIView):
    """POST /chat/conversations/<id>/read/ — set last_read_at = now."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        kind, obj = _require_identity(request)
        conv = Conversation.objects.filter(id=conversation_id).first()
        if not conv:
            raise ValidationError("Conversation not found.")
        me = services.participant_for(conv, kind, obj)
        if not me:
            raise PermissionDenied("Not a participant.")
        me.last_read_at = timezone.now()
        me.save(update_fields=["last_read_at"])
        services.redis_utils.clear_unread(me.identity_key(), conv.id)
        return Response({"detail": "ok", "last_read_at": me.last_read_at.isoformat()})


# ===========================================================================
# NEW — people directory (start a new chat) + blocking
# ===========================================================================

def _teacher_display_name(tp):
    """Mirror ExpertProfile/Participant naming so the directory matches the rest
    of the app: prefer the teacher's SELF learner-profile name, then username/
    email. Never raises."""
    try:
        u = tp.user
        lp = u.default_learner_profile()
        if lp:
            name = f"{(lp.first_name or '').strip()} {(lp.last_name or '').strip()}".strip()
            if name:
                return name
            if getattr(lp, "full_name", ""):
                return lp.full_name
            if getattr(lp, "display_name", ""):
                return lp.display_name
        return u.username or u.email
    except Exception:
        return "Teacher"


def _role_label(roles):
    if "faculty" in roles and "guest" in roles:
        return "Faculty · Guest expert"
    if "guest" in roles:
        return "Guest expert"
    if "faculty" in roles:
        return "Faculty"
    return "Teacher"


class DirectoryView(APIView):
    """
    GET /chat/directory/?q=<search>

    People a user can start a brand-new 1:1 chat with from the inbox. For
    privacy we only surface the public-facing teacher identities:
      • listed guest experts (ExpertProfile.is_listed = True)
      • approved faculty teachers (academy_status = approved)

    A teacher who is both is returned once with both roles merged. The acting
    identity is excluded. Student↔student conversations are not seeded here —
    those happen via course rooms or by replying to an existing thread.

    Item shape (matches StartDirectView's contract):
      { target_kind: "TEACHER", target_id, name, roles, role_label,
        subtitle, avatar }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        kind, obj = _require_identity(request)
        q = (request.query_params.get("q") or "").strip().lower()

        # tp.id -> assembled entry (deduped, roles merged via _teacher_roles)
        entries = {}

        # 1) Listed guest experts (carry headline + expert photo for nicer cards)
        try:
            from skills.models import ExpertProfile
            experts = (
                ExpertProfile.objects
                .filter(is_listed=True)
                .select_related("teacher_profile", "teacher_profile__user")
            )
            for ep in experts:
                tp = ep.teacher_profile
                if not tp:
                    continue
                roles = services._teacher_roles(tp)
                avatar = None
                try:
                    if ep.photo:
                        avatar = ep.photo.url
                    elif tp.photo:
                        avatar = tp.photo.url
                except Exception:
                    avatar = None
                entries[str(tp.id)] = {
                    "target_kind": Participant.KIND_TEACHER,
                    "target_id": str(tp.id),
                    "name": _teacher_display_name(tp),
                    "roles": roles,
                    "role_label": _role_label(roles),
                    "subtitle": (ep.headline or _role_label(roles)),
                    "avatar": avatar,
                }
        except Exception:
            pass

        # 2) Approved faculty teachers
        try:
            faculty = (
                TeacherProfile.objects
                .filter(academy_status=TeacherProfile.TRACK_APPROVED)
                .select_related("user")
            )
            for tp in faculty:
                key = str(tp.id)
                if key in entries:
                    # Already added as an expert — make sure "faculty" is present.
                    if "faculty" not in entries[key]["roles"]:
                        entries[key]["roles"].append("faculty")
                        entries[key]["role_label"] = _role_label(entries[key]["roles"])
                    continue
                roles = services._teacher_roles(tp)
                avatar = None
                try:
                    if tp.photo:
                        avatar = tp.photo.url
                except Exception:
                    avatar = None
                entries[key] = {
                    "target_kind": Participant.KIND_TEACHER,
                    "target_id": key,
                    "name": _teacher_display_name(tp),
                    "roles": roles,
                    "role_label": _role_label(roles),
                    "subtitle": _role_label(roles),
                    "avatar": avatar,
                }
        except Exception:
            pass

        # Exclude self (a teacher shouldn't see their own card).
        if kind == Participant.KIND_TEACHER:
            entries.pop(str(getattr(obj, "id", "")), None)

        out = list(entries.values())
        if q:
            out = [e for e in out if q in e["name"].lower()
                   or q in (e["subtitle"] or "").lower()]
        out.sort(key=lambda e: e["name"].lower())
        return Response(out[:50])


class BlockListView(APIView):
    """
    GET  /chat/blocks/                       — list everyone I've blocked.
    POST /chat/blocks/  { target_kind, target_id }
         — block someone (server enforces the platform rule via can_block()).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        kind, obj = _require_identity(request)
        out = []
        for b in services.my_blocks(kind, obj):
            if b.blocked_kind == Participant.KIND_LEARNER and b.blocked_learner_id:
                out.append({
                    "target_kind": Participant.KIND_LEARNER,
                    "target_id": str(b.blocked_learner_id),
                    "name": b.blocked_learner.display_name,
                    "created_at": b.created_at.isoformat(),
                })
            elif b.blocked_kind == Participant.KIND_TEACHER and b.blocked_teacher_id:
                out.append({
                    "target_kind": Participant.KIND_TEACHER,
                    "target_id": str(b.blocked_teacher_id),
                    "name": _teacher_display_name(b.blocked_teacher),
                    "created_at": b.created_at.isoformat(),
                })
        return Response(out)

    def post(self, request):
        kind, obj = _require_identity(request)
        target_kind = request.data.get("target_kind")
        target_id = request.data.get("target_id")
        if target_kind not in (Participant.KIND_LEARNER, Participant.KIND_TEACHER):
            raise ValidationError({"target_kind": "Must be LEARNER or TEACHER."})

        # Platform permission rule.
        if not services.can_block(kind, target_kind):
            raise PermissionDenied(
                "Students can block other students, but not faculty or guest experts."
            )

        target = services.resolve_identity(target_kind, target_id)
        if not target:
            raise ValidationError({"target_id": "Target not found."})
        if kind == target_kind and str(getattr(obj, "id", "")) == str(target_id):
            raise ValidationError({"target_id": "You cannot block yourself."})

        services.create_block(kind, obj, target_kind, target)
        return Response({"detail": "blocked"}, status=status.HTTP_201_CREATED)


class UnblockView(APIView):
    """POST /chat/blocks/remove/ { target_kind, target_id } — lift a block."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        kind, obj = _require_identity(request)
        target_kind = request.data.get("target_kind")
        target_id = request.data.get("target_id")
        if target_kind not in (Participant.KIND_LEARNER, Participant.KIND_TEACHER):
            raise ValidationError({"target_kind": "Must be LEARNER or TEACHER."})
        services.remove_block(kind, getattr(obj, "id", None), target_kind, target_id)
        return Response({"detail": "unblocked"})
