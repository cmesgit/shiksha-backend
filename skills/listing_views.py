"""skills/listing_views.py — teacher-owned CRUD for SkillListing.

    GET    /skill/teacher/listings/          list mine
    POST   /skill/teacher/listings/          create (live immediately)
    GET    /skill/teacher/listings/<id>/     detail
    PATCH  /skill/teacher/listings/<id>/     update (incl. is_active pause)
    DELETE /skill/teacher/listings/<id>/     remove — refused if it has sessions
    PUT    /skill/teacher/listings/<id>/slots/   which expert slots it may use

Publishing is instant by product decision; an admin can suspend any listing
afterwards (admin_views.SuspendListingView).
"""
import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .listing_models import ListingModerationFlag, SkillListing, SkillListingSlot
from .listing_serializers import SkillListingWriteSerializer
from .models import ExpertProfile, SkillSession
from .moderation import flag_for_review


def expert_or_403(request):
    expert = (
        ExpertProfile.objects
        .filter(teacher_profile__user=request.user)
        .select_related("teacher_profile")
        .first()
    )
    if not expert:
        raise PermissionDenied("You don't have an expert profile yet.")
    return expert


class TeacherListingListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        expert = expert_or_403(request)
        qs = expert.listings.select_related("category").prefetch_related("slot_keys")
        return Response(
            SkillListingWriteSerializer(qs, many=True, context={"request": request}).data
        )

    def post(self, request):
        expert = expert_or_403(request)
        ser = SkillListingWriteSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        # No hard cap by product decision. Rate-limit instead: more than three
        # new listings in seven days flags the expert into the admin queue.
        recent = expert.listings.filter(
            created_at__gte=timezone.now() - datetime.timedelta(days=7)
        ).count()
        listing = ser.save(expert=expert, order=expert.listings.count())
        if recent >= 3:
            flag_for_review(
                expert,
                reason=ListingModerationFlag.REASON_LISTING_BURST,
                listing=listing,
                detail=f"{recent + 1} listings published in the last 7 days.",
            )
        return Response(
            SkillListingWriteSerializer(listing, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class TeacherListingDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, listing_id):
        expert = expert_or_403(request)
        listing = (
            expert.listings.filter(id=listing_id)
            .select_related("category", "expert")
            .prefetch_related("slot_keys")
            .first()
        )
        if not listing:
            raise NotFound("Skill not found.")
        return listing

    def get(self, request, listing_id):
        listing = self._get(request, listing_id)
        return Response(SkillListingWriteSerializer(listing, context={"request": request}).data)

    def patch(self, request, listing_id):
        listing = self._get(request, listing_id)
        if listing.is_suspended and request.data.get("is_active"):
            raise ValidationError("This skill was suspended by an admin and can't be re-listed here.")
        ser = SkillListingWriteSerializer(
            listing, data=request.data, partial=True, context={"request": request}
        )
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, listing_id):
        listing = self._get(request, listing_id)
        # Deleting a listing with history would orphan sessions and reviews.
        # Pausing is the reversible, non-destructive path; say so.
        if SkillSession.objects.filter(listing=listing).exists():
            raise ValidationError(
                "This skill has sessions attached. Pause it instead — "
                "pausing hides it from learners and keeps the history intact."
            )
        listing.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TeacherListingSlotsView(APIView):
    """Which of the EXPERT's weekly slots this listing may be booked into.

    Availability itself stays on ExpertProfile — one human, one grid, so two
    skills can never claim the same hour. An empty set means "any open slot",
    which is what every listing starts as.

        GET /skill/teacher/listings/<id>/slots/  → {"slot_keys": ["3-1", ...]}
        PUT same                                 ← {"slot_keys": [...]}
    """
    permission_classes = [IsAuthenticated]

    def _get(self, request, listing_id):
        expert = expert_or_403(request)
        listing = expert.listings.filter(id=listing_id).first()
        if not listing:
            raise NotFound("Skill not found.")
        return listing

    def get(self, request, listing_id):
        listing = self._get(request, listing_id)
        return Response({
            "slot_keys": list(listing.slot_keys.values_list("slot_key", flat=True)),
            "expert_open": (listing.expert.availability_slots or {}).get("open", []),
        })

    def put(self, request, listing_id):
        listing = self._get(request, listing_id)
        keys = request.data.get("slot_keys")
        if not isinstance(keys, list):
            raise ValidationError({"slot_keys": "Send a list of slot keys."})
        clean = {str(k).strip() for k in keys if str(k).strip()}
        with transaction.atomic():
            listing.slot_keys.all().delete()
            SkillListingSlot.objects.bulk_create(
                [SkillListingSlot(listing=listing, slot_key=k) for k in sorted(clean)]
            )
        return Response({"slot_keys": sorted(clean)})
