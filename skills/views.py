# PLACEMENT: skills/views.py  (replace the whole file)
# validates it's open, stores slot_key + a real scheduled_for, and locks it.
"""
PLACEMENT: backend/backend/skills/views.py
ACTION:    Replace the entire file.

Change from original:
  CreateOrderView.post() was storing the entire sessionDraft dict as a string
  via note=str(request.data.get("draft") or ""), producing raw Python repr
  like "{'topic': 'Intro to the skill', 'slot': None, ...}" in the session
  topic field.

  Fixed: _extract_note() extracts a clean "Topic: X. Requested slot: Y."
  sentence from the draft dict instead.
"""
from django.db import transaction
from django.utils import timezone
import datetime

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from accounts.models import LearnerProfile, Role, UserRole
from accounts.permissions import IsAdmin
from accounts.auth_flow import get_active_profile, profile_mismatch_response

# Slot bookkeeping lives next to the teacher-facing availability views so the
# booking flow and the expert's own grid share one source of truth.
from .teacher_views import slot_is_open, mark_slot_booked
from .notifications import push_skill_bell

from .models import (
    SkillCategory,
    ExpertProfile,
    TeacherApplication,
    InterviewSlot,
    Interview,
    Evaluation,
    SkillSession,
)
from .marketing_models import SkillMarketingBlock
from .serializers import (
    SkillCategorySerializer,
    SkillCategoryAdminSerializer,
    SkillMarketingBlockSerializer,
    SkillMarketingBlockAdminSerializer,
    ExpertCardSerializer,
    TeacherApplicationCreateSerializer,
    InterviewSlotSerializer,
    ReviewQueueSerializer,
    EvaluationSerializer,
    SkillSessionSerializer,
)


# =====================================================
# HELPERS
# =====================================================

def _extract_note(draft):
    """
    Build a clean human-readable note from the sessionDraft object the
    frontend sends via POST /skill/payments/create-order/.

    draft shape: { topic, note, slotLabel, date, time, duration_mins, expertId, ... }

    Returns a plain string like "Topic: React hooks. Requested slot: Mon 23 · 6 PM."
    Falls back gracefully when fields are missing.
    """
    if not draft:
        return ""

    # Safeguard: if it somehow arrives as a string already, pass it through
    if isinstance(draft, str):
        return draft[:200]

    parts = []

    topic = (draft.get("topic") or "").strip()
    # Skip generic default topics that add no information
    generic = {"intro to the skill", "1-on-1 session with", "intro to"}
    if topic and not any(topic.lower().startswith(g) for g in generic):
        parts.append(f"Topic: {topic}.")

    slot_label = (draft.get("slotLabel") or "").strip()
    if slot_label and slot_label.lower() not in ("none", "null", ""):
        parts.append(f"Requested slot: {slot_label}.")
    elif draft.get("date") and draft.get("time"):
        parts.append(f"Requested: {draft['date']} · {draft['time']}.")

    duration = draft.get("duration_mins")
    if duration and int(duration) != 60:
        parts.append(f"Duration: {duration} min.")

    # If we got nothing useful, use the raw note field as fallback
    if not parts:
        raw = (draft.get("note") or "").strip()
        if raw:
            return raw[:200]
        if topic:
            return topic[:200]

    return " ".join(parts)


# Hours that the booking grid's slot indices map to. Must stay in lock-step
# with SLOTS in the frontend availability.js: ["9 AM","11 AM","2 PM","4 PM","6 PM","8 PM"].
_SLOT_HOURS = [9, 11, 14, 16, 18, 20]


def _slot_to_datetime(slot_key):
    """
    Turn a weekly grid key "<dayIndex>-<slotIndex>" (e.g. "3-1") into a concrete
    timezone-aware datetime in the *current* week. dayIndex 0 = Monday .. 5 = Sat.

    The grid is weekly-recurring, so if the resolved time has already passed this
    week we roll it forward to the same slot next week. Returns None for an
    unparseable / out-of-range key so booking can still proceed without a time.
    """
    if not slot_key:
        return None
    try:
        di, si = (int(x) for x in slot_key.split("-"))
    except (ValueError, AttributeError):
        return None
    if not (0 <= di <= 6) or not (0 <= si < len(_SLOT_HOURS)):
        return None

    now = timezone.localtime()
    monday = (now - datetime.timedelta(days=now.weekday())).date()
    target_date = monday + datetime.timedelta(days=di)
    naive = datetime.datetime.combine(target_date, datetime.time(hour=_SLOT_HOURS[si]))
    dt = timezone.make_aware(naive, timezone.get_current_timezone())
    if dt < now:
        dt += datetime.timedelta(days=7)
    return dt


# =====================================================
# DIRECTORY (public)
# =====================================================

class CategoryListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
        from django.db.models.functions import Coalesce

        # An expert counts once for a category they teach, whether through
        # their primary `category` or the `categories` M2M — the same OR the
        # directory's ?cat= filter uses, so the count matches what clicking it
        # actually returns. It has to be a DISTINCT subquery rather than two
        # added Counts: listing writes mirror the primary category into the
        # M2M, so summing the two would double every expert.
        per_category = (
            ExpertProfile.objects
            .filter(Q(category_id=OuterRef("pk")) | Q(categories=OuterRef("pk")), is_listed=True)
            .order_by()
            .values(one=Value(1))
            .annotate(n=Count("pk", distinct=True))
            .values("n")
        )
        qs = SkillCategory.objects.filter(is_active=True).annotate(
            expert_count=Coalesce(
                Subquery(per_category, output_field=IntegerField()), Value(0)
            )
        )
        return Response(
            SkillCategorySerializer(qs, many=True, context={"request": request}).data
        )


class MarketingBlockListView(APIView):
    """GET /skill/marketing/  → active marketing blocks keyed by `key`."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = SkillMarketingBlock.objects.filter(is_active=True)
        data = SkillMarketingBlockSerializer(qs, many=True, context={"request": request}).data
        return Response({row["key"]: row for row in data})


# NOTE: the public directory view that used to live here (ExpertListView) moved
# to skills/directory_views.py in the Skill Browse redesign. It matched ONLY
# `headline` on ?search= and returned every expert unpaginated; the replacement
# adds the ten filters the sidebar exposes, widens search to skill tags /
# subject / listing titles / teacher name, and paginates. Route is unchanged.
# Its two helpers (_rank_experts, _apply_location_filter) went with it — the
# advertised-first pass now lives in directory_views._ORDER plus its Python
# split, and the location filters are ten lines of directory_views.get().


class ExpertDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, expert_id):
        expert = (
            ExpertProfile.objects
            .filter(id=expert_id, is_listed=True)
            .select_related("category", "teacher_profile__user")
            .first()
        )
        if not expert:
            raise NotFound("Expert not found.")
        return Response(ExpertCardSerializer(expert, context={"request": request}).data)


# =====================================================
# LEARNER REGISTRATION
# =====================================================

class StudentRegisterView(APIView):
    """
    Guest-student entry for the skill feature. Account creation itself goes
    through accounts signup; this just guarantees the logged-in account has
    at least one learner profile to book sessions with.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        profile = user.learner_profiles.filter(is_active=True).first()
        if not profile:
            profile = LearnerProfile.objects.create(
                account=user,
                display_name=user.username or "Learner",
                relationship=LearnerProfile.RELATIONSHIP_SELF,
                is_default=not user.learner_profiles.exists(),
            )
        return Response({"ok": True, "profile_id": str(profile.id)}, status=status.HTTP_201_CREATED)


# =====================================================
# TEACHER APPLICATION + SCREENING
# =====================================================

class TeacherApplicationCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TeacherApplicationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = serializer.save(
            user=request.user,
            status=TeacherApplication.STATUS_SUBMITTED,
        )
        return Response(
            {"ok": True, "applicationId": str(application.id), "status": application.status},
            status=status.HTTP_201_CREATED,
        )


class InterviewSlotListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = [s for s in InterviewSlot.objects.filter(is_active=True) if s.is_open]
        return Response(InterviewSlotSerializer(qs, many=True).data)


class ScheduleInterviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, application_id):
        application = (
            TeacherApplication.objects
            .filter(id=application_id, user=request.user)
            .first()
        )
        if not application:
            raise NotFound("Application not found.")

        slot_id = request.data.get("slot")
        slot = None
        scheduled_for = None
        if slot_id:
            # Early un-locked pre-check for a clean error; the authoritative
            # capacity check happens under a row lock inside the transaction.
            slot = InterviewSlot.objects.filter(id=slot_id, is_active=True).first()
            if not slot or not slot.is_open:
                raise ValidationError({"slot": "That slot is no longer available."})
            scheduled_for = slot.starts_at
        else:
            scheduled_for = request.data.get("scheduled_for")
            if not scheduled_for:
                raise ValidationError("A slot or scheduled_for is required.")

        with transaction.atomic():
            if slot:
                # Lock the slot row so a capacity check + booked_count increment
                # is atomic. Without this, two applicants both read
                # booked_count < capacity and both increment, over-booking a
                # capped slot past its capacity.
                slot = InterviewSlot.objects.select_for_update().get(id=slot.id)
                if not slot.is_open:
                    raise ValidationError({"slot": "That slot is no longer available."})
            interview, _ = Interview.objects.update_or_create(
                application=application,
                defaults={"slot": slot, "scheduled_for": scheduled_for},
            )
            if slot:
                slot.booked_count += 1
                slot.save(update_fields=["booked_count"])
            application.status = TeacherApplication.STATUS_INTERVIEW_SCHEDULED
            application.save(update_fields=["status", "updated_at"])

        return Response({"ok": True, "scheduled_for": interview.scheduled_for})


# =====================================================
# ADMIN: reviewer queue + evaluation
# =====================================================

class ReviewQueueView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (
            TeacherApplication.objects
            .exclude(status=TeacherApplication.STATUS_REJECTED)
            .select_related("category", "user", "interview")
            .order_by("-created_at")
        )
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        return Response(ReviewQueueSerializer(qs, many=True, context={"request": request}).data)


class SubmitEvaluationView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, application_id):
        application = (
            TeacherApplication.objects
            .select_related("user")
            .filter(id=application_id)
            .first()
        )
        if not application:
            raise NotFound("Application not found.")

        interview = getattr(application, "interview", None)
        if not interview:
            interview = Interview.objects.create(
                application=application, scheduled_for=timezone.now(),
                status=Interview.STATUS_COMPLETED,
            )

        serializer = EvaluationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        evaluation = serializer.save(interview=interview, evaluator=request.user)

        decision = evaluation.decision
        if decision == Evaluation.DECISION_APPROVE:
            application.status = TeacherApplication.STATUS_APPROVED
            self._approve_expert(application, evaluation)
        elif decision == Evaluation.DECISION_HOLD:
            application.status = TeacherApplication.STATUS_HOLD
        else:
            application.status = TeacherApplication.STATUS_REJECTED

        application.reviewed_by = request.user
        application.decided_at = timezone.now()
        application.save(update_fields=["status", "reviewed_by", "decided_at", "updated_at"])
        interview.status = Interview.STATUS_COMPLETED
        interview.save(update_fields=["status"])

        return Response({"ok": True, "status": application.status})

    def _approve_expert(self, application, evaluation):
        user = application.user
        tp = getattr(user, "teacher_profile", None)
        if tp is None:
            raise ValidationError(
                "Applicant has no TeacherProfile; complete teacher onboarding first."
            )

        tp.is_approved = True
        if evaluation.recommended_tier:
            tp.tier = evaluation.recommended_tier
        TP = tp.__class__
        if tp.teacher_type == TP.TYPE_FACULTY:
            tp.teacher_type = TP.TYPE_BOTH
        elif not tp.teacher_type:
            tp.teacher_type = TP.TYPE_GUEST
        tp.save(update_fields=["is_approved", "tier", "teacher_type"])

        rate_band = {
            Evaluation.TIER_STANDARD: 40000,
            Evaluation.TIER_SENIOR:   50000,
            Evaluation.TIER_EXPERT:   60000,
        }
        ExpertProfile.objects.update_or_create(
            teacher_profile=tp,
            defaults={
                "category":     application.category,
                "headline":     application.headline or application.skill_name,
                "skill_tags":   application.skill_tags or [],
                "bio":          tp.bio or application.method_note,
                "hourly_rate":  rate_band.get(evaluation.recommended_tier, 35000),
                "is_listed":    True,
            },
        )

        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        ur, _ = UserRole.objects.get_or_create(user=user, role=teacher_role)
        if not ur.is_active:
            ur.approve(self.request.user)


# =====================================================
# SESSIONS + PAYMENT
# =====================================================

class SessionRequestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        learner = get_active_profile(request)
        if learner is None:
            raise PermissionDenied("Select a learner profile before requesting a session.")
        mismatch = profile_mismatch_response(request, request.data.get("active_profile_id"))
        if mismatch is not None:
            return mismatch

        serializer = SkillSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = serializer.save(
            learner_profile=learner,
            status=SkillSession.STATUS_REQUESTED,
        )
        push_skill_bell(session, "requested")
        return Response(
            {"ok": True, "sessionId": str(session.id)},
            status=status.HTTP_201_CREATED,
        )


class CreateOrderView(APIView):
    """
    Book a session — payment is DIRECT (P2P) between the learner and the expert.

    The platform never collects session money. We confirm the booking and hand
    back the expert's own payee details (`pay_to`) plus the rate, so the learner
    can pay the expert directly and the two coordinate over chat. The expert
    later marks the session complete. (Course purchases work the same way — see
    course_views.CourseEnrollView.)

    `payment_status` stays UNPAID because the platform can't observe an
    off-platform transfer; it is not a gate on joining or completing.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        learner = get_active_profile(request)
        if learner is None:
            raise PermissionDenied("Select a learner profile first.")
        mismatch = profile_mismatch_response(request, request.data.get("active_profile_id"))
        if mismatch is not None:
            return mismatch

        expert_id = request.data.get("teacherId") or request.data.get("expert")
        if not expert_id:
            raise ValidationError("expert/teacherId is required.")

        expert = ExpertProfile.objects.filter(id=expert_id, is_listed=True).first()
        if not expert:
            raise NotFound("Expert not found.")

        draft = request.data.get("draft")
        slot_key = ""
        if isinstance(draft, dict):
            slot_key = (draft.get("slot") or "").strip()

        # Reserve the chosen slot. The grid the learner picked from is the
        # expert's `availability_slots` (served by ExpertAvailabilityView), so we
        # validate against the same source of truth before locking it. NOTE: this
        # is an early, un-locked pre-check purely for a fast/clean error before we
        # do any work; the AUTHORITATIVE check+book happens under a row lock inside
        # the transaction below (two learners can both pass this pre-check
        # simultaneously — the locked re-check is what actually prevents a
        # double-booking / JSONField read-modify-write clobber).
        if slot_key and not slot_is_open(expert, slot_key):
            raise ValidationError(
                {"slot": "That time slot is no longer available. Please pick another."}
            )

        scheduled_for = _slot_to_datetime(slot_key)

        duration = 60
        if isinstance(draft, dict):
            try:
                duration = int(draft.get("duration_mins") or 60)
            except (TypeError, ValueError):
                duration = 60

        # WHICH skill is being booked. Multi-skill experts price each listing
        # separately, so the listing — not the profile's legacy hourly_rate —
        # decides what the learner owes. Falls back to the primary listing so a
        # client that predates multi-skill still books something real, and to
        # the expert rate when they have no listing at all.
        listing_id = request.data.get("listing") or (
            draft.get("listing") if isinstance(draft, dict) else None
        )
        listing = None
        if listing_id:
            listing = expert.listings.filter(id=listing_id).first()
            if not listing:
                raise NotFound("That skill isn't offered by this teacher.")
            if not listing.is_bookable:
                raise ValidationError(
                    {"listing": "That skill isn't taking bookings right now."}
                )
        else:
            listing = expert.listings.filter(
                is_active=True, is_suspended=False
            ).order_by("order").first()

        # The rate the learner owes the expert directly (paise on the model).
        amount = listing.price_paise if listing else (expert.hourly_rate or 0)

        with transaction.atomic():
            # Lock the expert row for the whole check-then-book critical section.
            # `availability_slots` is a JSONField whose "booked" list we read,
            # mutate and write back — without this lock two concurrent learners
            # both read {"open":[...],"booked":[]}, both pass slot_is_open, both
            # append and save, and one write clobbers the other (or both bookings
            # succeed for the same slot). Re-checking slot_is_open on the LOCKED
            # instance closes that window.
            if slot_key:
                expert = ExpertProfile.objects.select_for_update().get(id=expert.id)
                if not slot_is_open(expert, slot_key):
                    raise ValidationError(
                        {"slot": "That time slot is no longer available. Please pick another."}
                    )
            session = SkillSession.objects.create(
                learner_profile=learner,
                expert=expert,
                listing=listing,
                contact_mode=SkillSession.CONTACT_SESSION,
                # A booking is a REQUEST until the expert accepts it. It must
                # land in the expert's "Pending requests" queue (status
                # 'requested'), NOT be auto-confirmed — the expert chooses to
                # accept (→ confirmed) or decline (→ cancelled, slot released).
                status=SkillSession.STATUS_REQUESTED,
                # Platform does not collect — settlement is direct (P2P).
                payment_status=SkillSession.PAYMENT_UNPAID,
                amount=amount,
                note=_extract_note(draft),
                slot_key=slot_key,
                scheduled_for=scheduled_for,
                duration_mins=duration,
            )
            # Lock the slot so it greys out for every other learner. (Released
            # again on decline/complete so the weekly grid stays reusable.)
            if slot_key:
                mark_slot_booked(expert, slot_key)

        push_skill_bell(session, "requested")

        booking_ref = f"SHK-{session.id.hex[:8].upper()}"

        return Response({
            "ok":            True,
            "bookingId":     booking_ref,
            "sessionId":     str(session.id),
            "listing":       str(listing.id) if listing else None,
            "listing_title": listing.title if listing else None,
            "status":        session.status,   # 'requested' — awaiting expert acceptance
            "amount":        amount,
            "amount_rupees": amount // 100,
            # Direct settlement details for the learner.
            "settlement":    "direct",
            "pay_to":        expert.pay_to(),
            "expert_teacher_id": str(expert.teacher_profile_id),  # to open chat
            "slot_key":      slot_key,
            "scheduled_for": scheduled_for,
        }, status=status.HTTP_201_CREATED)


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN: Skill-expert roster + suspend (delist + block bookings + pause ads)
# ═══════════════════════════════════════════════════════════════════════════
def _expert_email(ep):
    tp = getattr(ep, "teacher_profile", None)
    user = getattr(tp, "user", None)
    return getattr(user, "email", "") if user else ""


def _sub_summary(ep):
    sub = getattr(ep, "ad_subscription", None)
    if not sub:
        return {"status": "none", "active": False, "period_end": None}
    return {
        "status": sub.status,
        "active": sub.is_currently_active(),
        "period_end": sub.current_period_end,
    }


def _admin_expert_row(ep, request=None):
    return {
        "id":           str(ep.id),
        "name":         ep.display_name(),
        "email":        _expert_email(ep),
        # NOTE: SkillCategory has no `name` field (only `label`) — this used to
        # raise AttributeError for any expert with a category set. Fixed here.
        "category":     ep.category.label if ep.category else None,
        "headline":     ep.headline,
        "rating":       float(ep.rating) if ep.rating is not None else None,
        "sessions":     ep.sessions_count,
        "reach":        ep.reach_count,
        "is_listed":    ep.is_listed,
        "is_featured":  ep.is_featured,
        "is_suspended": ep.is_suspended,
        "subscription": _sub_summary(ep),
        "photo":        _absolute_url(ep.photo, request),
    }


def _absolute_url(field_file, request=None):
    if not field_file:
        return None
    return request.build_absolute_uri(field_file.url) if request else field_file.url


class AdminExpertListView(APIView):
    """GET /skill/admin/experts/  → every expert (incl. suspended), for the admin roster."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (ExpertProfile.objects
              .select_related("category", "teacher_profile__user")
              .order_by("-is_listed", "-rating", "-sessions_count"))
        return Response([_admin_expert_row(ep, request) for ep in qs])


class AdminExpertDetailView(APIView):
    """GET /skill/admin/experts/<id>/  → full detail for the expert profile modal.
    PATCH — media moderation only: replace the expert's public photo."""
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, expert_id):
        ep = (ExpertProfile.objects
              .select_related("category", "teacher_profile__user")
              .filter(id=expert_id).first())
        if not ep:
            raise NotFound("Expert not found.")
        row = _admin_expert_row(ep, request)
        row.update({
            "bio":          ep.bio,
            "skill_tags":   ep.skill_tags or [],
            "availability": ep.availability,
        })
        return Response(row)

    def patch(self, request, expert_id):
        ep = ExpertProfile.objects.filter(id=expert_id).first()
        if not ep:
            raise NotFound("Expert not found.")
        photo = request.data.get("photo")
        if photo is None:
            raise ValidationError("photo is required.")
        ep.photo = photo
        ep.save(update_fields=["photo", "updated_at"])
        return Response(_admin_expert_row(ep, request))


class AdminExpertSuspendView(APIView):
    """
    POST /skill/admin/experts/<id>/suspend/   { "action": "suspend" | "unsuspend" }

    Suspend = all three: delist from the marketplace, block new bookings
    (booking requires is_listed=True), and pause advertising (cancel the ad
    subscription + unfeature). Unsuspend re-lists if the profile is complete.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, expert_id):
        ep = ExpertProfile.objects.filter(id=expert_id).first()
        if not ep:
            raise NotFound("Expert not found.")
        action = (request.data.get("action") or "suspend").lower()

        if action == "suspend":
            ep.is_suspended = True
            ep.is_listed = False        # delists + blocks new bookings
            ep.is_featured = False       # stops advertising
            ep.save(update_fields=["is_suspended", "is_listed", "is_featured", "updated_at"])
            sub = getattr(ep, "ad_subscription", None)
            if sub and sub.status not in ("cancelled", "expired"):
                sub.cancel()             # pauses the ad subscription + decays reach
        elif action == "unsuspend":
            ep.is_suspended = False
            ep.save(update_fields=["is_suspended", "updated_at"])
            ep.sync_listing(save=True)   # re-list if the profile is complete
        else:
            raise ValidationError("action must be 'suspend' or 'unsuspend'.")

        ep.refresh_from_db()
        return Response({"ok": True, **_admin_expert_row(ep, request)})


# =====================================================
# ADMIN — SKILLDEV CMS  (categories + marketing copy)
# =====================================================

class AdminSkillCategoryListView(APIView):
    """GET list / POST create — SkillDev CMS "Categories" tab."""
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        qs = SkillCategory.objects.all()
        return Response(
            SkillCategoryAdminSerializer(qs, many=True, context={"request": request}).data
        )

    def post(self, request):
        serializer = SkillCategoryAdminSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AdminSkillCategoryDetailView(APIView):
    """GET / PATCH / DELETE one category — SkillDev CMS "Categories" tab."""
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _get(self, category_id):
        cat = SkillCategory.objects.filter(id=category_id).first()
        if not cat:
            raise NotFound("Category not found.")
        return cat

    def get(self, request, category_id):
        cat = self._get(category_id)
        return Response(
            SkillCategoryAdminSerializer(cat, context={"request": request}).data
        )

    def patch(self, request, category_id):
        cat = self._get(category_id)
        serializer = SkillCategoryAdminSerializer(
            cat, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, category_id):
        cat = self._get(category_id)
        cat.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminSkillMarketingBlockListView(APIView):
    """GET the 3 fixed marketing blocks (created on first read if missing) —
    SkillDev CMS "Marketing" tab. No create/delete: keys are fixed."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        for key, _label in SkillMarketingBlock.KEY_CHOICES:
            SkillMarketingBlock.objects.get_or_create(key=key)
        qs = SkillMarketingBlock.objects.all()
        return Response(
            SkillMarketingBlockAdminSerializer(qs, many=True, context={"request": request}).data
        )


class AdminSkillMarketingBlockDetailView(APIView):
    """PATCH one marketing block by key — SkillDev CMS "Marketing" tab."""
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, key):
        block, _created = SkillMarketingBlock.objects.get_or_create(key=key)
        serializer = SkillMarketingBlockAdminSerializer(
            block, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
