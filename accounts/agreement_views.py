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

from .permissions import IsAdmin
from .models import AgreementLetter, AgreementLetterVersion


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


class AdminAgreementListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        letters = AgreementLetter.objects.select_related("current_version").all()
        return Response([_letter_dict(l) for l in letters])


class AdminAgreementDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, key):
        letter = (AgreementLetter.objects
                  .select_related("current_version")
                  .filter(key=key).first())
        if not letter:
            # Not created yet — return an empty shell the editor can save into.
            return Response({"key": key, "title": "", "current_version": None, "updated_at": None})
        return Response(_letter_dict(letter, full=True))


class AdminAgreementSaveView(APIView):
    """Create a new immutable version and make it current."""
    permission_classes = [IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, key):
        title = (request.data.get("title") or "").strip()
        body = request.data.get("body") or ""
        change_note = (request.data.get("change_note") or "").strip()
        if not title:
            raise ValidationError({"title": "Title is required."})
        if not body.strip():
            raise ValidationError({"body": "Body cannot be empty."})

        letter, _ = AgreementLetter.objects.get_or_create(key=key, defaults={"title": title})
        letter.title = title
        next_num = (letter.versions.aggregate(m=Max("version_number"))["m"] or 0) + 1
        version = AgreementLetterVersion.objects.create(
            letter=letter, version_number=next_num, title=title,
            body=body, change_note=change_note, created_by=request.user,
        )
        letter.current_version = version
        letter.save(update_fields=["title", "current_version", "updated_at"])
        return Response({"ok": True, **_letter_dict(letter, full=True)})


class AdminAgreementVersionsView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, key):
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
        letter = old.letter
        next_num = (letter.versions.aggregate(m=Max("version_number"))["m"] or 0) + 1
        version = AgreementLetterVersion.objects.create(
            letter=letter, version_number=next_num, title=old.title,
            body=old.body,
            change_note=f"Restored from v{old.version_number}",
            created_by=request.user,
        )
        letter.current_version = version
        letter.save(update_fields=["current_version", "updated_at"])
        return Response({"ok": True, **_letter_dict(letter, full=True)})


class PublicAgreementView(APIView):
    """The current agreement text, for the faculty signup / form-fillup screen."""
    permission_classes = [AllowAny]

    def get(self, request, key):
        letter = AgreementLetter.objects.select_related("current_version").filter(key=key).first()
        if not letter or not letter.current_version:
            raise NotFound("Agreement not published yet.")
        return Response(_letter_dict(letter, full=True))


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
