from django.urls import path
from .views import MyEnrolledCoursesView, CourseSubjectsView
from .views import TeacherMyClassesView
from .views import CourseCatalogView
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
    PublicCourseDetailView,
    AdminCourseListView,
    AdminBoardListCreateView,
    AdminBoardDetailView,
    AdminBoardCoursesView,
    AdminCourseCreateView,
    AdminCourseDeleteView,
    AdminCourseSubjectsView,
    AdminSubjectDeleteView,
)
from .views import MySubjectsView
from .views_recordings import (
    SubjectRecordingsView,
    CreateRecordingView,
    DeleteRecordingView,
    CreateVideoSlotView,
    SaveRecordingView,
    RecordingDetailView,
    CheckVideoStatusView,
    SignedUploadUrlView,
)
from .views_progress import (
    GetVideoProgressView,
    SaveVideoProgressView,
)
# Course coverage progress (teacher ticks chapters). NOTE: this is a different
# file from views_progress.py above, which handles video-watch progress.
from .progress_views import (
    CourseProgressView,
    ChapterCoverageView,
    MyCourseProgressView,
)
urlpatterns = [
    path("teacher/my-classes/",   TeacherMyClassesView.as_view()),
    path("teacher/all-students/", TeacherAllStudentsView.as_view()),
    path("subjects-by-course/",   SubjectsByCourseTitleView.as_view()),
    path("admin/",                AdminCourseListView.as_view()),
    # Admin Boards
    path("admin/boards/",                          AdminBoardListCreateView.as_view()),
    path("admin/boards/<uuid:board_id>/",          AdminBoardDetailView.as_view()),
    path("admin/boards/<uuid:board_id>/courses/",  AdminBoardCoursesView.as_view()),
    # Admin Course CRUD
    path("admin/courses/",                         AdminCourseCreateView.as_view()),
    path("admin/courses/<uuid:course_id>/",        AdminCourseDeleteView.as_view()),
    path("admin/courses/<uuid:course_id>/subjects/", AdminCourseSubjectsView.as_view()),
    # Admin Subject delete
    path("admin/subjects/<uuid:subject_id>/",      AdminSubjectDeleteView.as_view()),
    path("",                           CreateCourseView.as_view()),
    path("mine/",                      MyCoursesView.as_view()),
    path("my/",                        MyEnrolledCoursesView.as_view()),
    # Student-facing browsable catalog for the in-dashboard "Browse Courses" shop.
    # "catalog" is a static segment, so the <uuid:course_id> route below never
    # captures it.
    path("catalog/",                   CourseCatalogView.as_view()),
    path("<uuid:course_id>/public/",   PublicCourseDetailView.as_view()),
    path("<uuid:course_id>/",          UpdateCourseView.as_view()),
    path("<uuid:course_id>/delete/",   DeleteCourseView.as_view()),
    path("<uuid:course_id>/subjects/", CourseSubjectsView.as_view()),
    path("subject/<uuid:subject_id>/", SubjectDetailView.as_view()),
    # static before uuid
    path("subjects/mine/",             MySubjectsView.as_view()),
    path("subjects/<uuid:subject_id>/dashboard/",
         SubjectDashboardView.as_view()),
    path("subjects/<uuid:subject_id>/chapters/",  SubjectChaptersView.as_view()),
    # STUDENTS
    path("subjects/<uuid:subject_id>/students/", SubjectStudentsView.as_view()),
    # COURSE PROGRESS — teacher-ticked chapter coverage (one shared state)
    # "chapters" is a static segment, so the uuid converter above never
    # swallows it; the course-scoped routes match a bare uuid + suffix only.
    path("chapters/<uuid:chapter_id>/coverage/", ChapterCoverageView.as_view()),
    path("<uuid:course_id>/progress/",     CourseProgressView.as_view()),
    path("<uuid:course_id>/my-progress/",  MyCourseProgressView.as_view()),
    # RECORDINGS — subjects-scoped
    path("subjects/<uuid:subject_id>/recordings/",
         SubjectRecordingsView.as_view()),
    path("subjects/<uuid:subject_id>/recordings/create/",
         CreateRecordingView.as_view()),
    path("subjects/<uuid:subject_id>/recordings/save/",
         SaveRecordingView.as_view()),
    # RECORDINGS — static before uuid
    path("recordings/create-video/",      CreateVideoSlotView.as_view()),
    path("recordings/signed-upload-url/", SignedUploadUrlView.as_view()),
    # RECORDINGS — uuid-parameterised
    path("recordings/<uuid:recording_id>/delete/",
         DeleteRecordingView.as_view()),
    path("recordings/<uuid:recording_id>/progress/",
         GetVideoProgressView.as_view()),
    path("recordings/<uuid:recording_id>/progress/save/",
         SaveVideoProgressView.as_view()),
    path("recordings/<uuid:recording_id>/status/",
         CheckVideoStatusView.as_view()),
    path("recordings/<uuid:recording_id>/",
         RecordingDetailView.as_view()),
]
