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
    }
    if full:
        d["body"] = v.body
    return d


def _letter_dict(letter, *, full=False):
    return {
        "key":             letter.key,
        "title":           letter.title,
        "current_version": _version_dict(letter.current_version, full=full),
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
    """Create a new immutable version and make it current.

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

        # select_for_update: two admins saving the same letter concurrently
        # both read the same Max(version_number) and both create version N+1,
        # which the DB's unique_together constraint then turns into an
        # IntegrityError/500 for whoever commits second. Locking the row
        # first serializes the two saves instead of racing them.
        letter, _ = AgreementLetter.objects.select_for_update().get_or_create(
            key=key, defaults={"title": title}
        )
        letter.title = title
        next_num = (letter.versions.aggregate(m=Max("version_number"))["m"] or 0) + 1
        version = AgreementLetterVersion.objects.create(
            letter=letter, version_number=next_num, title=title,
            body=body, change_note=change_note, created_by=request.user,
            document=document or None,
        )
        letter.current_version = version
        letter.save(update_fields=["title", "current_version", "updated_at"])
        return Response({"ok": True, **_letter_dict(letter, full=True)})


class AdminAgreementVersionsView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, key):
        _valid_key_or_404(key)
        letter = AgreementLetter.objects.filter(key=key).first()
        if not letter:
            return Response([])
        current_id = letter.current_version_id
        rows = []
        for v in letter.versions.select_related("created_by").all():
            d = _version_dict(v)
            d["is_current"] = (v.id == current_id)
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
    """Restore an old version by appending a NEW version that copies its body."""
    permission_classes = [IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, version_id):
        old = AgreementLetterVersion.objects.select_related("letter").filter(id=version_id).first()
        if not old:
            raise NotFound("Version not found.")
        # Lock the letter row for the same race-prevention reason as Save.
        letter = AgreementLetter.objects.select_for_update().get(pk=old.letter_id)
        next_num = (letter.versions.aggregate(m=Max("version_number"))["m"] or 0) + 1
        version = AgreementLetterVersion.objects.create(
            letter=letter, version_number=next_num, title=old.title,
            body=old.body,
            # Carry the imported file forward as well, or restoring a
            # file-based version would silently produce a body-only one —
            # i.e. quietly change what applicants actually sign. Points at the
            # same stored file rather than copying bytes; versions are
            # immutable so nothing can rewrite it underneath either of them.
            document=(old.document.name or None) if old.document else None,
            change_note=f"Restored from v{old.version_number}",
            created_by=request.user,
        )
        letter.current_version = version
        # Was missing: restoring an old version's TEXT without also restoring
        # its TITLE left AgreementLetter.title stuck on whatever the most
        # recent save had set — so the admin list and the letter that
        # actually renders to faculty could disagree about its own name.
        letter.title = old.title
        letter.save(update_fields=["current_version", "title", "updated_at"])
        return Response({"ok": True, **_letter_dict(letter, full=True)})


class PublicAgreementView(APIView):
    """The current agreement text, for the faculty signup / form-fillup screen."""
    permission_classes = [AllowAny]
    throttle_classes = [AgreementPublicRateThrottle]

    def get(self, request, key):
        _valid_key_or_404(key)
        letter = AgreementLetter.objects.select_related("current_version").filter(key=key).first()
        if not letter or not letter.current_version:
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
