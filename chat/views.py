# PLACEMENT: backend/backend/chat/views.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/chat/views.py
"""
chat/views.py — REST endpoints for the Communication Center.

Live delivery is over the websocket (see consumers.py); REST covers
everything else: history, the conversation list, starting chats, the
people directory, blocking, and — Stage B/C/D of the Communication Center
gap-analysis closure — attachments, reactions, soft delete, conversation
management (pin/archive/mute/report), course-hub composition
(members/resources/assignments/announcements), academic support tickets,
per-identity communication preferences, search, and the admin moderation
console (reports queue, message removal, suspensions, platform broadcast,
support-ticket assignment, and basic logs).

Every mutation funnels through the SAME chat/services.py gate
(post_message_checked / post_attachment_checked / toggle_reaction /
soft_delete_message / ...) no matter which endpoint calls it, so policy,
moderation, blocking, and suspension are enforced identically everywhere —
exactly the guarantee the M0-era docstring above this file already
described for text sends.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework import status
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta

from accounts.models import LearnerProfile, TeacherProfile
from accounts.permissions import IsAdmin
from .models import (
    Conversation, Participant, Message, MessageReaction, Report,
    ChatSuspension, CommPreference, SupportTicket,
)
from . import services
from . import policy
from . import realtime


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


def _get_conv_and_me(request, conversation_id):
    """Shared by every "act on a conversation I'm already in" endpoint
    (pin/archive/mute/report/members/search/attachments/react/delete).
    Raises the same errors _require_identity()/MessageListView already use,
    so error handling stays uniform across all of them."""
    kind, obj = _require_identity(request)
    conv = Conversation.objects.filter(id=conversation_id).first()
    if not conv:
        raise ValidationError("Conversation not found.")
    me = services.participant_for(conv, kind, obj)
    if not me:
        raise PermissionDenied("You are not a participant in this conversation.")
    return conv, me


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
        conv, me = _get_conv_and_me(request, conversation_id)

        qs = conv.messages.select_related(
            "sender", "reply_to", "reply_to__sender", "attachment",
        ).prefetch_related("reactions__participant")
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
        conv, me = _get_conv_and_me(request, conversation_id)
        me.last_read_at = timezone.now()
        me.save(update_fields=["last_read_at"])
        services.redis_utils.clear_unread(me.identity_key(), conv.id)
        # Same fan-out the WS "read" frame triggers, so a reply typed from
        # the REST-only mobile app (or a page that hasn't opened the socket
        # yet) still flips the sender's read-receipt live.
        realtime.push_conversation_event(conv.id, "chat.read", {
            "identity": me.identity_key(), "last_read_at": me.last_read_at.isoformat(),
        })
        return Response({"detail": "ok", "last_read_at": me.last_read_at.isoformat()})


# ===========================================================================
# Conversation management — CC-006 card actions (Stage B)
# ===========================================================================

class PinConversationView(APIView):
    """POST /chat/conversations/<id>/pin/ { pinned? } — toggles if omitted."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        want = request.data.get("pinned")
        me.pinned = (not me.pinned) if want is None else bool(want)
        me.save(update_fields=["pinned"])
        return Response(services.serialize_conversation(conv, me))


class ArchiveConversationView(APIView):
    """POST /chat/conversations/<id>/archive/ { archived? } — toggles if
    omitted. This IS the CC-006 "Delete" action — see chat/models.py's
    Participant.archived_at docstring for why nothing is ever destroyed."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        want = request.data.get("archived")
        if want is None:
            want = me.archived_at is None
        me.archived_at = timezone.now() if want else None
        me.save(update_fields=["archived_at"])
        return Response(services.serialize_conversation(conv, me))


class MuteConversationView(APIView):
    """POST /chat/conversations/<id>/mute/
    { unmute: true } | { forever: true } | { minutes: N } | { until: iso }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        data = request.data or {}
        if data.get("unmute"):
            me.muted_until = None
        elif data.get("forever"):
            # A concrete far-future date rather than datetime.max — avoids
            # timezone-conversion overflow edge cases while still reading as
            # "indefinitely" to any human or UI checking the value.
            me.muted_until = timezone.now() + timedelta(days=3650)
        elif data.get("minutes"):
            try:
                minutes = int(data["minutes"])
            except (TypeError, ValueError):
                raise ValidationError({"minutes": "Must be a number."})
            me.muted_until = timezone.now() + timedelta(minutes=minutes)
        elif data.get("until"):
            from django.utils.dateparse import parse_datetime
            parsed = parse_datetime(data["until"])
            if not parsed:
                raise ValidationError({"until": "Must be an ISO datetime."})
            me.muted_until = parsed
        else:
            raise ValidationError({"detail": "Provide unmute, forever, minutes, or until."})
        me.save(update_fields=["muted_until"])
        return Response(services.serialize_conversation(conv, me))


class ReportView(APIView):
    """POST /chat/conversations/<id>/report/ { message_id?, reason, detail? }
    CC-006 (report a whole thread/person) and CC-010 (report one message)
    share this endpoint — message_id is what tells them apart."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        reason = request.data.get("reason")
        if reason not in dict(Report.REASON_CHOICES):
            raise ValidationError({"reason": f"Must be one of {list(dict(Report.REASON_CHOICES))}."})

        message = None
        message_id = request.data.get("message_id")
        target_identity = ""
        if message_id:
            message = conv.messages.filter(id=message_id).first()
            if not message:
                raise ValidationError({"message_id": "Not found."})
            if message.sender_id:
                target_identity = message.sender.identity_key()
        elif conv.kind == Conversation.KIND_DIRECT:
            other = services.other_participant(conv, me)
            if other:
                target_identity = other.identity_key()

        report = Report.objects.create(
            conversation=conv, message=message, reporter=me,
            target_identity=target_identity,
            reason=reason, detail=(request.data.get("detail") or "")[:2000],
        )
        return Response({"detail": "reported", "id": str(report.id)}, status=status.HTTP_201_CREATED)


class ConversationMembersView(APIView):
    """GET /chat/conversations/<id>/members/ — CC-014. Trivial: Participant
    rows already exist for every course room; this just lists them with
    roles."""
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        members = [
            services._participant_dict(p)
            for p in conv.participants.select_related("learner_profile", "teacher_profile", "staff_user").all()
        ]
        return Response(members)


class ConversationSearchView(APIView):
    """GET /chat/conversations/<id>/search/?q= — CC-002 (per-conversation
    first, per the gap analysis's own recommended order)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        q = (request.query_params.get("q") or "").strip()
        if not q:
            return Response([])
        msgs = (
            conv.messages.filter(deleted_at__isnull=True, body__icontains=q)
            .select_related("sender", "reply_to", "reply_to__sender", "attachment")
            .order_by("-created_at")[:50]
        )
        return Response([services.serialize_message(m) for m in msgs])


class GlobalSearchView(APIView):
    """GET /chat/search/?q= — CC-002 across my own conversations, messages,
    and the people directory. Plain icontains, not Postgres FTS — the gap
    analysis itself flags FTS as a later upgrade ("message search
    (per-conversation first; global later — consider Postgres FTS)"); this
    is the "global later" half, done with a portable query so it works
    identically in this sandbox's SQLite and the real deployment's Postgres.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        kind, obj = _require_identity(request)
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"conversations": [], "messages": [], "people": []})

        convs = list(_my_conversations(kind, obj).prefetch_related("participants"))
        conv_hits = []
        for c in convs:
            me = services.participant_for(c, kind, obj)
            data = services.serialize_conversation(c, me)
            title = data.get("title") or ""
            if q.lower() in title.lower():
                conv_hits.append(data)

        msg_qs = (
            Message.objects.filter(conversation__in=convs, deleted_at__isnull=True, body__icontains=q)
            .select_related("conversation", "sender")
            .order_by("-created_at")[:30]
        )
        msg_hits = []
        for m in msg_qs:
            entry = services.serialize_message(m)
            entry["conversation_title"] = m.conversation.title or ""
            msg_hits.append(entry)

        people_hits = services.directory_entries(kind, obj, q)

        return Response({
            "conversations": conv_hits[:20],
            "messages": msg_hits,
            "people": people_hits[:20],
        })


# ===========================================================================
# Attachments — CC-010/012/016 (Stage C)
# ===========================================================================

class ConversationAttachmentUploadView(APIView):
    """
    GET  /chat/conversations/<id>/attachments/   — CC-016 Shared Resources:
         every file ever sent in this conversation, newest first. Works for
         ANY conversation kind (a DIRECT thread's shared photos, not just a
         course room's) — the Course Hub's own "Resources" tab is a
         different, richer view over materials.StudyMaterial specifically;
         this is the simpler "what's been sent in this thread" gallery.
    POST /chat/conversations/<id>/attachments/   (multipart: file, caption?, reply_to?)
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        items = (
            conv.attachments.filter(message__deleted_at__isnull=True)
            .select_related("message", "uploaded_by")
            .order_by("-created_at")[:200]
        )
        return Response([{
            "id": str(a.id),
            "message_id": str(a.message_id),
            "url": a.file.url if a.file else None,
            "name": a.filename(),
            "kind": a.kind,
            "size": a.size_bytes,
            "uploaded_by": a.uploaded_by.display_name() if a.uploaded_by else "Unknown",
            "created_at": a.created_at.isoformat(),
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        } for a in items])

    def post(self, request, conversation_id):
        conv, me = _get_conv_and_me(request, conversation_id)
        django_file = request.FILES.get("file")
        if not django_file:
            raise ValidationError({"file": "Required."})
        msg, error = services.post_attachment_checked(
            conv, me, django_file,
            caption=request.data.get("caption", ""),
            reply_to_id=request.data.get("reply_to") or None,
        )
        if error:
            return Response(error, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response(services.serialize_message(msg), status=status.HTTP_201_CREATED)


# ===========================================================================
# Reactions & delete — CC-007/010 (Stage B)
# ===========================================================================

class ReactToMessageView(APIView):
    """POST /chat/messages/<id>/react/ { emoji } — toggles."""
    permission_classes = [IsAuthenticated]

    def post(self, request, message_id):
        kind, obj = _require_identity(request)
        msg = Message.objects.select_related("conversation").filter(id=message_id).first()
        if not msg:
            raise ValidationError("Message not found.")
        me = services.participant_for(msg.conversation, kind, obj)
        if not me:
            raise PermissionDenied("You are not a participant in this conversation.")
        emoji = request.data.get("emoji")
        if emoji not in services.ALLOWED_REACTIONS:
            raise ValidationError({"emoji": f"Must be one of {sorted(services.ALLOWED_REACTIONS)}."})

        action, summary = services.toggle_reaction(msg, me, emoji)
        realtime.push_conversation_event(msg.conversation_id, "chat.reaction", {
            "message_id": str(msg.id), "reactions": summary,
            "actor": me.identity_key(), "emoji": emoji, "action": action,
        })
        return Response({"action": action, "reactions": summary})


class DeleteMessageView(APIView):
    """POST /chat/messages/<id>/delete/ — soft delete (sender, or a TEACHER
    moderating their own ROOM). Admin removal is a separate, IsAdmin-gated
    endpoint below (AdminRemoveMessageView) — see soft_delete_message()."""
    permission_classes = [IsAuthenticated]

    def post(self, request, message_id):
        kind, obj = _require_identity(request)
        msg = Message.objects.select_related("conversation", "sender").filter(id=message_id).first()
        if not msg:
            raise ValidationError("Message not found.")
        me = services.participant_for(msg.conversation, kind, obj)
        if not me:
            raise PermissionDenied("You are not a participant in this conversation.")
        if not services.can_delete_message(me, msg):
            raise PermissionDenied("You can only delete your own messages.")

        services.soft_delete_message(msg, participant=me)
        realtime.push_conversation_event(msg.conversation_id, "chat.message_deleted", {"id": str(msg.id)})
        return Response({"detail": "deleted"})


# ===========================================================================
# Communication preferences — CC-020/021 (Stage E)
# ===========================================================================

class CommPreferenceView(APIView):
    """GET/PUT /chat/preferences/ — the acting identity's own online-status
    / read-receipt toggles."""
    permission_classes = [IsAuthenticated]

    def _payload(self, pref):
        return {
            "show_online_status": pref.show_online_status,
            "show_read_receipts": pref.show_read_receipts,
        }

    def get(self, request):
        kind, obj = _require_identity(request)
        pref = CommPreference.for_identity(services.identity_key_for_ids(kind, obj.id))
        return Response(self._payload(pref))

    def put(self, request):
        kind, obj = _require_identity(request)
        pref = CommPreference.for_identity(services.identity_key_for_ids(kind, obj.id))
        data = request.data or {}
        for field in ("show_online_status", "show_read_receipts"):
            if field in data:
                if not isinstance(data[field], bool):
                    raise ValidationError({field: "Must be a boolean."})
                setattr(pref, field, data[field])
        pref.save()
        return Response(self._payload(pref))


# ===========================================================================
# Course Hub composition — CC-013/014/016 (Stage C)
# ===========================================================================

class CourseResourcesView(APIView):
    """GET /chat/course/<course_id>/resources/ — Course Hub "Resources" tab.
    Composition over materials.StudyMaterial; the data already existed, this
    is purely the read-side join the gap analysis called "largely a
    composition/frontend gap"."""
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        kind, obj = _require_identity(request)
        if not services.can_join_course_room(kind, obj, course_id):
            raise PermissionDenied("This course's resources are only visible to its students and teachers.")
        return Response(services.course_resources(course_id))


class CourseAssignmentsForHubView(APIView):
    """GET /chat/course/<course_id>/assignments/ — Course Hub "Assignments"
    tab (same composition idea as Resources above, over assignments.Assignment)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        kind, obj = _require_identity(request)
        if not services.can_join_course_room(kind, obj, course_id):
            raise PermissionDenied("This course's assignments are only visible to its students and teachers.")
        return Response(services.course_assignments_summary(course_id))


# ===========================================================================
# Announcements — CC-015 (Stage D, wires the reserved BROADCAST kind)
# ===========================================================================

class CourseAnnouncementsView(APIView):
    """
    GET  /chat/course/<course_id>/announcements/  — feed (students + teachers)
    POST /chat/course/<course_id>/announcements/  { body } — faculty-only create
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        kind, obj = _require_identity(request)
        if not services.can_join_course_room(kind, obj, course_id):
            raise PermissionDenied("Announcements for this course are only visible to its students and teachers.")
        conv = services.ensure_course_announcements(course_id)
        services._attach_participant(conv, kind, obj)
        me = services.participant_for(conv, kind, obj)
        msgs = list(
            conv.messages.filter(deleted_at__isnull=True)
            .select_related("sender", "attachment")
            .prefetch_related("reactions__participant")
            .order_by("-created_at")[:50]
        )[::-1]
        return Response({
            "conversation": services.serialize_conversation(conv, me),
            "messages": [services.serialize_message(m) for m in msgs],
        })

    def post(self, request, course_id):
        kind, obj = _require_identity(request)
        if kind != Participant.KIND_TEACHER or not services.teacher_in_course(obj, course_id):
            raise PermissionDenied("Only this course's teachers can post announcements.")
        conv = services.ensure_course_announcements(course_id)
        services._attach_participant(conv, kind, obj)
        me = services.participant_for(conv, kind, obj)
        msg, error = services.post_message_checked(conv, me, request.data.get("body", ""))
        if error:
            return Response(error, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if not msg:
            raise ValidationError({"body": "Required."})
        return Response(services.serialize_message(msg), status=status.HTTP_201_CREATED)


# ===========================================================================
# Academic Support tickets — CC-022 (Stage D, wires the reserved SUPPORT kind)
# ===========================================================================

def _ticket_and_participant(request, ticket_id):
    ticket = SupportTicket.objects.select_related("conversation").filter(id=ticket_id).first()
    if not ticket:
        raise ValidationError("Ticket not found.")
    kind, obj = services.active_identity_from_request(request)
    if kind:
        me = services.participant_for(ticket.conversation, kind, obj)
        if me:
            return ticket, me
    if request.user.is_staff:
        me = services.attach_staff_participant(ticket.conversation, request.user)
        return ticket, me
    raise PermissionDenied("You don't have access to this ticket.")


class SupportTicketListCreateView(APIView):
    """GET  /chat/support/tickets/  — my tickets (learner or teacher).
    POST /chat/support/tickets/  { subject, category, message } — open one."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        kind, obj = _require_identity(request)
        filters = {"requester_kind": kind}
        if kind == Participant.KIND_LEARNER:
            filters["requester_learner"] = obj
        else:
            filters["requester_teacher"] = obj
        qs = SupportTicket.objects.filter(**filters).select_related("conversation", "assignee")
        return Response([services.serialize_ticket(t) for t in qs])

    def post(self, request):
        kind, obj = _require_identity(request)
        subject = (request.data.get("subject") or "").strip()
        if not subject:
            raise ValidationError({"subject": "Required."})
        category = request.data.get("category", SupportTicket.CATEGORY_OTHER)
        if category not in dict(SupportTicket.CATEGORY_CHOICES):
            category = SupportTicket.CATEGORY_OTHER
        ticket, error = services.create_support_ticket(
            kind, obj, subject, category, request.data.get("message", ""),
        )
        if error:
            return Response(error, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response(services.serialize_ticket(ticket), status=status.HTTP_201_CREATED)


class SupportTicketMessagesView(APIView):
    """GET /chat/support/tickets/<id>/messages/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, ticket_id):
        ticket, me = _ticket_and_participant(request, ticket_id)
        msgs = (
            ticket.conversation.messages.filter(deleted_at__isnull=True)
            .select_related("sender", "attachment")
            .order_by("created_at")
        )
        return Response([services.serialize_message(m) for m in msgs])


class SupportTicketReplyView(APIView):
    """POST /chat/support/tickets/<id>/reply/ { message }"""
    permission_classes = [IsAuthenticated]

    def post(self, request, ticket_id):
        ticket, me = _ticket_and_participant(request, ticket_id)
        msg, error = services.post_message_checked(ticket.conversation, me, request.data.get("message", ""))
        if error:
            return Response(error, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if not msg:
            raise ValidationError({"message": "Required."})
        if me.kind == Participant.KIND_STAFF and ticket.status == SupportTicket.STATUS_OPEN:
            ticket.status = SupportTicket.STATUS_IN_PROGRESS
            ticket.save(update_fields=["status"])
        return Response(services.serialize_message(msg), status=status.HTTP_201_CREATED)


class SupportTicketCloseView(APIView):
    """POST /chat/support/tickets/<id>/close/ — requester or staff."""
    permission_classes = [IsAuthenticated]

    def post(self, request, ticket_id):
        ticket, me = _ticket_and_participant(request, ticket_id)
        ticket.status = SupportTicket.STATUS_CLOSED
        ticket.closed_at = timezone.now()
        ticket.save(update_fields=["status", "closed_at"])
        return Response(services.serialize_ticket(ticket))


# ===========================================================================
# NEW — people directory (start a new chat) + blocking
# ===========================================================================

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
        q = request.query_params.get("q") or ""
        return Response(services.directory_entries(kind, obj, q)[:50])


class ProfileView(APIView):
    """GET /chat/profile/<kind>/<id>/ — CC-019 User Profile. Reachable from
    a DIRECT thread's header, the Course Hub members list, and the
    Directory — see build_profile()'s docstring for the privacy stance."""
    permission_classes = [IsAuthenticated]

    def get(self, request, kind, target_id):
        _require_identity(request)  # must be a resolvable chat identity to browse profiles at all
        if kind not in (Participant.KIND_LEARNER, Participant.KIND_TEACHER):
            raise ValidationError({"kind": "Must be LEARNER or TEACHER."})
        profile = services.build_profile(kind, target_id)
        if not profile:
            raise ValidationError("Profile not found.")
        return Response(profile)


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
                    "name": services.teacher_display_name(b.blocked_teacher),
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


# ===========================================================================
# Administrator Console — CC-023 (Stage D)
# ===========================================================================
# Every view below is gated on accounts.permissions.IsAdmin (request.user.is_staff),
# the same permission src_admin's other endpoints already use.

class AdminReportsQueueView(APIView):
    """GET /chat/admin/reports/?status=OPEN — the moderation queue."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = Report.objects.select_related("conversation", "message", "reporter").order_by("-created_at")
        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response([services.serialize_report(r) for r in qs[:200]])


class AdminResolveReportView(APIView):
    """POST /chat/admin/reports/<id>/resolve/ { action, note? }
    action ∈ remove_message | suspend_user | dismiss"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, report_id):
        report = Report.objects.select_related("conversation", "message").filter(id=report_id).first()
        if not report:
            raise ValidationError("Report not found.")
        action = request.data.get("action")
        note = request.data.get("note", "")

        if action == "remove_message":
            if not report.message_id:
                raise ValidationError({"action": "This report has no target message."})
            services.soft_delete_message(
                report.message, participant=None,
                admin_reason=note or "Removed by a moderator",
            )
            realtime.push_conversation_event(
                report.conversation_id, "chat.message_deleted", {"id": str(report.message_id)},
            )
            report.status = Report.STATUS_ACTION_TAKEN
        elif action == "suspend_user":
            if not report.target_identity:
                raise ValidationError({"action": "This report has no clear target user."})
            services.suspend_identity(
                report.target_identity, note or "Suspended following a report.",
                created_by=request.user, until=request.data.get("until"),
            )
            report.status = Report.STATUS_ACTION_TAKEN
        elif action == "dismiss":
            report.status = Report.STATUS_DISMISSED
        else:
            raise ValidationError({"action": "Must be remove_message, suspend_user, or dismiss."})

        report.resolved_by = request.user
        report.resolution_note = note
        report.resolved_at = timezone.now()
        report.save(update_fields=["status", "resolved_by", "resolution_note", "resolved_at"])
        return Response(services.serialize_report(report))


class AdminRemoveMessageView(APIView):
    """POST /chat/admin/messages/<id>/remove/ { reason? } — standalone
    removal, independent of a filed Report."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, message_id):
        msg = Message.objects.select_related("conversation").filter(id=message_id).first()
        if not msg:
            raise ValidationError("Message not found.")
        services.soft_delete_message(
            msg, participant=None,
            admin_reason=request.data.get("reason") or "Removed by a moderator",
        )
        realtime.push_conversation_event(msg.conversation_id, "chat.message_deleted", {"id": str(msg.id)})
        return Response({"detail": "removed"})


class AdminSuspensionsView(APIView):
    """GET  /chat/admin/suspensions/            — active + past.
    POST /chat/admin/suspensions/ { identity_key, reason?, until? } — create."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = ChatSuspension.objects.select_related("created_by").order_by("-created_at")
        return Response([{
            "id": str(s.id),
            "identity_key": s.identity_key,
            "reason": s.reason,
            "suspended_until": s.suspended_until.isoformat() if s.suspended_until else None,
            "is_active": s.is_active(),
            "created_by": (s.created_by.get_full_name() or s.created_by.username) if s.created_by else None,
            "created_at": s.created_at.isoformat(),
        } for s in qs[:200]])

    def post(self, request):
        identity_key = request.data.get("identity_key")
        if not identity_key or ":" not in identity_key:
            raise ValidationError({"identity_key": 'Required, format "L:<id>" / "T:<id>".'})
        obj = services.suspend_identity(
            identity_key, request.data.get("reason", ""),
            created_by=request.user, until=request.data.get("until"),
        )
        return Response({
            "id": str(obj.id), "identity_key": obj.identity_key,
            "suspended_until": obj.suspended_until.isoformat() if obj.suspended_until else None,
        }, status=status.HTTP_201_CREATED)


class AdminLiftSuspensionView(APIView):
    """POST /chat/admin/suspensions/<identity_key>/lift/"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, identity_key):
        services.lift_suspension(identity_key)
        return Response({"detail": "lifted"})


class AdminBroadcastView(APIView):
    """POST /chat/admin/broadcast/ { audience, title, body, link_url? }
    audience ∈ all | all_students | all_teachers"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        title = (request.data.get("title") or "").strip()
        if not title:
            raise ValidationError({"title": "Required."})
        audience = request.data.get("audience", "all")
        count = services.send_admin_broadcast(
            audience, title, request.data.get("body", ""),
            request.data.get("link_url", ""), actor=request.user,
        )
        return Response({"detail": "sent", "recipients": count})


class AdminSupportTicketsView(APIView):
    """GET /chat/admin/support/tickets/?status=OPEN"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = SupportTicket.objects.select_related("conversation", "assignee").order_by("-updated_at")
        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response([services.serialize_ticket(t) for t in qs[:200]])


class AdminAssignTicketView(APIView):
    """POST /chat/admin/support/tickets/<id>/assign/ { assignee_id }"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, ticket_id):
        ticket = SupportTicket.objects.filter(id=ticket_id).first()
        if not ticket:
            raise ValidationError("Ticket not found.")
        assignee_id = request.data.get("assignee_id")
        assignee = None
        if assignee_id:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            assignee = User.objects.filter(id=assignee_id, is_staff=True).first()
            if not assignee:
                raise ValidationError({"assignee_id": "Must be an existing staff user."})
        ticket.assignee = assignee
        if assignee and ticket.status == SupportTicket.STATUS_OPEN:
            ticket.status = SupportTicket.STATUS_IN_PROGRESS
        ticket.save(update_fields=["assignee", "status"])
        return Response(services.serialize_ticket(ticket))


class AdminTicketStatusView(APIView):
    """POST /chat/admin/support/tickets/<id>/status/ { status }"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, ticket_id):
        ticket = SupportTicket.objects.filter(id=ticket_id).first()
        if not ticket:
            raise ValidationError("Ticket not found.")
        new_status = request.data.get("status")
        if new_status not in dict(SupportTicket.STATUS_CHOICES):
            raise ValidationError({"status": f"Must be one of {list(dict(SupportTicket.STATUS_CHOICES))}."})
        ticket.status = new_status
        if new_status in (SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED):
            ticket.closed_at = timezone.now()
        ticket.save(update_fields=["status", "closed_at"])
        return Response(services.serialize_ticket(ticket))


class AdminLogsView(APIView):
    """GET /chat/admin/logs/ — lightweight comms analytics (CC-023). Not a
    full export pipeline — a first, honest pass at the numbers a moderator
    actually opens this page to check."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        data = {
            "messages_today": Message.objects.filter(created_at__gte=today_start).count(),
            "messages_last_7_days": Message.objects.filter(created_at__gte=week_start).count(),
            "active_conversations_last_7_days": Conversation.objects.filter(last_message_at__gte=week_start).count(),
            "open_reports": Report.objects.filter(status=Report.STATUS_OPEN).count(),
            "open_support_tickets": SupportTicket.objects.exclude(
                status__in=[SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED]
            ).count(),
            "active_suspensions": ChatSuspension.objects.filter(
                Q(suspended_until__isnull=True) | Q(suspended_until__gt=now)
            ).count(),
            "generated_at": now.isoformat(),
        }
        return Response(data)
