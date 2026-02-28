from django.urls import path
from .views import MyEnrolledCoursesView, CourseSubjectsView

# update
from .views import (
    CreateCourseView,
    MyCoursesView,
    UpdateCourseView,
    DeleteCourseView,
    SubjectDetailView,
    SubjectDashboardView,
)

urlpatterns = [
    path("", CreateCourseView.as_view()),                  # POST /api/courses/
    # GET /api/courses/mine/
    path("mine/", MyCoursesView.as_view()),
    # GET /api/courses/my/
    path("my/", MyEnrolledCoursesView.as_view()),
    path("<uuid:course_id>/", UpdateCourseView.as_view()),
    path("<uuid:course_id>/delete/", DeleteCourseView.as_view()),
    path("<uuid:course_id>/subjects/", CourseSubjectsView.as_view()),
    path("subject/<uuid:subject_id>/", SubjectDetailView.as_view()),
    path("subjects/<uuid:subject_id>/dashboard/", SubjectDashboardView.as_view()
         ),
]
