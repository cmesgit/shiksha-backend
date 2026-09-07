"""One flat list of everything a teacher has to look after.

WHY THIS EXISTS
  A teacher's content lives in four apps behind four endpoints with four
  differently-shaped payloads:

      materials    GET /api/materials/teacher/materials/all/
      assignments  GET /api/assignments/teacher/all/
      quizzes      GET /api/teacher/quizzes/all/
      recordings   GET /api/courses/teacher/recordings/all/

  Answering "what have I got on Class 10 Physics?" meant opening four screens
  and holding the answer in your head, and "what have I made?" was not
  answerable at all — see OWNERSHIP below. This endpoint is the union, in one
  vocabulary, filterable and sortable across all four types at once.

  It does NOT replace those four endpoints. They back the per-type screens,
  each of which carries type-specific machinery this list deliberately omits
  (quiz analytics subqueries, assignment submission rosters, recording
  playback tokens). This is the index; those remain the detail.

SCOPING — the rule is "whatever that type already does", not a new one
  Each type keeps its OWN existing scope filter rather than being unified onto
  a single rule, because the per-object permission checks were not unified
  either. `teacher_scope_filter`'s docstring in assignments/views.py spells out
  the consequence of a list that disagrees with its per-object check: the UI
  renders Edit and Delete buttons that can only ever 403.

  So: assignments are batch-aware (an active TeachingAssignment covering the
  assignment's batch); materials, quizzes and recordings are subject-level (any
  active TeachingAssignment on the subject, every batch). That asymmetry is
  real and pre-existing. Unifying it belongs in a migration of the underlying
  rules, not in a read endpoint that would then silently offer actions the
  mutation endpoints refuse.

OWNERSHIP — `is_mine` is not the same question as "can I edit this"
  All four list endpoints return colleagues' content, by design: a co-teacher
  covering a class needs to see and edit the subject's material. `is_mine`
  answers "did I make this", which is a filing question, and is NEVER consulted
  for authorization here or anywhere downstream.

  `owner_name` is None for a genuinely unknown owner — Assignment.created_by
  and Quiz.created_by are both nullable, and rows predating those columns have
  no recoverable author. A NULL owner is never reported as the caller, so
  `mine=true` excludes them rather than sweeping them up.

PAGINATION
  Four heterogeneous querysets cannot be paginated by the database as one
  relation without a UNION over incompatible column sets. Rows are therefore
  filtered and counted per-type in SQL, then merged, sorted and sliced in
  Python. That is bounded by one teacher's own content — the largest holding
  on prod is a teacher with 42 subjects, and the seeded catalogue runs to ~171
  rows per type — but it is NOT unbounded, so MAX_ROWS_PER_TYPE caps each leg
  and `truncated` reports when a cap bit. Silent truncation would read as "you
  have nothing else", which is exactly the failure this screen exists to fix.
"""

import uuid

from django.db.models import Count, Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.display import display_name_for
from accounts.permissions import IsTeacherContext
from assignments.models import Assignment
from assignments.views import teacher_scope_filter
from courses.models_recordings import SessionRecording
from materials.models import StudyMaterial
from quizzes.models import Quiz

# Per-type ceiling. See PAGINATION above — this is a backstop against a
# pathological account, not an expected limit, and a hit is reported rather
# than swallowed.
MAX_ROWS_PER_TYPE = 2000

TYPE_MATERIAL = "material"
TYPE_ASSIGNMENT = "assignment"
TYPE_QUIZ = "quiz"
TYPE_RECORDING = "recording"
ALL_TYPES = (TYPE_MATERIAL, TYPE_ASSIGNMENT, TYPE_QUIZ, TYPE_RECORDING)

# Normalized status vocabulary. The four models express "can a student see
# this" four different ways and with three different defaults (materials have
# no flag at all and are live on upload; assignments and recordings default to
# published; quizzes default to hidden) — see the teacher-resources domain map.
# The hub reports one word so a teacher can scan a mixed list without having to
# remember which model they are looking at.
STATUS_LIVE = "live"          # students can see it now
STATUS_DRAFT = "draft"        # saved, not released
STATUS_PROCESSING = "processing"  # recording still transcoding at Bunny
STATUS_ERROR = "error"        # recording upload/transcode failed


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _subject_scoped(qs, user):
    """The subject-level teacher scope shared by materials/quizzes/recordings.

    distinct() is not optional: a teacher listed twice on one subject (a
    course-wide row plus a batch row, which is a legitimate and common staffing
    shape) would otherwise duplicate every row on that subject.
    """
    return qs.filter(
        subject__teaching_assignments__teacher=user,
        subject__teaching_assignments__is_active=True,
    ).distinct()


class TeacherResourcesView(APIView):
    """GET /api/dashboard/teacher/resources/

    Query parameters, all optional:

        type=material,assignment,quiz,recording   comma-separated; default all
        subject_id=<uuid>                          repeatable
        course_id=<uuid>                           repeatable
        batch_id=<uuid>|none                       "none" = course-wide only
        mine=true                                  only rows I authored
        status=live,draft,processing,error         comma-separated
        q=<text>                                   title/description substring
        limit=<int>   (default 50, max 200)
        offset=<int>

    Response:

        {
          "count": <int>,          total matching, before limit/offset
          "truncated": <bool>,     a per-type cap was hit; count is a floor
          "results": [ <row>, ... ],
          "totals": {"material": n, "assignment": n, "quiz": n, "recording": n}
        }

    `totals` is computed over the filtered set MINUS the type filter, so the
    type chips can show their own counts without the UI having to issue four
    more requests — and without a chip reading 0 merely because it isn't the
    currently selected one.
    """

    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get(self, request):
        user = request.user
        params = request.query_params

        wanted_types = self._parse_types(params.get("type"))
        wanted_status = {
            s.strip().lower()
            for s in (params.get("status") or "").split(",")
            if s.strip()
        }
        subject_ids = self._parse_ids(params, "subject_id")
        course_ids = self._parse_ids(params, "course_id")
        batch_param = (params.get("batch_id") or "").strip()
        mine = _truthy(params.get("mine"))
        search = (params.get("q") or "").strip()

        rows = []
        truncated = False
        # Type counts are built over everything EXCEPT the type filter, so the
        # chips stay meaningful while one of them is active.
        totals = {t: 0 for t in ALL_TYPES}

        for content_type in ALL_TYPES:
            # A type the caller did NOT select contributes only its number to
            # the chips. Building its rows to then call len() on them meant a
            # request for ?type=material still instantiated every assignment,
            # quiz and recording the teacher owns — hundreds of model objects
            # and dicts, discarded — so cost scaled with total holdings rather
            # than with the page being shown.
            #
            # The exception is a status filter: status is derived per row in
            # Python (the four models express it four different ways), so it
            # cannot be counted in SQL and those rows genuinely must be built.
            selected = content_type in wanted_types
            legs, hit_cap = self._collect(
                content_type,
                user=user,
                subject_ids=subject_ids,
                course_ids=course_ids,
                batch_param=batch_param,
                mine=mine,
                search=search,
                wanted_status=wanted_status,
                count_only=not selected and not wanted_status,
            )
            truncated = truncated or hit_cap
            totals[content_type] = legs if isinstance(legs, int) else len(legs)
            if selected:
                rows.extend(legs)

        # Newest first, matching every per-type screen this replaces. created_at
        # is the only ordering column all four models share — three of them have
        # no updated_at, so "recently edited" is not sortable across the union
        # and is deliberately not offered.
        rows.sort(key=lambda r: r["created_at"], reverse=True)

        count = len(rows)
        limit, offset = self._parse_window(params)
        page = rows[offset:offset + limit]

        return Response({
            "count": count,
            "truncated": truncated,
            "results": page,
            "totals": totals,
        })

    # ------------------------------------------------------------------
    # parameter parsing
    # ------------------------------------------------------------------

    def _parse_types(self, raw):
        requested = {
            t.strip().lower() for t in (raw or "").split(",") if t.strip()
        }
        # An unrecognised type name yields the empty set, which would silently
        # return nothing. Fall back to "everything" instead: a typo'd filter
        # showing too much is diagnosable, showing nothing reads as "you have
        # no content".
        valid = requested & set(ALL_TYPES)
        return valid or set(ALL_TYPES)

    def _parse_ids(self, params, key):
        """Comma-separated or repeated UUIDs, silently discarding junk.

        The columns these feed are UUIDFields, and handing the ORM a
        non-UUID string raises django.core.exceptions.ValidationError — which
        DRF's exception handler does NOT translate, so `?subject_id=abc` came
        back as a 500 (verified against a running server, not inferred).

        Dropping unparseable values matches what _parse_types already does with
        an unrecognised type name: a filter the server cannot honour must not
        take the screen down. A caller that sends one real id and one typo gets
        the real one's rows.
        """
        values = []
        for raw in params.getlist(key):
            for candidate in raw.split(","):
                candidate = candidate.strip()
                if not candidate:
                    continue
                try:
                    values.append(str(uuid.UUID(candidate)))
                except (ValueError, AttributeError, TypeError):
                    continue
        return values

    def _parse_window(self, params):
        try:
            limit = int(params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = int(params.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        return max(1, min(limit, 200)), max(0, offset)

    # ------------------------------------------------------------------
    # per-type collection
    # ------------------------------------------------------------------

    def _collect(self, content_type, *, user, subject_ids, course_ids,
                 batch_param, mine, search, wanted_status, count_only=False):
        builder = {
            TYPE_MATERIAL: self._materials,
            TYPE_ASSIGNMENT: self._assignments,
            TYPE_QUIZ: self._quizzes,
            TYPE_RECORDING: self._recordings,
        }[content_type]

        qs = builder(user, mine)

        if subject_ids:
            qs = qs.filter(subject_id__in=subject_ids)
        if course_ids:
            qs = qs.filter(subject__course_id__in=course_ids)
        if batch_param:
            if batch_param.lower() == "none":
                qs = self._filter_course_wide(qs, content_type)
            else:
                # Same UUID guard as _parse_ids, for the same reason: this is
                # compared against a UUID column and a junk value 500s. An
                # unparseable batch is treated as no batch filter rather than
                # as "no results", so a typo shows too much, never nothing.
                try:
                    qs = self._filter_batch(
                        qs, content_type, str(uuid.UUID(batch_param))
                    )
                except (ValueError, AttributeError, TypeError):
                    pass
        if search:
            qs = qs.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )

        if count_only:
            # Returns an int, not a list — the caller only wants the chip
            # number. Capped the same way so a pathological account reports the
            # cap rather than an unbounded count, and reports `truncated`.
            #
            # .values("pk") strips the select_related/annotate the row builders
            # need, so the COUNT does not carry joins nobody will read.
            total = qs.values("pk")[:MAX_ROWS_PER_TYPE + 1].count()
            return min(total, MAX_ROWS_PER_TYPE), total > MAX_ROWS_PER_TYPE

        # +1 so a full page is distinguishable from a truncated one.
        objects = list(qs[:MAX_ROWS_PER_TYPE + 1])
        hit_cap = len(objects) > MAX_ROWS_PER_TYPE
        objects = objects[:MAX_ROWS_PER_TYPE]

        normalizer = {
            TYPE_MATERIAL: self._row_material,
            TYPE_ASSIGNMENT: self._row_assignment,
            TYPE_QUIZ: self._row_quiz,
            TYPE_RECORDING: self._row_recording,
        }[content_type]

        rows = [normalizer(obj, user) for obj in objects]
        # Status is derived per type in Python (the four models disagree on how
        # to express it), so it cannot be a DB filter without four separate
        # translations that would drift from the normalizers below.
        if wanted_status:
            rows = [r for r in rows if r["status"] in wanted_status]
        return rows, hit_cap

    def _filter_batch(self, qs, content_type, batch_id):
        """Rows delivered to `batch_id` — its own, PLUS course-wide.

        Course-wide rows are included because that is what the batch actually
        receives: a NULL batch (or, for quizzes, an empty `batches` M2M) means
        "every batch of this course". Filtering to `batch=X` alone would hide
        the majority of a batch's own content from a teacher asking what that
        batch has been given.
        """
        if content_type == TYPE_QUIZ:
            # Quiz alone uses an M2M where EMPTY means course-wide — the other
            # three use a nullable FK where NULL means the same thing. Reusing
            # the FK form here silently returned nothing.
            return qs.filter(
                Q(batches__id=batch_id) | Q(batches__isnull=True)
            ).distinct()
        return qs.filter(Q(batch_id=batch_id) | Q(batch__isnull=True))

    def _filter_course_wide(self, qs, content_type):
        if content_type == TYPE_QUIZ:
            return qs.filter(batches__isnull=True)
        return qs.filter(batch__isnull=True)

    # ------------------------------------------------------------------
    # querysets — each keeps its own pre-existing scope rule (see module doc)
    # ------------------------------------------------------------------

    def _materials(self, user, mine):
        qs = _subject_scoped(StudyMaterial.objects.all(), user)
        if mine:
            qs = qs.filter(uploaded_by=user)
        return (
            qs.select_related("subject__course", "chapter", "batch",
                              "uploaded_by")
            .annotate(file_count=Count("files", distinct=True))
            .order_by("-created_at")
        )

    def _assignments(self, user, mine):
        # Batch-aware, unlike the other three. See module docstring.
        qs = teacher_scope_filter(Assignment.objects.all(), user).distinct()
        if mine:
            qs = qs.filter(created_by=user)
        return (
            qs.select_related("subject__course", "chapter", "batch",
                              "created_by")
            .annotate(submission_count=Count("submissions", distinct=True))
            .order_by("-created_at")
        )

    def _quizzes(self, user, mine):
        qs = _subject_scoped(Quiz.objects.all(), user)
        if mine:
            qs = qs.filter(created_by=user)
        return (
            # No "chapter"/"batch" in select_related: Quiz has NEITHER. The
            # legacy chapter FK was dropped (migrations 0029/0030) and delivery
            # scope is the `batches` M2M — naming either here is a FieldError,
            # and _base_row's getattr(obj, "chapter_id", None) is what keeps the
            # shared normalizer working across a model that lacks the column.
            qs.select_related("subject__course", "created_by")
            # prefetch_related, NOT select_related — batches is many-to-many.
            # Without it _row_quiz's obj.batches.all() is one query PER ROW:
            # measured 3 queries for 2 quizzes, so a teacher with 100 quizzes
            # paid 101. The count is only used for the row's batch label.
            .prefetch_related("batches")
            .annotate(question_count=Count("questions", distinct=True))
            .order_by("-created_at")
        )

    def _recordings(self, user, mine):
        qs = _subject_scoped(SessionRecording.objects.all(), user)
        if mine:
            qs = qs.filter(uploaded_by=user)
        return (
            qs.select_related("subject__course", "chapter", "batch",
                              "uploaded_by")
            .order_by("-created_at")
        )

    # ------------------------------------------------------------------
    # normalizers — one row shape for all four
    # ------------------------------------------------------------------

    def _base_row(self, obj, content_type, owner, user):
        subject = obj.subject
        course = getattr(subject, "course", None)
        return {
            "id": str(obj.id),
            "type": content_type,
            "title": obj.title,
            "created_at": obj.created_at,
            "subject_id": str(obj.subject_id),
            "subject_name": subject.name,
            "course_id": str(course.id) if course else None,
            "course_title": course.title if course else None,
            "chapter_name": (
                obj.chapter.title if getattr(obj, "chapter_id", None) else None
            ),
            "owner_id": str(owner.id) if owner else None,
            "owner_name": display_name_for(owner),
            # Identity comparison on the id, never on the rendered name — two
            # teachers can share a display name.
            "is_mine": bool(owner and owner.id == user.id),
        }

    def _row_material(self, obj, user):
        row = self._base_row(obj, TYPE_MATERIAL, obj.uploaded_by, user)
        row.update({
            "batch_id": str(obj.batch_id) if obj.batch_id else None,
            "batch_name": obj.batch.name if obj.batch_id else None,
            # StudyMaterial has no publication flag: uploading IS publishing.
            # Reporting "live" is the honest answer, not a placeholder.
            "status": STATUS_LIVE,
            "meta": {"file_count": obj.file_count},
        })
        return row

    def _row_assignment(self, obj, user):
        row = self._base_row(obj, TYPE_ASSIGNMENT, obj.created_by, user)
        row.update({
            "batch_id": str(obj.batch_id) if obj.batch_id else None,
            "batch_name": obj.batch.name if obj.batch_id else None,
            "status": STATUS_LIVE if obj.is_published else STATUS_DRAFT,
            "meta": {
                "due_date": obj.due_date,
                "submission_count": obj.submission_count,
                "max_marks": obj.max_marks,
            },
        })
        return row

    def _row_quiz(self, obj, user):
        row = self._base_row(obj, TYPE_QUIZ, obj.created_by, user)
        # Quiz has no batch FK — an EMPTY batches M2M is course-wide. Reading
        # obj.batch_id here (as the other three do) is an AttributeError, and
        # reporting "no batch" would invert the meaning.
        batches = list(obj.batches.all())
        row.update({
            "batch_id": str(batches[0].id) if len(batches) == 1 else None,
            "batch_name": (
                batches[0].name if len(batches) == 1
                else (f"{len(batches)} batches" if batches else None)
            ),
            # is_assigned, NOT review_status: review_status is the admin's
            # opinion of the questions and is informational only — gating on it
            # would report an unreviewed but assigned quiz as invisible to
            # students when it is in fact live to them.
            "status": STATUS_LIVE if obj.is_assigned else STATUS_DRAFT,
            "meta": {
                "question_count": obj.question_count,
                "review_status": obj.review_status,
                "batch_count": len(batches),
            },
        })
        return row

    def _row_recording(self, obj, user):
        row = self._base_row(obj, TYPE_RECORDING, obj.uploaded_by, user)
        # Two independent gates: Bunny's transcode state AND the teacher's own
        # publish flag. A finished-but-unpublished recording is a draft; an
        # unfinished one is not publishable yet whatever the flag says, so
        # transcode state is reported first.
        if obj.status == 5:
            status = STATUS_ERROR
        elif obj.status != 4:
            status = STATUS_PROCESSING
        elif obj.is_published:
            status = STATUS_LIVE
        else:
            status = STATUS_DRAFT
        row.update({
            "batch_id": str(obj.batch_id) if obj.batch_id else None,
            "batch_name": obj.batch.name if obj.batch_id else None,
            "status": status,
            "meta": {
                "duration_seconds": obj.duration_seconds,
                "session_date": obj.session_date,
            },
        })
        return row
