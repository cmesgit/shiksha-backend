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
# Per-batch coverage progress (teacher ticks chapters for one batch + notes;
# students see their own batch's coverage).
from .batch_progress_views import (
    BatchProgressView,
    BatchChapterCoverageView,
    MyBatchProgressView,
)
# Admin academy management: subject-teacher assignment + batch CRUD.
from .admin_academy_views import (
    AdminTeacherListView,
    AdminSubjectTeachersView,
    AdminSubjectTeacherDetailView,
    AdminCourseBatchesView,
    AdminBatchDetailView,
)
# Teacher: the batches they can record progress for.
from .teacher_batch_views import TeacherMyBatchesView
urlpatterns = [
    path("teacher/my-classes/",   TeacherMyClassesView.as_view()),
    path("teacher/my-batches/",   TeacherMyBatchesView.as_view()),
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
    # Admin — teacher assignment (subject ↔ teacher)
    path("admin/teachers/",                                  AdminTeacherListView.as_view()),
    path("admin/subjects/<uuid:subject_id>/teachers/",       AdminSubjectTeachersView.as_view()),
    path("admin/subject-teachers/<int:assignment_id>/",      AdminSubjectTeacherDetailView.as_view()),
    # Admin — batches
    path("admin/courses/<uuid:course_id>/batches/",          AdminCourseBatchesView.as_view()),
    path("admin/batches/<uuid:batch_id>/",                   AdminBatchDetailView.as_view()),
    path("",                           CreateCourseView.as_view()),
    path("mine/",                      MyCoursesView.as_view()),
    path("my/",                        MyEnrolledCoursesView.as_view()),
    # Student-facing browsable catalog for the in-dashboard "Browse Courses" shop.
    # Supports ?q=, ?board=, and ?stream= so filters scale as more boards and
    # streams are added.
    # "catalog" is a static segment, so the <uuid:course_id> route below never
    # captures it.
    path("catalog/",                   CourseCatalogView.as_view()),
    # PER-BATCH PROGRESS — teacher-ticked chapter coverage, per batch.
    # "batches" and "my-batch-progress" are static segments, so the bare
    # <uuid:course_id> routes further down never capture them.
    path("batches/<uuid:batch_id>/progress/",
         BatchProgressView.as_view()),
    path("batches/<uuid:batch_id>/chapters/<uuid:chapter_id>/coverage/",
         BatchChapterCoverageView.as_view()),
    path("my-batch-progress/",
         MyBatchProgressView.as_view()),
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
