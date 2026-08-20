"""
Agreement-letter views — admin editor with immutable version history.

  GET  /accounts/admin/agreements/                       → list letters (current version summary)
  GET  /accounts/admin/agreements/<key>/                 → current version (full body)
  POST /accounts/admin/agreements/<key>/save/            → create a NEW version {title, body, change_note}
  GET  /accounts/admin/agreements/<key>/versions/        → version history (newest first)
  GET  /accounts/admin/agreements/versions/<id>/         → one version (full body)
  POST /accounts/admin/agreements/versions/<id>/restore/ → new version copying that body → current

  GET  /accounts/agreements/<key>/                       → public: current version (for the signup screen)

Editing NEVER mutates an existing version; every Save appends a new immutable
version and repoints ``current_version``. Faculty stay bound to the version
they signed via TeacherProfile.signed_agreement_version.
"""
from django.db import transaction
from django.db.models import Max
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.throttling import AnonRateThrottle

from .permissions import IsAdmin
from .models import AgreementLetter, AgreementLetterVersion

# Every valid `key` an agreement letter can be created under — a bare
# slug field + get_or_create used to mean any URL, admin or public, silently
# minted a permanent new letter row on a typo (e.g. POSTing to .../faculy/
# instead of .../faculty/), with no way to tell it apart from a real one.
AGREEMENT_KEYS = {"faculty": "Faculty Agreement"}


def _valid_key_or_404(key):
    if key not in AGREEMENT_KEYS:
        raise NotFound("Unknown agreement key.")


class AgreementPublicRateThrottle(AnonRateThrottle):
    # Unauthenticated and previously had no throttle at all, unlike every
    # other AllowAny endpoint in this app.
    scope = "agreement_public"


def _document_url(v):
    """The imported-file URL for a version, or None. Goes through the storage
    layer so private-media gating applies (config/media_security.py)."""
    try:
        return v.document.url if v.document else None
    except Exception:
        return None


def _version_dict(v, *, full=False):
    if v is None:
        return None
    d = {
        "id":             str(v.id),
        "version_number": v.version_number,
        "title":          v.title,
        "change_note":    v.change_note,
        "created_at":     v.created_at,
        "created_by":     (v.created_by.email if v.created_by else None),
        "document_url":   _document_url(v),
        "document_name":  (v.document.name.rsplit("/", 1)[-1] if v.document else None),
        "status":         v.status,
        "is_draft":       v.status == AgreementLetterVersion.STATUS_DRAFT,
    }
    if full:
        d["body"] = v.body
    return d


def _draft_of(letter):
    """The letter's single mutable draft, or None."""
    if letter is None:
        return None
    return letter.versions.filter(
        status=AgreementLetterVersion.STATUS_DRAFT).first()


def _letter_dict(letter, *, full=False):
    """Admin view of a letter: what is LIVE, plus the unpublished draft if any.
    Both are returned so the editor can show "live is v3, you have unsaved
    draft changes" instead of silently conflating them."""
    return {
        "key":             letter.key,
        "title":           letter.title,
        "current_version": _version_dict(letter.current_version, full=full),
        "draft":           _version_dict(_draft_of(letter), full=full),
        "updated_at":      letter.updated_at,
    }


def _public_letter_dict(letter):
    """Same shape as _letter_dict(full=True) but never leaks who wrote the
    version (an admin's real email address) to an unauthenticated caller."""
    v = letter.current_version
    return {
        "key":             letter.key,
        "title":           letter.title,
        "current_version": {
            "id":             str(v.id),
            "version_number": v.version_number,
            "title":          v.title,
            "body":           v.body,
            "created_at":     v.created_at,
            # Present only when the admin imported a file for this version —
            # the signup screen offers it as the download in that case, and
            # prints the rendered body otherwise.
            "document_url":   _document_url(v),
            "document_name":  (v.document.name.rsplit("/", 1)[-1] if v.document else None),
        },
        "updated_at":      letter.updated_at,
    }


class AdminAgreementListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        letters = AgreementLetter.objects.select_related("current_version").all()
        return Response([_letter_dict(l) for l in letters])


class AdminAgreementDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, key):
        _valid_key_or_404(key)
        letter = (AgreementLetter.objects
                  .select_related("current_version")
                  .filter(key=key).first())
        if not letter:
            # Not created yet — return an empty shell the editor can save into.
            return Response({"key": key, "title": "", "current_version": None, "updated_at": None})
        return Response(_letter_dict(letter, full=True))


class AdminAgreementSaveView(APIView):
    """Save the DRAFT. Does NOT go live — see AdminAgreementPublishView.

    Every save used to create a numbered version AND repoint
    `current_version`, so a half-finished clause was binding on every
    subsequent signup the instant it was typed, and drafting next year's
    letter across several sittings was impossible. This now upserts the
    letter's single mutable draft; nothing an applicant sees changes until
    Publish.

    Accepts EITHER authoring route (see AgreementLetterVersion.document):
    author the text in `body`, or import a ready-made file as `document`
    (multipart). A version may carry both — the file is what gets signed,
    the body is what renders on screen.
    """
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @transaction.atomic
    def post(self, request, key):
        _valid_key_or_404(key)
        title = (request.data.get("title") or "").strip()
        body = request.data.get("body") or ""
        change_note = (request.data.get("change_note") or "").strip()
        document = request.FILES.get("document")
        if not title:
            raise ValidationError({"title": "Title is required."})
        # Body was unconditionally required; an imported file is now an equally
        # valid way to publish a version, so require ONE of the two rather
        # than forcing an admin to retype a lawyer-drafted PDF as markdown.
        if not body.strip() and not document:
            raise ValidationError(
                {"body": "Add the agreement text, or import a file."}
            )
        if document:
            from django.core.exceptions import ValidationError as DjangoValidationError
            from config.upload_validation import validate_upload
            try:
                validate_upload(
                    document,
                    {".pdf", ".doc", ".docx"},
                    max_mb=10,
                )
            except DjangoValidationError as e:
                raise ValidationError({"document": str(e.message)})

        # select_for_update so two admins saving concurrently serialize on the
        # letter row instead of racing to create a second draft, which the
        # single-draft constraint would turn into an IntegrityError/500.
        letter, _ = AgreementLetter.objects.select_for_update().get_or_create(
            key=key, defaults={"title": title}
        )
        # NOTE: letter.title is deliberately NOT updated here. It reflects the
        # PUBLISHED letter's name; renaming it from an unpublished draft would
        # change what the admin list shows for a letter that hasn't changed.
        draft = _draft_of(letter)
        if draft is None:
            draft = AgreementLetterVersion(
                letter=letter, status=AgreementLetterVersion.STATUS_DRAFT,
                version_number=None,
            )
        draft.title = title
        draft.body = body
        draft.change_note = change_note
        draft.created_by = request.user
        if document:
            draft.document = document
        draft.save()
        return Response({"ok": True, "saved": "draft", **_letter_dict(letter, full=True)})


class AdminAgreementPublishView(APIView):
    """Freeze the draft as the next numbered version and make it live.

    This is the only endpoint that changes what an applicant sees. The number
    is assigned HERE rather than at draft-creation time, so abandoning a draft
    leaves no gap in the history.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, key):
        _valid_key_or_404(key)
        letter = AgreementLetter.objects.select_for_update().filter(key=key).first()
        if not letter:
            raise NotFound("Nothing to publish.")
        draft = _draft_of(letter)
        if draft is None:
            raise ValidationError({"detail": "There is no draft to publish."})

        next_num = (letter.versions
                    .filter(status=AgreementLetterVersion.STATUS_PUBLISHED)
                    .aggregate(m=Max("version_number"))["m"] or 0) + 1
        draft.status = AgreementLetterVersion.STATUS_PUBLISHED
        draft.version_number = next_num
        draft.save(update_fields=["status", "version_number"])

        letter.current_version = draft
        letter.title = draft.title
        letter.save(update_fields=["current_version", "title", "updated_at"])
        return Response({"ok": True, "published_version": next_num,
                         **_letter_dict(letter, full=True)})


class AdminAgreementDiscardDraftView(APIView):
    """Throw the draft away. The live version is untouched."""
    permission_classes = [IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, key):
        _valid_key_or_404(key)
        letter = AgreementLetter.objects.select_for_update().filter(key=key).first()
        draft = _draft_of(letter)
        if draft is None:
            raise ValidationError({"detail": "There is no draft to discard."})
        draft.delete()
        return Response({"ok": True, "discarded": True, **_letter_dict(letter, full=True)})


class AdminAgreementVersionsView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, key):
        _valid_key_or_404(key)
        letter = AgreementLetter.objects.filter(key=key).first()
        if not letter:
            return Response([])
        current_id = letter.current_version_id
        rows = []
        # PUBLISHED only — history is the record of what was actually live.
        # The unpublished draft is returned separately by the detail endpoint
        # so the editor can show it without it masquerading as a version.
        published = (letter.versions
                     .filter(status=AgreementLetterVersion.STATUS_PUBLISHED)
                     .select_related("created_by"))
        for v in published:
            d = _version_dict(v)
            d["is_current"] = (v.id == current_id)
            d["signed_count"] = v.signed_by.count()
            rows.append(d)
        return Response(rows)


class AdminAgreementVersionDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, version_id):
        v = AgreementLetterVersion.objects.select_related("created_by", "letter").filter(id=version_id).first()
        if not v:
            raise NotFound("Version not found.")
        d = _version_dict(v, full=True)
        d["is_current"] = (v.letter.current_version_id == v.id)
        return Response(d)


class AdminAgreementRestoreView(APIView):
    """Load an old version's contents into the DRAFT for review.

    Restore used to publish immediately. Now it stages, so restoring is
    reviewable and reversible like any other edit — the admin still has to
    press Publish, and nothing an applicant sees changes until they do.
    Overwrites an existing draft (there is only ever one).
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, version_id):
        old = AgreementLetterVersion.objects.select_related("letter").filter(id=version_id).first()
        if not old:
            raise NotFound("Version not found.")
        if old.status != AgreementLetterVersion.STATUS_PUBLISHED:
            raise ValidationError({"detail": "That version is the current draft."})
        # Lock the letter row for the same race-prevention reason as Save.
        letter = AgreementLetter.objects.select_for_update().get(pk=old.letter_id)

        draft = _draft_of(letter)
        if draft is None:
            draft = AgreementLetterVersion(
                letter=letter, status=AgreementLetterVersion.STATUS_DRAFT,
                version_number=None,
            )
        draft.title = old.title
        draft.body = old.body
        # Carry the imported file forward as well, or restoring a file-based
        # version would silently produce a body-only one — i.e. quietly
        # change what applicants actually sign. Points at the same stored
        # file rather than copying bytes; published versions are immutable so
        # nothing can rewrite it underneath either of them.
        draft.document = (old.document.name or None) if old.document else None
        draft.change_note = f"Restored from v{old.version_number}"
        draft.created_by = request.user
        draft.save()
        return Response({"ok": True, "saved": "draft", **_letter_dict(letter, full=True)})


class PublicAgreementView(APIView):
    """The current PUBLISHED agreement text, for the faculty signup screen.

    Reads `current_version` only, which the publish path is the sole writer
    of — so an unpublished draft can never reach an applicant. The status
    assertion below is belt-and-braces: it costs nothing and it means a
    future bug that repoints current_version at a draft fails loudly here
    rather than quietly binding someone to unreviewed terms.
    """
    permission_classes = [AllowAny]
    throttle_classes = [AgreementPublicRateThrottle]

    def get(self, request, key):
        _valid_key_or_404(key)
        letter = AgreementLetter.objects.select_related("current_version").filter(key=key).first()
        if not letter or not letter.current_version:
            raise NotFound("Agreement not published yet.")
        if letter.current_version.status != AgreementLetterVersion.STATUS_PUBLISHED:
            raise NotFound("Agreement not published yet.")
        return Response(_public_letter_dict(letter))


# ─── Faculty re-apply after rejection ───────────────────────────────────────
from rest_framework.exceptions import PermissionDenied
from .models import TeacherProfile


class ReapplyAcademyView(APIView):
    """
    POST /accounts/teacher/reapply-academy/

    A faculty applicant whose academy application was REJECTED can re-submit for
    review. Flips academy_status back to PENDING and clears the rejection reason.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tp = getattr(request.user, "teacher_profile", None)
        if not tp:
            raise PermissionDenied("No teacher profile.")
        if tp.academy_status != TeacherProfile.TRACK_REJECTED:
            raise ValidationError("Only a rejected application can be re-submitted.")
        tp.academy_status = TeacherProfile.TRACK_PENDING
        tp.academy_rejection_reason = ""
        tp.academy_rejected_at = None
        tp.save(update_fields=["academy_status", "academy_rejection_reason", "academy_rejected_at"])
        return Response({"ok": True, "academy_status": tp.academy_status})


# ─── Faculty signup choice lists ────────────────────────────────────────────

class FacultyChoicesRateThrottle(AnonRateThrottle):
    scope = "faculty_choices"


class FacultyChoicesView(APIView):
    """GET /accounts/faculty-choices/ — every option list the faculty signup
    form renders.

    Served rather than duplicated in JS, for exactly the reason
    SettingsChoicesView (accounts/settings_views.py) already documents: the
    hardcoded copies drift. They had already drifted badly here —
    FacultySignup.jsx shipped 10 subjects while the model accepted 15, so five
    subjects (Computer Science, Accountancy, Business Studies, Political
    Science, Other) were unreachable for every applicant, and
    FormFillup.jsx kept two MORE hardcoded copies of the same list. Any value
    the form sends that isn't in these lists is silently dropped by
    SignupSerializer's validation, so a drifted copy fails invisibly.

    AllowAny: the standalone /faculty/signup flow renders step 2 BEFORE the
    account exists (the account is only created on final submit), so this
    cannot require authentication. It exposes nothing but a static taxonomy —
    no user data, no counts — but is throttled anyway, matching every other
    AllowAny endpoint in this app.
    """
    permission_classes = [AllowAny]
    throttle_classes = [FacultyChoicesRateThrottle]

    def get(self, request):
        def pairs(choices):
            return [{"value": v, "label": l} for v, l in choices]

        return Response({
            # Grouped for rendering; `subjects` is the flat authoritative list
            # so a client can validate without caring about presentation.
            "subject_groups": [
                {"group": group, "options": pairs(options)}
                for group, options in TeacherProfile.SUBJECT_GROUPS
            ],
            "subjects":          pairs(TeacherProfile.SUBJECT_CHOICES),
            "classes":           pairs(TeacherProfile.CLASS_CHOICES),
            "streams":           pairs(TeacherProfile.STREAM_CHOICES),
            "highest_degree":    pairs(TeacherProfile.HIGHEST_DEGREE_CHOICES),
            "experience_range":  pairs(TeacherProfile.EXPERIENCE_CHOICES),
            "employment_status": pairs(TeacherProfile.EMPLOYMENT_STATUS_CHOICES),
            "govt_id_type":      pairs(TeacherProfile.GOVT_ID_TYPE_CHOICES),
            "boards":            pairs(TeacherProfile.BOARD_CHOICES),
        })
