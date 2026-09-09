"""The admin console's view of academy content, and the pickers to add to it.

WHY THIS EXISTS
  Everything a student is given in the academy — study material, assignments,
  quizzes, recordings — could only ever be created from the teacher dashboard,
  by a teacher who holds an active TeachingAssignment on the subject. An admin
  had no route to it at all: `assignments/` has no admin endpoint of any kind,
  material upload was gated at the class level on the teacher-context claim
  (so a pure admin got "Switch to your teacher profile" before any view code
  ran), and the quiz admin surface is a review queue, not an authoring one.

  The only thing that worked was Django's own /admin/, which writes the row
  directly and therefore skips the file validators, the batch/subject triangle
  guard and the student notification fan-out. Content created that way is
  subtly broken in ways nobody sees until a student can't open it.

  So this module adds the two things the console was missing: one list of all
  academy content, and the option tree needed to file a new piece of it under
  a real teacher.

THE OWNERSHIP RULE — an admin files content UNDER A TEACHER, never under
themselves
  Academy content is reached through teaching staff on every screen that
  displays it: a teacher's lists scope by TeachingAssignment, and the edit and
  delete gates ask whether you teach the subject. Content owned by an admin
  account would therefore be content nobody can look after — visible to
  students, absent from every teacher's screen.

  Every create path here consequently takes a `teacher_id`, and the pre-
  existing staffing guard in each app runs against THAT teacher rather than
  against the admin. See courses.services.resolve_content_author, which is the
  single place that decision is made; the guards themselves were not moved or
  loosened.

  The consequence to design for, not around: an admin cannot add content to a
  subject nobody teaches. That is why the options endpoint reports each
  subject's staffing, so the console can say "assign a teacher first" instead
  of offering a form that can only 400.

WHAT IS NOT HERE
  Quizzes and recordings are readable in the list but not creatable from the
  console.

  Quizzes because attaching questions and making a quiz live both compare
  `quiz.created_by != request.user` (quizzes/views.py, four call sites), so an
  admin who created a quiz for a teacher would immediately be locked out of
  filling it in — a quiz shell with no questions, which is worse than no quiz.
  Making that work means widening those four gates, and it needs the whole
  question builder on the admin side to be worth anything.

  Recordings because no manual create path exists for anyone: a SessionRecording
  is produced by the LiveKit→Bunny egress pipeline, and a hand-made row with a
  fabricated bunny_video_id renders a player pointing at a video that does not
  exist (config/bunny_signing.py never contacts Bunny, so it returns HTTP 200).
"""

import uuid

from django.db.models import Count, Prefetch
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.display import display_name_for
from accounts.permissions import IsAdmin
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment

from .teacher_resources import OWNER_FIELD, TeacherResourcesView


class AdminAcademyResourcesView(TeacherResourcesView):
    """GET /api/dashboard/admin/academy/resources/

    The teacher hub's list, unscoped. Same query parameters, same row shape,
    same status vocabulary — deliberately inherited rather than reimplemented,
    because a second copy of the status derivation is how the same material
    ends up described one way to a teacher and another way to an admin.

    Two differences:

        * no teacher scope. An admin sees every subject's content, including
          subjects with no staff at all, which is the population this screen
          exists to find.
        * `teacher_id=<uuid>` filters by the row's author. `mine=true` still
          works and still means the caller — for an admin that is "content I
          created myself", which after this change is nothing, by design.

    COST, measured rather than assumed: 4 queries for the default page, and 4
    for `?type=material` and `?status=live` alike — no N+1 in any leg.

    But note what a `status=` filter costs in PYTHON. Status is derived per row
    (the four models express it four different ways), so it cannot be a SQL
    filter, and `_collect`'s `count_only` shortcut therefore switches OFF for
    every type when a status is requested — including the three the caller did
    not select. With no teacher scope to narrow them, that is every material,
    assignment, quiz and recording in the database, up to MAX_ROWS_PER_TYPE
    each, instantiated and normalized to return a 50-row page. About 500 rows
    on prod today; the answer at ten times that is to narrow by course or
    subject, which is why those filters are the first two controls on the
    screen. Deriving status in SQL would mean four more translations of the
    rule, drifting from the normalizers — see the parent module's docstring.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    # Class-level default so _owner cannot AttributeError if it is ever reached
    # by a path that did not come through get(). Per-request instances mean the
    # instance attribute set below shadows this and nothing leaks between
    # requests.
    _teacher_filter = None

    def get(self, request):
        # Parsed here rather than in _owner because _owner is called once per
        # type and per request; a malformed id must be rejected once. Stored on
        # self, which is safe: DRF builds a fresh view instance per request.
        raw = (request.query_params.get("teacher_id") or "").strip()
        self._teacher_filter = None
        if raw:
            try:
                self._teacher_filter = str(uuid.UUID(raw))
            except (ValueError, AttributeError, TypeError):
                # Same treatment as every other id filter on this endpoint: an
                # unparseable value is dropped, so a typo shows too much rather
                # than showing nothing and reading as "there is no content".
                self._teacher_filter = None
        return super().get(request)

    def _scope(self, qs, content_type, user):
        # No teaching-assignment join at all. Note what this also removes: the
        # .distinct() the teacher scope needs (a teacher listed twice on one
        # subject duplicated every row). With no join there is nothing to
        # duplicate, and _filter_batch still adds its own distinct() for the
        # quiz M2M.
        return qs

    def _owner(self, qs, content_type, mine, user):
        qs = super()._owner(qs, content_type, mine, user)
        if self._teacher_filter:
            qs = qs.filter(
                **{f"{OWNER_FIELD[content_type]}_id": self._teacher_filter}
            )
        return qs


class AdminAcademyOptionsView(APIView):
    """GET /api/dashboard/admin/academy/options/[?course_id=<uuid>]

    Everything the create forms need to offer a valid choice, in at most two
    requests: courses, then one course's batches, subjects, chapters and
    staffing.

    Without `course_id`:

        {"courses": [{"id", "title", "status", "board_name", "subject_count"}]}

    With `course_id`:

        {
          "course":   {"id", "title", "status"},
          "batches":  [{"id", "name", "code", "year", "is_active"}],
          "subjects": [{
            "id", "name", "order",
            "chapters": [{"id", "title"}],
            "teachers": [{
              "id", "name", "role",
              "course_wide": <bool>,      # an active batch=NULL assignment
              "batch_ids":   [<uuid>, …], # active batch-scoped assignments
            }],
          }],
        }

    `course_wide` and `batch_ids` are not decoration. Materials and quizzes are
    gated on teaches_subject (any active assignment, any batch), but assignments
    are gated on is_teacher_of, which is satisfied only by a batch=NULL row OR a
    row for that exact batch. A form that offered every subject teacher for
    every batch would therefore render choices the assignment endpoint refuses,
    with an error naming a batch the admin did not know was relevant. These two
    fields let the console apply the same rule the server will.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        raw = (request.query_params.get("course_id") or "").strip()
        if not raw:
            return Response({"courses": self._courses()})

        try:
            course_id = uuid.UUID(raw)
        except (ValueError, AttributeError, TypeError):
            # A 400, not a silent fall-back to the course list: this parameter
            # decides the entire shape of the response, so guessing would hand
            # the form a payload it cannot read.
            return Response({"detail": "Not a valid course id."}, status=400)

        course = (
            Course.objects.filter(id=course_id)
            .select_related("board")
            .first()
        )
        if course is None:
            return Response({"detail": "No such course."}, status=404)

        return Response({
            "course": {
                "id": str(course.id),
                "title": course.title,
                "status": course.status,
            },
            "batches": self._batches(course),
            "subjects": self._subjects(course),
        })

    # ------------------------------------------------------------------

    def _courses(self):
        # Every course, including DRAFT and ARCHIVED ones, with the status
        # reported. Filtering them out would hide the two real DRAFT courses on
        # prod, which do own content — and an admin looking for "where did that
        # material go" needs to be able to reach them.
        return [
            {
                "id": str(c.id),
                "title": c.title,
                "status": c.status,
                "board_name": c.board.name if c.board_id else None,
                "subject_count": c.subject_count,
            }
            for c in (
                Course.objects.select_related("board")
                .annotate(subject_count=Count("subjects", distinct=True))
                .order_by("title")
            )
        ]

    def _batches(self, course):
        return [
            {
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "year": b.year,
                "is_active": b.is_active,
            }
            # Batch.Meta.ordering is ["-year", "code"], which is what the
            # teacher UIs show, so it is deliberately not re-sorted here.
            for b in Batch.objects.filter(course=course)
        ]

    def _subjects(self, course):
        subjects = list(
            Subject.objects.filter(course=course)
            .prefetch_related(
                Prefetch(
                    "chapters",
                    queryset=Chapter.objects.order_by("order", "title"),
                ),
            )
            .order_by("order", "name")
        )

        # One query for the whole course's staffing rather than one per
        # subject.
        #
        # The three teacher predicates are not defensive, they are the exact
        # set resolve_content_author will accept — an option this endpoint
        # offers and that endpoint refuses is a form that can only 400, with an
        # error the admin cannot act on:
        #
        #   teacher__isnull=False  TeachingAssignment.teacher is SET_NULL, and
        #                          prod carries ACTIVE PRIMARY rows with nobody
        #                          in them. They would render as a nameless
        #                          option.
        #   is_active=True         a deactivated account cannot log in, so it
        #                          cannot look after what it owns. Assignment
        #                          rows routinely outlive a deactivation.
        #   user_roles…            holding an ACTIVE TeachingAssignment does
        #                          not imply still holding the TEACHER role;
        #                          ending the role does not end the assignment.
        staffing = {}
        rows = (
            TeachingAssignment.objects
            .filter(
                subject__course=course,
                is_active=True,
                teacher__isnull=False,
                teacher__is_active=True,
                teacher__user_roles__role__name="TEACHER",
                teacher__user_roles__is_active=True,
            )
            .select_related("teacher")
            # The user_roles join can multiply rows (an account may hold the
            # role more than once across history), and the accumulation below
            # would then double-count nothing visible but do the work twice.
            .distinct()
            .order_by("order", "id")
        )
        for ta in rows:
            per_subject = staffing.setdefault(ta.subject_id, {})
            entry = per_subject.get(ta.teacher_id)
            if entry is None:
                entry = {
                    "id": str(ta.teacher_id),
                    "name": display_name_for(ta.teacher),
                    "role": ta.role,
                    "course_wide": False,
                    "batch_ids": [],
                }
                per_subject[ta.teacher_id] = entry
            # A teacher can legitimately hold BOTH a course-wide row and
            # batch rows on one subject, so these accumulate instead of
            # overwriting — the last row read must not decide the answer.
            if ta.batch_id is None:
                entry["course_wide"] = True
            else:
                entry["batch_ids"].append(str(ta.batch_id))

        return [
            {
                "id": str(s.id),
                "name": s.name,
                "order": s.order,
                "chapters": [
                    {"id": str(ch.id), "title": ch.title}
                    for ch in s.chapters.all()
                ],
                "teachers": list(staffing.get(s.id, {}).values()),
            }
            for s in subjects
        ]
