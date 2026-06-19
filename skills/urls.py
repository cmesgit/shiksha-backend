"""
skills/urls.py

Mount in the project urls.py with:

    path("api/skill/", include("skills.urls")),

so these line up with the frontend's `/skill/...` axios calls
(apiClient baseURL already includes `/api`).
"""
from django.urls import path

from .views import (
    CategoryListView,
    ExpertListView,
    ExpertDetailView,
    StudentRegisterView,
    TeacherApplicationCreateView,
    InterviewSlotListView,
    ScheduleInterviewView,
    ReviewQueueView,
    SubmitEvaluationView,
    SessionRequestView,
    CreateOrderView,
)

urlpatterns = [
    # Directory (public)
    path("categories/", CategoryListView.as_view()),
    path("teachers/", ExpertListView.as_view()),
    path("teachers/<uuid:expert_id>/", ExpertDetailView.as_view()),

    # Registration
    path("students/", StudentRegisterView.as_view()),
    path("teacher-applications/", TeacherApplicationCreateView.as_view()),
    path("teacher-applications/<uuid:application_id>/schedule/", ScheduleInterviewView.as_view()),

    # Interview screening
    path("interview-slots/", InterviewSlotListView.as_view()),
    path("admin/interview-queue/", ReviewQueueView.as_view()),
    path("admin/interviews/<uuid:application_id>/evaluation/", SubmitEvaluationView.as_view()),

    # Sessions + payments
    path("sessions/", SessionRequestView.as_view()),
    path("payments/create-order/", CreateOrderView.as_view()),
]
