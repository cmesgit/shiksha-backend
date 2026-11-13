"""Wire shapes for community verification.

Everything the Verify page and the admin screen read. Kept apart from
`documents/serializers.py` so the library's own shapes stay readable.

⚠ Nothing here recomputes a rule. Eligibility, counts and state all come from
`documents/verification.py`. A serializer that decided for itself whether
someone may review would be the second place that decision lives, and the
screens would disagree with the server the first time a setting changed.
"""
from rest_framework import serializers

from . import verification as v
from .models import (
    Document,
    SpecializationStatus,
    SubjectSpecialization,
    VerificationInvite,
    VerificationReview,
    VerificationSettings,
)


def _display_name(user):
    if user is None:
        return ""
    full = (getattr(user, "get_full_name", lambda: "")() or "").strip()
    return full or getattr(user, "username", "") or ""


class VerificationProgressSerializer(serializers.Serializer):
    """The badge and its progress line, for one set.

    Embedded in every card and on the document page. The frontend renders
    `label` verbatim — the wording is the design's and belongs next to the
    rule that produces it, not spread across three components.
    """

    status = serializers.CharField()
    confirmations = serializers.IntegerField()
    needed = serializers.IntegerField()
    remaining = serializers.IntegerField()
    version = serializers.IntegerField()
    label = serializers.SerializerMethodField()
    badge = serializers.SerializerMethodField()

    def get_badge(self, obj):
        # The public never sees "changes requested" — the design is explicit
        # that such a set "stays in Browse, marked Unverified". Only the
        # uploader and the admin see the real state, and they read `status`.
        return "verified" if obj["status"] == "verified" else "unverified"

    def get_label(self, obj):
        if obj["status"] == "verified":
            return f"Confirmed by {obj['confirmations']} specialists"
        return f"{obj['confirmations']} of {obj['needed']} confirmations"


class QueueItemSerializer(serializers.ModelSerializer):
    """A row in the reviewer's Review queue."""

    category = serializers.SerializerMethodField()
    uploader = serializers.SerializerMethodField()
    file_count = serializers.SerializerMethodField()
    waiting = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    invite = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ["id", "title", "category", "uploader", "file_count",
                  "waiting", "tags", "progress", "invite"]

    def get_category(self, doc):
        return doc.category.name if doc.category_id else ""

    def get_uploader(self, doc):
        return _display_name(doc.owner)

    def get_file_count(self, doc):
        return doc.files.count()

    def get_waiting(self, doc):
        """Humanised, because the design's row reads "waiting 6 days" and
        "waiting 5 hours" — the unit changes with the age."""
        from django.utils import timezone
        delta = timezone.now() - doc.created_at
        days = delta.days
        if days >= 1:
            return f"{days} day{'s' if days != 1 else ''}"
        hours = delta.seconds // 3600
        if hours >= 1:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        return "under an hour"

    def get_tags(self, doc):
        return [
            {"field": t.field, "subject": t.subject, "branch": t.branch,
             "path": str(t)}
            for t in doc.topics.all()
        ]

    def get_progress(self, doc):
        settings = self.context.get("vsettings") or VerificationSettings.load()
        return VerificationProgressSerializer(v.progress(doc, settings)).data

    def get_invite(self, doc):
        """`Invited · 1 day left to respond · reminder sent` — only present
        when this viewer actually holds an invite. Absent is meaningful: the
        set is in their open queue, not assigned to them."""
        user = self.context.get("request").user if self.context.get("request") else None
        if not user or not user.is_authenticated:
            return None
        invite = VerificationInvite.objects.filter(
            document=doc, version=doc.verification_version,
            reviewer=user, state="open",
        ).first()
        if invite is None:
            return None

        from django.utils import timezone
        left = None
        if invite.expires_at:
            delta = invite.expires_at - timezone.now()
            left = max(0, delta.days)
        return {
            "days_left": left,
            "reminder_sent": invite.reminder_sent_at is not None,
            "assigned_by_admin": invite.assigned_by_admin,
        }


class ReviewFileSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    size = serializers.IntegerField()
    filetype = serializers.CharField()
    url = serializers.CharField()


class ReviewTargetSerializer(QueueItemSerializer):
    """The set as the review form shows it — the queue row plus its files,
    plus whether this viewer may actually act."""

    files = serializers.SerializerMethodField()
    uploaded_ago = serializers.SerializerMethodField()
    can_review = serializers.SerializerMethodField()
    reason = serializers.SerializerMethodField()
    reviewing_as = serializers.SerializerMethodField()

    class Meta(QueueItemSerializer.Meta):
        fields = QueueItemSerializer.Meta.fields + [
            "files", "uploaded_ago", "can_review", "reason", "reviewing_as",
        ]

    def get_files(self, doc):
        out = []
        for f in doc.files.all():
            try:
                url = f.file.url
            except Exception:
                # A backfilled row can point at storage that no longer holds
                # the object. An unopenable file is exactly what check three
                # asks about, so surfacing it beats a 500.
                url = ""
            out.append({
                "id": f.id,
                "name": f.original_name or str(f.file).rsplit("/", 1)[-1],
                "size": f.size,
                "filetype": f.filetype,
                "url": url,
            })
        return out

    def get_uploaded_ago(self, doc):
        return self.get_waiting(doc)

    def _verdict(self, doc):
        request = self.context.get("request")
        user = request.user if request else None
        settings = self.context.get("vsettings") or VerificationSettings.load()
        return v.can_review(user, doc, settings)

    def get_can_review(self, doc):
        return self._verdict(doc)[0]

    def get_reason(self, doc):
        return self._verdict(doc)[1]

    def get_reviewing_as(self, doc):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return ""
        return v.matching_subject(request.user, doc)


class SubmitReviewSerializer(serializers.Serializer):
    """The three checks and the note.

    ⚠ The note is conditionally required, which is why this is a Serializer
    with a `validate` rather than three fields with their own rules: whether
    `note` may be blank depends on the other three values. The design shows it
    by relabelling the field "(required)" and the button "Request changes" at
    the same moment.
    """

    matches_tags = serializers.BooleanField()
    content_accurate = serializers.BooleanField()
    files_complete = serializers.BooleanField()
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        all_yes = (attrs["matches_tags"] and attrs["content_accurate"]
                   and attrs["files_complete"])
        if not all_yes and not attrs.get("note", "").strip():
            raise serializers.ValidationError({
                "note": "Say what is wrong and where, so the uploader can fix it.",
            })
        return attrs


class SpecializationSerializer(serializers.ModelSerializer):
    reason = serializers.SerializerMethodField()

    class Meta:
        model = SubjectSpecialization
        fields = ["id", "subject", "source", "status", "is_paused", "reason",
                  "evidence", "created_at"]
        read_only_fields = ["source", "status", "is_paused", "created_at"]

    def get_reason(self, spec):
        """The grey line under each subject on the Specializations tab. It
        explains *why* someone is confirmed, which is the only thing that
        makes the list meaningful to the member reading it."""
        if spec.status == SpecializationStatus.PENDING:
            return "Waiting for an administrator"
        if spec.status == SpecializationStatus.DECLINED:
            return "An administrator declined this request"
        if spec.source == "teacher":
            return "Teacher account"
        if spec.source == "earned":
            needed = (self.context.get("vsettings")
                      or VerificationSettings.load()).verified_uploads_to_earn_it
            return f"{needed} of your uploads in this subject are verified"
        return "Approved by an admin"


class MyUploadSerializer(serializers.ModelSerializer):
    """A card on the reviewer page's "My uploads" tab."""

    progress = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    reviewer_note = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ["id", "title", "tags", "progress", "reviewer_note"]

    def get_tags(self, doc):
        return [str(t) for t in doc.topics.all()]

    def get_progress(self, doc):
        settings = self.context.get("vsettings") or VerificationSettings.load()
        data = VerificationProgressSerializer(v.progress(doc, settings)).data
        # The uploader is the one person who should see the real state —
        # "Confirmations paused at 1 of 3" rather than the public's bare
        # "Unverified". Hiding it here is how the design's own My uploads tab
        # would have had nothing to show.
        if doc.verification_status == "changes_requested":
            # "Confirmations paused at 1 of 3", not "…of 3 confirmations" —
            # the word is already in the sentence.
            data["label"] = (
                f"Confirmations paused at {data['confirmations']} "
                f"of {data['needed']}"
            )
        return data

    def get_reviewer_note(self, doc):
        if doc.verification_status != "changes_requested":
            return ""
        review = (
            VerificationReview.objects.filter(
                document=doc, version=doc.verification_version,
                outcome="changes_requested",
            ).order_by("-created_at").first()
        )
        return review.note if review else ""


class VerificationSettingsSerializer(serializers.ModelSerializer):
    summary = serializers.SerializerMethodField()

    class Meta:
        model = VerificationSettings
        exclude = ["singleton_id"]

    def get_summary(self, s):
        """The admin screen's "WHAT HAPPENS TO AN UPLOAD WITH THESE SETTINGS"
        box, as six sentences.

        ⚠ Derived here rather than written in the frontend, deliberately. It is
        the one place the admin is told what their configuration actually does;
        a hardcoded copy would keep saying "7 days" after someone changed it to
        3, and would be believed.
        """
        lines = []
        if s.show_unverified_in_browse:
            lines.append("It is published straight away with an Unverified badge.")
        else:
            lines.append("It stays private until it is Verified.")

        if s.reviewer_selection == "automatic":
            lines.append(
                f"The {s.reviewers_invited_per_set} least busy reviewers in its "
                f"subjects are invited. Each has {s.days_to_respond} days before "
                f"the invite passes on."
            )
        elif s.reviewer_selection == "open_queue":
            lines.append(
                "It sits in the open queue for every reviewer in its subjects. "
                "Nobody is invited and there is no deadline."
            )
        else:
            lines.append("It waits for you to assign a reviewer by hand.")

        skips = ["the uploader"]
        if s.skip_uploader_institution:
            skips.append("anyone from the same institution")
        skips.append(f"reviewers with {s.open_reviews_per_reviewer} open reviews")
        lines.append("Never invited: " + ", ".join(skips) + ".")

        if s.widen_to_same_field:
            lines.append(
                "If nobody is available, reviewers from the same field are "
                "invited, then it comes to you."
            )
        elif s.notify_admins_when_no_reviewer:
            lines.append("If nobody is available, it comes to you.")
        else:
            lines.append("If nobody is available, it waits.")

        lines.append(
            f"After {s.confirmations_needed} confirmations it is marked Verified."
        )
        lines.append(
            "A change request sends it back to the uploader. After "
            f"{s.change_requests_before_hidden} on one version it is hidden and "
            "comes to you."
        )
        return lines


class AdminReviewerSerializer(serializers.ModelSerializer):
    """A row in the admin's Reviewers table."""

    name = serializers.SerializerMethodField()
    reviews = serializers.SerializerMethodField()
    open_reviews = serializers.SerializerMethodField()
    qualification = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()

    class Meta:
        model = SubjectSpecialization
        fields = ["id", "name", "subject", "reviews", "open_reviews",
                  "qualification", "state", "evidence", "status"]

    def get_name(self, spec):
        return _display_name(spec.user)

    def get_reviews(self, spec):
        return VerificationReview.objects.filter(reviewer_id=spec.user_id).count()

    def get_open_reviews(self, spec):
        return VerificationInvite.objects.filter(
            reviewer_id=spec.user_id, state="open").count()

    def get_qualification(self, spec):
        return {
            "teacher": "Teacher account",
            "earned": "Earned from verified uploads",
            "approved": "Approved request",
        }.get(spec.source, "Approved request")

    def get_state(self, spec):
        """`At limit` is DERIVED, not stored — it is "open reviews == the
        limit" at this instant. Storing it would mean a reviewer stayed at
        limit after finishing a review, or after an admin raised the cap."""
        if spec.is_paused:
            return "paused"
        settings = (self.context.get("vsettings")
                    or VerificationSettings.load())
        if self.get_open_reviews(spec) >= settings.open_reviews_per_reviewer:
            return "at_limit"
        return "active"


class AttentionItemSerializer(serializers.ModelSerializer):
    reason = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    change_requests = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ["id", "title", "reason", "tags", "progress", "change_requests"]

    def get_reason(self, doc):
        return self.context.get("reasons", {}).get(doc.id, "")

    def get_tags(self, doc):
        return [str(t) for t in doc.topics.all()]

    def get_progress(self, doc):
        settings = self.context.get("vsettings") or VerificationSettings.load()
        return VerificationProgressSerializer(v.progress(doc, settings)).data

    def get_change_requests(self, doc):
        """Every note, not the latest. A set is hidden because two people
        objected, and an admin deciding whether to restore it needs both."""
        return [
            {"reviewer": _display_name(r.reviewer), "note": r.note}
            for r in VerificationReview.objects.filter(
                document=doc, version=doc.verification_version,
                outcome="changes_requested",
            ).select_related("reviewer").order_by("created_at")
        ]
