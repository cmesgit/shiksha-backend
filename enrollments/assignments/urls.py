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
    TeacherAssignmentSubmissionsView,
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
    path(
        "teacher/<uuid:assignment_id>/submissions/",
        TeacherAssignmentSubmissionsView.as_view(),
    ),
    path(
        "teacher/<uuid:assignment_id>/download-all/",
        DownloadAllSubmissionsView.as_view(),
    ),
]
