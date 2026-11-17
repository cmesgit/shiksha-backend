"""Forms — the site's public forms, and where their answers go.

design_handoff_admin_counsellor, Admin v2 Phase 5 ("Forms").

⚠ THIS IS NOT A FORM BUILDER, AND THE ARTBOARD DOES NOT ASK FOR ONE.
Its Forms screen lists the forms the site ALREADY has — "Teacher application",
"Contact us", "Book a counselling session" — each with its fields, **where
the answers go**, and its submissions. That is a directory over real data,
not a builder for imaginary data, and it is the honest reading: a builder
would invent a second way to collect information that nothing on the public
site would render.

THE GAP THIS ACTUALLY CLOSES
────────────────────────────
Most of the site's forms already deliver somewhere an admin can see — a
teacher application becomes a pending `TeacherProfile` on /approvals, a
counselling booking becomes a pending `Appointment`. **`ContactMessage` does
not.** It is a fully-formed model with `status` (new / in_progress / closed)
and `handled_note` — built to be worked — and it has had **no admin API and
no console screen at all**. Its own docstring says the point of the row is
that "the enquiry is recoverable from the admin"; in practice that meant the
Django admin, which the people answering enquiries do not use. So a visitor
filled in Contact us, was told "we got it", and nobody on the console ever
saw it.

This module gives that model its console home, and gives every other form an
honest pointer to the screen that already owns it rather than a second copy.
"""
from django.db.models import Count, Q
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

from .models import ContactMessage, NewsletterSubscriber


class ContactMessageSerializer(serializers.ModelSerializer):
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    topic_label = serializers.CharField(source="get_topic_display", read_only=True)

    class Meta:
        model = ContactMessage
        fields = ("id", "name", "email", "phone", "role", "role_label",
                  "topic", "topic_label", "message", "status", "handled_note",
                  "created_at")
        # Everything a visitor sent is immutable: an admin works the enquiry,
        # they do not edit what was said. Only the two handling fields move.
        read_only_fields = ("name", "email", "phone", "role", "topic",
                            "message", "created_at")


def _contact_counts():
    agg = ContactMessage.objects.aggregate(
        total=Count("id"),
        new=Count("id", filter=Q(status=ContactMessage.Status.NEW)),
    )
    return {"total": agg["total"] or 0, "new": agg["new"] or 0}


class AdminFormDirectoryView(APIView):
    """GET /api/content/admin/forms/

    Every public form, what it asks for, and where its answers land.

    ⚠ `managed_here` is the load-bearing field. True means this screen owns
    the submissions and can work them; False means the answers become a
    domain object that another screen already owns, and `url` points there.
    Showing a second inbox for those would create two places to action the
    same teacher application — and therefore two ways to miss it.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        from accounts.models import TeacherProfile
        from counseling.models import Appointment, CounselorProfile

        def safe(fn):
            try:
                return fn()
            except Exception:
                return None

        forms = [
            {
                "key": "contact",
                "name": "Contact us",
                "where": "/contact",
                "fields": ["Name", "Email", "Phone", "Role", "Topic", "Message"],
                "destination": "Handled on this screen",
                "url": None,
                "managed_here": True,
                "counts": safe(_contact_counts) or {"total": 0, "new": 0},
            },
            {
                "key": "teacher_application",
                "name": "Teacher application",
                "where": "/become-faculty",
                "fields": ["Name", "Email", "Qualification", "Track"],
                "destination": "People › Teacher applications",
                "url": "/approvals",
                "managed_here": False,
                "counts": {"new": safe(lambda: TeacherProfile.objects.filter(
                    academy_status=TeacherProfile.TRACK_PENDING).count()) or 0},
            },
            {
                "key": "counsellor_application",
                "name": "Counsellor application",
                "where": "/become-counsellor",
                "fields": ["Display name", "Specialisations", "Qualifications",
                           "Experience"],
                "destination": "People › Counsellors",
                "url": "/counselor-approvals",
                "managed_here": False,
                "counts": {"new": safe(lambda: CounselorProfile.objects.filter(
                    status=CounselorProfile.STATUS_PENDING).count()) or 0},
            },
            {
                "key": "counselling_booking",
                "name": "Book a counselling session",
                "where": "/counselling/counsellors/<id>",
                "fields": ["Student", "Slot", "Note"],
                "destination": "Live classes › Counselling sessions",
                "url": "/counseling-sessions",
                "counts": {"new": safe(lambda: Appointment.objects.filter(
                    status="pending").count()) or 0},
                "managed_here": False,
            },
            {
                "key": "newsletter",
                "name": "Newsletter sign-up",
                "where": "Site footer",
                "fields": ["Email"],
                "destination": "Handled on this screen",
                "url": None,
                "managed_here": True,
                "counts": {"total": safe(
                    lambda: NewsletterSubscriber.objects.count()) or 0},
            },
        ]
        return Response({"results": forms})


class AdminContactSubmissionsView(APIView):
    """GET /api/content/admin/forms/contact/

    The enquiries themselves. `?status=` filters; omit it for everything.
    Newest first — an enquiry answered late is the failure mode here.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = ContactMessage.objects.all().order_by("-created_at")
        st = (request.query_params.get("status") or "").strip()
        if st:
            qs = qs.filter(status=st)
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(email__icontains=q) | Q(message__icontains=q))
        # Capped, and the count is the REAL count so the screen can say so
        # rather than implying the page is everything.
        total = qs.count()
        return Response({
            "results": ContactMessageSerializer(qs[:200], many=True).data,
            "count": total,
            "counts": _contact_counts(),
        })


class AdminContactSubmissionDetailView(APIView):
    """PATCH /api/content/admin/forms/contact/<id>/ — status + handled note."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, pk):
        row = ContactMessage.objects.filter(pk=pk).first()
        if row is None:
            return Response({"detail": "Not found."},
                            status=status.HTTP_404_NOT_FOUND)
        ser = ContactMessageSerializer(row, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)
