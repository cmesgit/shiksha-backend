from django.urls import path
from .views import MyEnrolledCoursesView, CourseSubjectsView
from .views import TeacherMyClassesView

# update
from .views import (
    CreateCourseView,
    MyCoursesView,
    UpdateCourseView,
    DeleteCourseView,
    SubjectDetailView,
    SubjectDashboardView,
    SubjectChaptersView,
    SubjectStudentsView,
    TeacherAllStudentsView,
    SubjectsByCourseTitleView,
)

# recordings
from .views_recordings import (
    SubjectRecordingsView,
    CreateRecordingView,
    DeleteRecordingView,
    CreateVideoSlotView,
    SaveRecordingView,
    RecordingDetailView
)
from .views_progress import (
    GetVideoProgressView,
    SaveVideoProgressView
)

urlpatterns = [

    path("teacher/my-classes/", TeacherMyClassesView.as_view()),
    path("teacher/all-students/", TeacherAllStudentsView.as_view()),
    path("subjects-by-course/", SubjectsByCourseTitleView.as_view()),

    path("", CreateCourseView.as_view()),

    path("mine/", MyCoursesView.as_view()),

    path("my/", MyEnrolledCoursesView.as_view()),

    path("<uuid:course_id>/", UpdateCourseView.as_view()),

    path("<uuid:course_id>/delete/", DeleteCourseView.as_view()),

    path("<uuid:course_id>/subjects/", CourseSubjectsView.as_view()),

    path("subject/<uuid:subject_id>/", SubjectDetailView.as_view()),

    path(
        "subjects/<uuid:subject_id>/dashboard/",
        SubjectDashboardView.as_view()
    ),

    path(
        "subjects/<uuid:subject_id>/chapters/",
        SubjectChaptersView.as_view(),
    ),

    # ============================
    # STUDENTS
    # ============================

    path(
        "subjects/<uuid:subject_id>/students/",
        SubjectStudentsView.as_view(),
    ),

    # ============================
    # RECORDINGS
    # ============================

    path(
        "subjects/<uuid:subject_id>/recordings/",
        SubjectRecordingsView.as_view(),
    ),

    path(
        "subjects/<uuid:subject_id>/recordings/create/",
        CreateRecordingView.as_view(),
    ),

    path(
        "recordings/<uuid:recording_id>/delete/",
        DeleteRecordingView.as_view(),
    ),
    path(
        "recordings/create-video/",
        CreateVideoSlotView.as_view(),
    ),

    path(
        "subjects/<uuid:subject_id>/recordings/save/",
        SaveRecordingView.as_view(),
    ),

    path(
        "recordings/<uuid:recording_id>/progress/",
        GetVideoProgressView.as_view(),
    ),

    path(
        "recordings/<uuid:recording_id>/progress/save/",
        SaveVideoProgressView.as_view(),
    ),
    path(
        "recordings/<uuid:recording_id>/",
        RecordingDetailView.as_view(),
    ),
]
