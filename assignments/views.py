from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Prefetch, Count, Case, When, Value, CharField, Q, F
from django.db import IntegrityError
from django.http import HttpResponse

from courses.models import Subject, TeachingAssignment, Batch
from courses.services import teaches_subject, is_teacher_of
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from enrollments.models import Enrollment

from courses.models import Chapter
from accounts.permissions import require_teacher_context, IsTeacherContext, _in_teacher_context
from accounts.auth_flow import get_active_profile

from .models import Assignment, AssignmentFile, AssignmentSubmission
from .serializers import (
    AssignmentListSerializer,
    AssignmentDetailSerializer,
    TeacherAssignmentCreateSerializer,
    TeacherAssignmentUpdateSerializer,
    TeacherAssignmentListSerializer,
    TeacherSubmissionListSerializer,
    AssignmentFileSerializer,
    validate_assignment_file,
    validate_submission_file,
)

import zipfile
from io import BytesIO


# ==========================================
# HELPER
# ==========================================

def _assert_teacher_owns_assignment(user, assignment):
    """Raises PermissionDenied unless this teacher may act on `assignment`.

    ── The scope decision, spelled out once ──────────────────────────────
    This used to gate on `teaches_subject()` (subject-level) while the CREATE
    path gated on the batch-aware `is_teacher_of()`. The asymmetry was not a
    design: a teacher staffed on Maths/10-B could list, open, download,
    re-grade, edit and DELETE every assignment belonging to Maths/10-A, and
    could not create one — the one operation that happened to be checked
    properly. Read and write are now both batch-aware, because "may grade
    another class's students" is not a privilege anyone intended to grant.

    A TeachingAssignment with `batch IS NULL` is course-wide and covers every
    batch, so a genuinely course-wide teacher loses nothing here — that is
    exactly what `is_teacher_of()` already encodes.

    Assignments with `batch IS NULL` are the legacy/course-wide rows (the
    model allows them, the create serializer no longer produces them). There
    is no batch to scope those to, so subject level is the only check
    available and they keep the old rule.
    """
    subject = assignment.subject

    if assignment.batch_id is None:
        if not teaches_subject(user, subject):
            raise PermissionDenied("Not assigned to this subject.")
        return

    if not is_teacher_of(user, assignment.batch, subject):
        raise PermissionDenied(
            "Not assigned to this subject in this batch."
        )


def _assert_learner_may_see_assignment(learner, assignment):
    """Raises Http404 unless `assignment` is one this learner is actually set.

    The two list endpoints (`CourseAssignmentsView`, `SubjectAssignmentsView`)
    have always enforced `is_published` plus batch isolation. The single-object
    endpoints — detail and submit — enforced neither: they resolved the row by
    UUID and checked only that the account had a live subscription to the
    course. Any subscribed learner holding a UUID could therefore read an
    unpublished DRAFT in full (title, description, every attachment URL) and
    submit to it, and a Batch-B learner could read and submit to a Batch-A
    assignment carrying a different due date.

    404 rather than 403 on purpose: a draft the learner has no business
    knowing about must not be confirmed to exist by the shape of the error.
    """
    from django.http import Http404
    from enrollments.services import active_batch_id

    if not assignment.is_published:
        raise Http404

    if assignment.batch_id is None:
        # Course-wide: every batch of the course sees it.
        return

    batch_id = active_batch_id(
        learner_profile=learner,
        course_id=assignment.subject.course_id,
    )
    # batch_id None = not placed in a batch yet. The list endpoints
    # deliberately degrade to showing EVERYTHING in that case rather than
    # hiding every batch-scoped assignment from an unplaced student; this
    # must match them or a row the learner can see would 404 on click.
    if batch_id is not None and batch_id != assignment.batch_id:
        raise Http404


def teacher_scope_filter(qs, user):
    """Restrict an Assignment queryset to what `user` may act on.

    The queryset-shaped twin of `_assert_teacher_owns_assignment` — the list
    endpoints have to agree with the per-object check or the UI goes back to
    offering Edit/Delete/Review buttons that 403.

    All three conditions ride the SAME `subject__teaching_assignments`
    join because they sit in one `filter()` call, so a row qualifies only when
    ONE teaching assignment satisfies teacher + active + scope together —
    not when three different rows each satisfy one clause.
    """
    return qs.filter(
        Q(batch__isnull=True)                                            # course-wide assignment
        | Q(subject__teaching_assignments__batch__isnull=True)  # course-wide staffing
        | Q(subject__teaching_assignments__batch=F("batch")),   # same batch
        subject__teaching_assignments__teacher=user,
        subject__teaching_assignments__is_active=True,
    )


# ==========================================
# ASSIGNMENT DETAIL VIEW
# ==========================================

class AssignmentDetailView(generics.RetrieveAPIView):
    serializer_class = AssignmentDetailSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"
    lookup_url_kwarg = "assignment_id"

    def get_queryset(self):
        # The "already submitted" card must reflect the ACTIVE LEARNER
        # PROFILE, not any submission by the account. In teacher context
        # (no active profile) the prefetch is empty, which is correct —
        # teachers read submissions through their own roster views.
        learner = get_active_profile(self.request)
        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(learner_profile=learner)
            if learner is not None
            else AssignmentSubmission.objects.none(),
            to_attr="user_submission_list",
        )
        return (
            Assignment.objects
            .select_related("subject__course__board", "chapter")
            .prefetch_related(submission_prefetch, "files")
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user
        subject = instance.subject
        course = subject.course

        instance.user_submission = (
            instance.user_submission_list[0]
            if instance.user_submission_list else None
        )

        if _in_teacher_context(request):
            _assert_teacher_owns_assignment(user, instance)
        else:
            from enrollments.services import has_active_subscription, lock_payload
            from accounts.auth_flow import get_active_profile

            learner = get_active_profile(request)
            if learner is None:
                return Response(
                    {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                    status=403,
                )
            if not has_active_subscription(user=user, course=course, learner_profile=learner):
                return Response(
                    lock_payload(user=user, course=course, learner_profile=learner),
                    status=402,
                )
            _assert_learner_may_see_assignment(learner, instance)

        serializer = self.get_serializer(instance)
        return Response(serializer.data)


# ==========================================
# SUBMIT ASSIGNMENT VIEW
# ==========================================

class SubmitAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, assignment_id):
        assignment = get_object_or_404(
            Assignment.objects.select_related("subject__course__board", "chapter"),
            id=assignment_id,
        )

        from enrollments.services import has_active_subscription, lock_payload
        from accounts.auth_flow import get_active_profile

        course = assignment.subject.course
        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )
        if not has_active_subscription(user=request.user, course=course, learner_profile=learner):
            return Response(
                lock_payload(user=request.user, course=course, learner_profile=learner),
                status=402,
            )

        # Same gate the detail view applies — a draft or another batch's
        # assignment must not be submittable just because its UUID leaked.
        _assert_learner_may_see_assignment(learner, assignment)

        file = request.FILES.get("file")
        if not file:
            return Response({"detail": "File required."}, status=status.HTTP_400_BAD_REQUEST)

        # This upload used to go onto the model unchecked — see
        # serializers.validate_submission_file for the whole story. Raises a
        # DRF ValidationError, which the exception handler renders as a 400.
        try:
            validate_submission_file(file)
        except ValidationError as exc:
            return Response({"detail": exc.detail[0] if isinstance(exc.detail, list) else exc.detail},
                            status=status.HTTP_400_BAD_REQUEST)

        # Keyed on (assignment, learner_profile): re-submitting replaces only
        # THIS learner's file. Previously the key was (assignment, account),
        # so one child's upload silently overwrote a sibling's.
        #
        # submitted_at is set explicitly now that the field is no longer
        # auto_now (see models.py) — a resubmission SHOULD re-stamp the clock,
        # but an unrelated save (grading, a management command) must not.
        submission, created = AssignmentSubmission.objects.update_or_create(
            assignment=assignment,
            learner_profile=learner,
            defaults={
                "submitted_file": file,
                "student": request.user,
                "submitted_at": timezone.now(),
            },
        )

        return Response(
            {"detail": "Submission successful.", "resubmitted": not created},
            status=status.HTTP_200_OK,
        )


# ==========================================
# COURSE ASSIGNMENTS LIST VIEW
# ==========================================

class CourseAssignmentsView(generics.ListAPIView):
    serializer_class = AssignmentListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        course_id = self.kwargs["course_id"]
        user = self.request.user
        learner = get_active_profile(self.request)

        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(learner_profile=learner)
            if learner is not None
            else AssignmentSubmission.objects.none(),
            to_attr="user_submission_list",
        )

        if _in_teacher_context(self.request):
            queryset = Assignment.objects.filter(
                subject__course__id=course_id,
                subject__teaching_assignments__teacher=user,
                subject__teaching_assignments__is_active=True,
            )
        else:
            from courses.models import Course
            from enrollments.services import has_active_subscription

            try:
                course_obj = Course.objects.get(pk=course_id)
            except Course.DoesNotExist:
                raise PermissionDenied("Course not found.")
            if not has_active_subscription(user=user, course=course_obj, learner_profile=learner):
                raise PermissionDenied("Your subscription for this course has expired.")
            # is_published: drafts are the teacher's alone. The branch above
            # deliberately does NOT filter on it — a teacher must see their
            # own drafts in order to finish and publish them.
            queryset = Assignment.objects.filter(
                subject__course__id=course_id, is_published=True)
            # Batch isolation: show course-wide assignments (batch IS NULL) plus
            # this student's own batch's assignments. Due dates are cohort-
            # relative, so a later batch must not inherit an earlier one's.
            # The batch is the ACTIVE PROFILE's — two children in the same
            # course can sit in different batches.
            enrollment = Enrollment.objects.filter(
                learner_profile=learner, course_id=course_id,
                status=Enrollment.STATUS_ACTIVE,
            ).first()
            batch_id = enrollment.batch_id if enrollment else None
            # No batch on the enrollment (e.g. self-enrolled, not yet placed
            # by an admin) means we can't tell which cohort's assignments
            # apply — show every assignment in the course rather than only
            # the course-wide (batch IS NULL) ones, which would otherwise
            # hide every batch-scoped assignment from most students.
            if batch_id is not None:
                queryset = queryset.filter(
                    Q(batch__isnull=True) | Q(batch_id=batch_id))

        return (
            queryset
            .select_related("subject__course__board", "chapter")
            .prefetch_related(submission_prefetch)
            .distinct()
        )

    def list(self, request, *args, **kwargs):
        queryset = list(self.get_queryset())
        for obj in queryset:
            obj.user_submission = (
                obj.user_submission_list[0] if obj.user_submission_list else None
            )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


# ==========================================
# TEACHER CREATE ASSIGNMENT VIEW
# — Idempotency guard prevents double-submit
# ==========================================

class TeacherCreateAssignmentView(APIView):
    permission_classes = [IsAuthenticated]
    # JSONParser alongside the multipart parsers: an assignment can carry
    # file attachments (hence multipart), but `chapter_tags` is a list of
    # objects, which multipart cannot express natively. A client with no
    # files to upload can now POST plain JSON; one with files sends
    # multipart and encodes chapter_tags as a JSON string, which the
    # serializer mixin decodes.
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        user = request.user

        require_teacher_context(request)

        # ── Idempotency check ────────────────────────────────────────
        # Frontend sends a per-session UUID so accidental double-clicks
        # return the existing assignment rather than creating a second one.
        idempotency_key = request.data.get(
            "idempotency_key") or request.headers.get("X-Idempotency-Key")

        if idempotency_key:
            existing = Assignment.objects.filter(
                idempotency_key=idempotency_key).first()
            if existing:
                return Response(
                    {
                        "message": "Assignment already created.",
                        "id": str(existing.id),
                        "duplicate": True,
                    },
                    status=status.HTTP_200_OK,
                )

        # Extra files, validated BEFORE the assignment row is written.
        #
        # Only the first upload (sent as `attachment`) ever reached a
        # validator: the rest were read straight out of
        # request.FILES.getlist("files") and handed to
        # AssignmentFile.objects.create() below, so BLOCKED_EXTENSIONS and the
        # 100 MB cap simply did not apply to them and a `.exe` was stored and
        # served to every student in the batch. The edit path (`new_files`)
        # was always checked; only create had the hole.
        #
        # Checked up here rather than in the loop so a bad second file 400s
        # instead of leaving a half-attached assignment behind.
        extra_files = request.FILES.getlist("files")
        for f in extra_files:
            validate_assignment_file(f)

        serializer = TeacherAssignmentCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        try:
            assignment = serializer.save()
        except IntegrityError:
            # Race condition: two requests with same key hit simultaneously
            existing = Assignment.objects.filter(
                idempotency_key=idempotency_key).first()
            if existing:
                return Response(
                    {"message": "Assignment already created.",
                        "id": str(existing.id), "duplicate": True},
                    status=status.HTTP_200_OK,
                )
            raise

        # Handle additional uploaded files (multi-file support) — validated above
        for f in extra_files:
            AssignmentFile.objects.create(
                assignment=assignment,
                file=f,
                original_filename=f.name,
            )

        return Response(
            {"message": "Assignment created successfully",
                "id": str(assignment.id)},
            status=status.HTTP_201_CREATED,
        )


# ==========================================
# TEACHER UPDATE ASSIGNMENT VIEW
# ==========================================

class TeacherUpdateAssignmentView(APIView):
    permission_classes = [IsAuthenticated]
    # See TeacherCreateAssignmentView on why JSONParser is here too.
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, assignment_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related(
                "subject__course__board", "chapter").prefetch_related("files"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        if assignment.due_date < timezone.now():
            return Response(
                {"detail": "Cannot edit an expired assignment."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Merge uploaded files list into validated data manually because
        # DRF's ListField doesn't auto-grab from request.FILES.getlist().
        data = request.data.copy()
        new_files = request.FILES.getlist("new_files")

        serializer = TeacherAssignmentUpdateSerializer(
            assignment, data=data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        # Inject the file list after validation so the serializer's
        # update() method receives them.
        serializer.validated_data["new_files"] = new_files

        updated = serializer.save()

        return Response(
            {
                "message": "Assignment updated successfully",
                "data": TeacherAssignmentListSerializer(
                    updated, context={"request": request}
                ).data,
            }
        )


# ==========================================
# TEACHER DELETE SINGLE FILE VIEW
# DELETE /assignments/teacher/<assignment_id>/files/<file_id>/
# ==========================================

class TeacherDeleteAssignmentFileView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, assignment_id, file_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related("subject", "chapter"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        file_obj = get_object_or_404(
            AssignmentFile, id=file_id, assignment=assignment)
        file_obj.file.delete(save=False)  # remove from storage
        file_obj.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


# ==========================================
# TEACHER DELETE ASSIGNMENT VIEW
# ==========================================

class TeacherDeleteAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, assignment_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related("subject", "chapter"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        if assignment.submissions.exists():
            return Response(
                {"detail": "Cannot delete an assignment that already has submissions."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assignment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ==========================================
# TEACHER SUBJECT ASSIGNMENTS VIEW
# ==========================================

class TeacherSubjectAssignmentsView(generics.ListAPIView):
    serializer_class = TeacherAssignmentListSerializer
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get_queryset(self):
        user = self.request.user
        subject_id = self.kwargs["subject_id"]

        subject = get_object_or_404(Subject, id=subject_id)

        if not teaches_subject(user, subject):
            raise PermissionDenied("Not assigned to this subject.")

        # teacher_scope_filter, not a bare subject filter: a teacher staffed
        # only on 10-B used to get 10-A's assignments listed here, complete
        # with working Edit / Delete / Review buttons.
        return (
            teacher_scope_filter(
                Assignment.objects.filter(subject=subject), user)
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files")
            .annotate(total_submissions=Count("submissions", distinct=True))
            # distinct(): a teacher holding two staffing rows on one subject
            # would otherwise see every assignment twice.
            .distinct()
            .order_by("-created_at")
        )


# ==========================================
# TEACHER — BATCHES THEY MAY SET WORK FOR
# ==========================================

class TeacherAssignableBatchesView(APIView):
    """The batches this teacher can actually post an assignment to, for one
    subject.

    The create form's batch picker was fed by
    `courses.TeacherSubjectBatchesView`, which lists every active batch of the
    course filtered on `course_id` + `is_active` only, and the form
    auto-selected `list[0]`. A teacher staffed on 9-B alone got 9-A
    preselected, filled the whole form, and hit a 400 — for a batch they never
    chose. This endpoint answers the question the picker is actually asking:
    which batches may I set work for?

    Deliberately lives here rather than in courses/: the rule it encodes is
    the assignment CREATE rule (`is_teacher_of`, via
    TeacherAssignmentCreateSerializer.validate), and it has to move with that
    rule. The courses endpoint stays as-is for the live-session picker, which
    has its own scoping rules.
    """
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id)

        if not teaches_subject(request.user, subject):
            raise PermissionDenied("Not assigned to this subject.")

        batches = Batch.objects.filter(
            course_id=subject.course_id, is_active=True)

        # A course-wide staffing row (batch IS NULL) covers every batch, so
        # don't narrow in that case — is_teacher_of() encodes the same rule
        # and this must not disagree with it.
        course_wide = TeachingAssignment.objects.filter(
            subject=subject, teacher=request.user,
            is_active=True, batch__isnull=True,
        ).exists()
        if not course_wide:
            batches = batches.filter(
                teaching_assignments__subject=subject,
                teaching_assignments__teacher=request.user,
                teaching_assignments__is_active=True,
            ).distinct()

        batches = batches.order_by("-year", "code")
        return Response([
            {
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "year": b.year,
            }
            for b in batches
        ])


# ==========================================
# TEACHER — ALL ASSIGNMENTS ACROSS THEIR SUBJECTS
# ==========================================

class TeacherAllAssignmentsView(generics.ListAPIView):
    """Every assignment across every subject this teacher is assigned to.

    The faculty Assignments screen is one flat, subject-filtered list (design
    handoff screen 11). Before this existed the frontend called
    TeacherSubjectAssignmentsView once per subject and flattened client-side —
    an N+1 that grew with the teacher's timetable.

    Scope is the same as the per-object permission check, expressed as a
    filter instead: an active TeachingAssignment for this user on the
    assignment's subject AND covering its batch (see teacher_scope_filter).
    Before that, this filtered on the subject alone, so a teacher staffed on
    one batch listed every other batch's work as if it were theirs.
    """

    serializer_class = TeacherAssignmentListSerializer
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get_queryset(self):
        return (
            teacher_scope_filter(Assignment.objects.all(), self.request.user)
            # subject + batch: the serializer reports subject_id/
            # subject_name and batch_id/batch_name off these.
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files")
            .annotate(total_submissions=Count("submissions", distinct=True))
            # distinct(): a teacher listed twice on one subject would otherwise
            # duplicate every assignment on it.
            .distinct()
            .order_by("-created_at")
        )


# ==========================================
# TEACHER ASSIGNMENT SUBMISSIONS VIEW
# ==========================================

class TeacherAssignmentSubmissionsView(APIView):
    """Every student the assignment was set for, submitted or not.

    This used to be a plain ListAPIView over AssignmentSubmission, and that
    made the screen it backs structurally incapable of doing its job. A
    submission row only exists once a student has submitted, and
    `submitted_file` is a non-null FileField, so the frontend's
    `s.submitted_file ? "Submitted" : "Pending"` was a constant: a batch of 32
    where 18 had submitted rendered "18/18 Submitted · 0 Pending · 100%", and
    the 14 students who had NOT handed in — the only reason to open this
    screen — were absent from the response entirely.

    The roster is the assignment's own cohort: active enrollments in its
    course, narrowed to its batch when it has one. Rows are the same shape
    either way, with `id: null` and `submitted_file: null` marking a
    non-submitter (the grade endpoint takes a submission id, so there is
    nothing to grade on those and the UI disables the control).

    Still returns a BARE ARRAY, not a paginated envelope — the client indexes
    it directly, and a roster is bounded by class size.
    """
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get(self, request, assignment_id):
        assignment = get_object_or_404(
            Assignment.objects.select_related("subject__course", "chapter"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(request.user, assignment)

        submissions = (
            AssignmentSubmission.objects
            .filter(assignment=assignment)
            .select_related("student", "learner_profile", "assignment")
            .annotate(
                submission_status=Case(
                    When(submitted_at__gt=assignment.due_date, then=Value("Late")),
                    default=Value("On time"),
                    output_field=CharField(),
                )
            )
            .order_by("-submitted_at")
        )
        rows = TeacherSubmissionListSerializer(
            submissions, many=True, context={"request": request}).data

        # Which students the row already covers. Legacy submissions carry
        # learner_profile=NULL, so an account key has to stand in for those or
        # their author would be listed twice — once as a submission and once
        # as a non-submitter.
        submitted_profiles = {
            str(s.learner_profile_id) for s in submissions if s.learner_profile_id
        }
        submitted_accounts = {
            str(s.student_id) for s in submissions if not s.learner_profile_id
        }

        # The roster helpers live in courses.views: legacy enrollments have
        # learner_profile=NULL and resolve to the account's DEFAULT profile,
        # and getting that rule subtly different here would put the same
        # student on the roster under two names. Imported at call time —
        # courses.views imports from enrollments, which imports from courses.
        from courses.views import (
            _resolve_enrollment_profiles, _roster_row, _roster_sort_key,
        )

        roster_qs = Enrollment.objects.filter(
            course_id=assignment.subject.course_id,
            status=Enrollment.STATUS_ACTIVE,
        ).select_related("user", "learner_profile")
        if assignment.batch_id is not None:
            # A batch-scoped assignment was only ever set for its own cohort;
            # listing the whole course as "pending" would invent 200 missing
            # submissions. batch IS NULL on the assignment means course-wide,
            # which is the whole course — matching the student-facing filter.
            roster_qs = roster_qs.filter(batch_id=assignment.batch_id)

        enrollments = list(roster_qs)
        profiles = _resolve_enrollment_profiles(enrollments)

        pending = []
        seen = set()
        for enrollment in enrollments:
            profile = profiles.get(enrollment.id)
            key = (("profile", str(profile.id)) if profile is not None
                   else ("account", str(enrollment.user_id)))
            if key in seen:
                continue
            seen.add(key)
            if key[0] == "profile" and key[1] in submitted_profiles:
                continue
            if key[0] == "account" and key[1] in submitted_accounts:
                continue
            # A legacy submission by this account is keyed on the account, but
            # the enrollment may now resolve to a profile — treat the account
            # match as covering it either way, so one student is one row.
            if str(enrollment.user_id) in submitted_accounts:
                continue

            info = _roster_row(enrollment, profile)
            pending.append((_roster_sort_key(info), {
                "id": None,
                "student_id": info["account_id"],
                "student_email": info["email"],
                "student_name": (info["full_name"] or info["display_name"]
                                 or info["username"] or info["email"]),
                "learner_profile_id": info["id"],
                "submitted_file": None,
                "submitted_at": None,
                "submission_status": "Not submitted",
                "marks_obtained": None,
                "max_marks": assignment.max_marks,
                "feedback": "",
                "graded_at": None,
            }))

        pending.sort(key=lambda pair: pair[0])
        return Response(rows + [row for _, row in pending])


class TeacherGradeSubmissionView(APIView):
    """
    POST /assignments/teacher/submissions/<submission_id>/grade/
    body: { marks_obtained: int, feedback?: str }

    Lets the assignment's teacher record a grade + written feedback on a real
    submission — the gap the student-facing "You'll receive feedback once
    graded" promise depended on but that had no backend support at all.
    Re-gradeable (a teacher fixing a mistake just POSTs again); each call
    re-stamps graded_at/graded_by and re-notifies the student.
    """
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def post(self, request, submission_id):
        submission = get_object_or_404(
            AssignmentSubmission.objects.select_related(
                "assignment", "assignment__subject", "assignment__chapter", "learner_profile__account",
            ),
            id=submission_id,
        )
        _assert_teacher_owns_assignment(request.user, submission.assignment)

        marks = request.data.get("marks_obtained")
        if marks in (None, ""):
            raise ValidationError({"marks_obtained": "This field is required."})
        try:
            marks = int(marks)
        except (TypeError, ValueError):
            raise ValidationError({"marks_obtained": "Must be a whole number."})
        max_marks = submission.assignment.max_marks
        if marks < 0 or marks > max_marks:
            raise ValidationError(
                {"marks_obtained": f"Must be between 0 and {max_marks}."}
            )

        submission.marks_obtained = marks
        submission.feedback = (request.data.get("feedback") or "").strip()
        submission.graded_at = timezone.now()
        submission.graded_by = request.user
        submission.save(update_fields=["marks_obtained", "feedback", "graded_at", "graded_by"])

        _notify_graded(submission)

        return Response({
            "detail": "Graded.",
            "marks_obtained": submission.marks_obtained,
            "max_marks": max_marks,
            "feedback": submission.feedback,
            "graded_at": submission.graded_at,
        })


def _notify_graded(submission):
    """Best-effort — a notification failure must never break grading itself."""
    try:
        from django.contrib.contenttypes.models import ContentType
        from activity.models import Activity
        from livestream.services.notifications import push_ws_notification

        recipient = submission.learner_profile.account if submission.learner_profile else submission.student
        title = f"✅ Your assignment \"{submission.assignment.title}\" was graded — {submission.marks_obtained}/{submission.assignment.max_marks}"
        content_type = ContentType.objects.get_for_model(AssignmentSubmission)
        activity, created = Activity.objects.update_or_create(
            user=recipient,
            type=Activity.TYPE_ASSIGNMENT,
            content_type=content_type,
            object_id=submission.id,
            defaults={
                "title": title,
                "subject_name": submission.assignment.title,
                "audience": Activity.AUDIENCE_LEARNER,
                "learner_profile": submission.learner_profile,
                "is_read": False,
                "created_at": timezone.now(),
            },
        )
        # Durable row alongside the Activity+WS pair. Grading is exactly
        # the kind of event a student is usually offline for; until now it
        # existed only as a fire-and-forget frame. push_ws=False because
        # the frame below already carries the type/id the bell routes on.
        from notifications.services import notify
        subject_id = getattr(
            getattr(submission.assignment, "chapter", None), "subject_id", None)
        notify(
            recipient=recipient,
            actor=submission.graded_by,
            verb="assignment.graded",
            title=title,
            link_url=(f"/subjects/{subject_id}/assignments/{submission.assignment_id}"
                      if subject_id else "/assignments"),
            payload={"submission_id": str(submission.id),
                     "assignment_id": str(submission.assignment_id)},
            audience_identity=(f"L:{submission.learner_profile_id}"
                               if submission.learner_profile_id else ""),
            learner_profile=submission.learner_profile,
            push_ws=False,
        )

        push_ws_notification(recipient.id, {
            "type": "ASSIGNMENT",
            "title": title,
            "subject_name": submission.assignment.title,
            "id": str(submission.id),
            "is_read": False,
            "created_at": activity.created_at.isoformat(),
            "track": "academy",
        })
    except Exception:
        pass


# ==========================================
# SUBJECT ASSIGNMENTS VIEW (STUDENT)
# ==========================================

class SubjectAssignmentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        user = request.user
        learner = get_active_profile(request)

        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id)

        from enrollments.services import has_active_subscription

        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )
        if not has_active_subscription(user=user, course=subject.course, learner_profile=learner):
            raise PermissionDenied("Your subscription for this course has expired.")

        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(learner_profile=learner)
            if learner is not None
            else AssignmentSubmission.objects.none(),
            to_attr="user_submission_list",
        )

        teacher_prefetch = Prefetch(
            "subject__teaching_assignments",
            queryset=TeachingAssignment.objects.filter(
                batch__isnull=True, is_active=True,
            ).select_related("teacher").order_by("order"),
            to_attr="prefetched_teachers",
        )

        assignments = (
            Assignment.objects
            # Student-facing view — drafts stay hidden until published.
            .filter(subject_id=subject_id, is_published=True)
            .select_related("subject__course__board", "chapter")
            .prefetch_related(submission_prefetch, teacher_prefetch, "files")
        )

        # Same batch isolation CourseAssignmentsView enforces: course-wide
        # assignments (batch IS NULL) plus this student's own batch. No batch
        # on the enrollment (not yet placed by an admin) shows every
        # assignment in the subject, matching CourseAssignmentsView's
        # degrade-to-everything behavior for an unplaced student.
        enrollment = Enrollment.objects.filter(
            learner_profile=learner, course_id=subject.course_id,
            status=Enrollment.STATUS_ACTIVE,
        ).first()
        batch_id = enrollment.batch_id if enrollment else None
        if batch_id is not None:
            assignments = assignments.filter(
                Q(batch__isnull=True) | Q(batch_id=batch_id))

        data = []
        for assignment in assignments:
            submission = (
                assignment.user_submission_list[0]
                if assignment.user_submission_list else None
            )

            teachers = assignment.subject.prefetched_teachers
            teacher_name = (
                (lambda p: p.full_name if p else None)(teachers[0].teacher.default_learner_profile()) if teachers else None if teachers else None
            )

            data.append({
                "id": assignment.id,
                "title": assignment.title,
                "due_date": assignment.due_date,
                "status": "SUBMITTED" if submission else "PENDING",
                "subject": assignment.subject.name,
                # chapter is optional now — null rather than an AttributeError.
                "chapter": assignment.chapter.title if assignment.chapter_id else None,
                "teacher": teacher_name,
            })

        return Response(data)


# ==========================================
# DOWNLOAD ALL SUBMISSIONS VIEW
# ==========================================

class DownloadAllSubmissionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, assignment_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related("subject", "chapter"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        submissions = (
            AssignmentSubmission.objects
            .filter(assignment=assignment)
            .select_related("student", "learner_profile")
        )

        buffer = BytesIO()
        used_names = set()
        with zipfile.ZipFile(buffer, "w") as zf:
            for sub in submissions:
                if sub.submitted_file:
                    # Name by the learner who submitted it — on a shared
                    # family account the default profile would mislabel a
                    # sibling's work.
                    lp = sub.learner_profile or sub.student.default_learner_profile()
                    student_name = (
                        (lp.full_name or lp.display_name) if lp else sub.student.email
                    ) or sub.student.email
                    filename = f"{student_name}_{sub.submitted_file.name.split('/')[-1]}"
                    # Two learners can now legitimately upload files with the
                    # same name; ZipFile silently accepts duplicate entries,
                    # so dedupe explicitly.
                    if filename in used_names:
                        stem, dot, ext = filename.rpartition(".")
                        base = stem if dot else filename
                        suffix = f".{ext}" if dot else ""
                        n = 2
                        while f"{base}_{n}{suffix}" in used_names:
                            n += 1
                        filename = f"{base}_{n}{suffix}"
                    used_names.add(filename)
                    zf.writestr(filename, sub.submitted_file.read())

        response = HttpResponse(
            buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = (
            f'attachment; filename="{assignment.title}_submissions.zip"'
        )
        return response
