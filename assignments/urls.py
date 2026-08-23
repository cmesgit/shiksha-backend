from django.urls import path
from .views import (
    CourseAssignmentsView,
    AssignmentDetailView,
    SubmitAssignmentView,
    TeacherCreateAssignmentView,
    TeacherUpdateAssignmentView,
    TeacherDeleteAssignmentView,
    TeacherDeleteAssignmentFileView,
    TeacherSubjectAssignmentsView,
    TeacherAssignableBatchesView,
    TeacherAllAssignmentsView,
    TeacherAssignmentSubmissionsView,
    TeacherGradeSubmissionView,
    SubjectAssignmentsView,
    DownloadAllSubmissionsView,
)

urlpatterns = [
    # ── Student ────────────────────────────────────────────────────────
    path(
        "courses/<uuid:course_id>/",
        CourseAssignmentsView.as_view(),
    ),
    path(
        "<uuid:assignment_id>/",
        AssignmentDetailView.as_view(),
    ),
    path(
        "<uuid:assignment_id>/submit/",
        SubmitAssignmentView.as_view(),
    ),
    path(
        "subject/<uuid:subject_id>/",
        SubjectAssignmentsView.as_view(),
    ),

    # Flat: every assignment across the subjects this teacher is assigned to.
    path(
        "teacher/all/",
        TeacherAllAssignmentsView.as_view(),
    ),

    # ── Teacher — assignment CRUD ──────────────────────────────────────
    path(
        "teacher/create/",
        TeacherCreateAssignmentView.as_view(),
    ),
    path(
        "teacher/<uuid:assignment_id>/edit/",
        TeacherUpdateAssignmentView.as_view(),
    ),
    path(
        "teacher/<uuid:assignment_id>/delete/",
        TeacherDeleteAssignmentView.as_view(),
    ),

    # ── Teacher — file management ──────────────────────────────────────
    # DELETE a single attached file (by AssignmentFile UUID)
    path(
        "teacher/<uuid:assignment_id>/files/<uuid:file_id>/",
        TeacherDeleteAssignmentFileView.as_view(),
    ),

    # ── Teacher — list & submissions ──────────────────────────────────
    path(
        "teacher/subject/<uuid:subject_id>/",
        TeacherSubjectAssignmentsView.as_view(),
    ),
    # Batches this teacher may actually SET WORK for — the create form's
    # picker. Distinct from courses/subjects/<id>/batches/, which lists every
    # active batch of the course regardless of staffing.
    path(
        "teacher/subject/<uuid:subject_id>/batches/",
        TeacherAssignableBatchesView.as_view(),
    ),
    path(
        "teacher/<uuid:assignment_id>/submissions/",
        TeacherAssignmentSubmissionsView.as_view(),
    ),
    path(
        "teacher/submissions/<uuid:submission_id>/grade/",
        TeacherGradeSubmissionView.as_view(),
    ),
    path(
        "teacher/<uuid:assignment_id>/download-all/",
        DownloadAllSubmissionsView.as_view(),
    ),
]
