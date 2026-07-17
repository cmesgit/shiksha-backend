# PLACEMENT: skills/urls.py  (replace the whole file)
# Wires GET /skill/teachers/<id>/availability/ for the Book-a-Tutor grid AND
# removes the dead /skill/conversations/ + /skill/teacher/inbox/ messaging routes
# (the messaging_views module is deleted; all chat now runs through the chat app).
"""skills/urls.py — mounted under /api/skill/ in project urls.py."""
from django.urls import path
from .views import (
    CategoryListView, ExpertListView, ExpertDetailView, StudentRegisterView,
    TeacherApplicationCreateView, InterviewSlotListView, ScheduleInterviewView,
    ReviewQueueView, SubmitEvaluationView, SessionRequestView, CreateOrderView,
    AdminExpertListView, AdminExpertDetailView, AdminExpertSuspendView,   # NEW
)
from .course_views import (
    PublicCourseListView, PublicCourseDetailView,
    TeacherCourseListCreateView, TeacherCourseDetailView, TeacherCourseSubmitView,
    TeacherSectionView, TeacherLectureView, TeacherLectureDetailView,
    CourseEnrollView, MySkillCoursesView, CourseLectureProgressView,
    AdminSkillCourseQueueView, AdminSkillCourseReviewView,
)
from .livekit_views import (
    JoinSessionView, MySessionsView,
    TeacherSessionsView, TeacherConfirmSessionView, TeacherCompleteSessionView,
    AdminSessionListView,        # NEW — admin platform-wide session monitor
    AdminUserSkillProfileView,   # NEW — per-user skill context for admin
)
from .review_views import (
    SubmitReviewView, ExpertReviewListView, MyReviewableSessionsView,
    StudentMyReviewsView, MyReviewUpdateView,   # NEW — learner's own reviews (My Reviews page)
)
from .payment_config_views import SkillPaymentConfigView
from .subscription_views import (
    ExpertSubscriptionView, ExpertSubscriptionSubmitPaymentView,
    AdminAdSubscriptionQueueView, AdminAdSubscriptionApproveView,
    AdminAdSubscriptionRejectView,
)
from .student_skill_views import (
    StudentSkillDashboardView,
    StudentSkillExpertsView,
    SkillSessionDetailView,      # NEW — powers the session detail page
)
from .teacher_views import (
    TeacherDashboardView, TeacherEarningsView, TeacherAvailabilityView,
    TeacherDeclineSessionView, TeacherProfileUpdateView,
    ExpertAvailabilityView,      # NEW — public read of a specific expert's slots
)
from .views_intro_video import (
    CreateIntroVideoSlotView, IntroVideoSignedUploadUrlView,
    SaveIntroVideoView, IntroVideoStatusView,
)
from .cancel_views import StudentCancelSessionView
from .reschedule_views import (
    TeacherRescheduleSessionView, StudentConfirmRescheduleView,
    StudentDeclineRescheduleView,
)

urlpatterns = [
    # ── Payment mode (free / manual_upi / razorpay) ──────────────────────────
    path("payment-config/", SkillPaymentConfigView.as_view()),

    # ── Public expert directory ──────────────────────────────────────────────
    path("categories/",                              CategoryListView.as_view()),
    path("teachers/",                                ExpertListView.as_view()),
    path("teachers/<uuid:expert_id>/",               ExpertDetailView.as_view()),
    # NEW: powers the Book-a-Tutor weekly grid (was unwired → grid showed empty,
    # so every slot looked "closed" and nothing could be booked).
    path("teachers/<uuid:expert_id>/availability/",  ExpertAvailabilityView.as_view()),
    path("teachers/<uuid:expert_id>/reviews/",       ExpertReviewListView.as_view()),

    # ── Public skill courses ─────────────────────────────────────────────────
    path("courses/",                                 PublicCourseListView.as_view()),
    path("courses/<uuid:course_id>/",                PublicCourseDetailView.as_view()),
    path("courses/<uuid:course_id>/enroll/",         CourseEnrollView.as_view()),

    # ── Student ──────────────────────────────────────────────────────────────
    path("students/",                                StudentRegisterView.as_view()),
    path("my-sessions/",                             MySessionsView.as_view()),
    path("my-courses/",                              MySkillCoursesView.as_view()),
    path("my-courses/<uuid:course_id>/progress/",    CourseLectureProgressView.as_view()),
    path("my-reviewable-sessions/",                  MyReviewableSessionsView.as_view()),
    # NEW: the learner's own submitted reviews (My Reviews nav page) + edit.
    path("my-reviews/",                              StudentMyReviewsView.as_view()),
    path("my-reviews/<uuid:review_id>/",             MyReviewUpdateView.as_view()),
    path("sessions/",                                SessionRequestView.as_view()),
    # NEW: detail route must come before the generic join/review routes
    # so Django matches <session_id>/ before trying <session_id>/join/ etc.
    path("sessions/<uuid:session_id>/",              SkillSessionDetailView.as_view()),
    path("sessions/<uuid:session_id>/join/",         JoinSessionView.as_view()),
    path("sessions/<uuid:session_id>/review/",       SubmitReviewView.as_view()),
    path("sessions/<uuid:session_id>/cancel/",       StudentCancelSessionView.as_view()),
    path("sessions/<uuid:session_id>/confirm-reschedule/", StudentConfirmRescheduleView.as_view()),
    path("sessions/<uuid:session_id>/decline-reschedule/", StudentDeclineRescheduleView.as_view()),
    path("payments/create-order/",                   CreateOrderView.as_view()),

    # ── Teacher application + screening ──────────────────────────────────────
    path("teacher-applications/",                                TeacherApplicationCreateView.as_view()),
    path("teacher-applications/<uuid:application_id>/schedule/", ScheduleInterviewView.as_view()),
    path("interview-slots/",                                     InterviewSlotListView.as_view()),

    # ── Teacher — skill courses ───────────────────────────────────────────────
    path("teacher/courses/",                              TeacherCourseListCreateView.as_view()),
    path("teacher/courses/<uuid:course_id>/",             TeacherCourseDetailView.as_view()),
    path("teacher/courses/<uuid:course_id>/submit/",      TeacherCourseSubmitView.as_view()),
    path("teacher/courses/<uuid:course_id>/sections/",    TeacherSectionView.as_view()),
    path("teacher/sections/<uuid:section_id>/lectures/",  TeacherLectureView.as_view()),
    path("teacher/lectures/<uuid:lecture_id>/",           TeacherLectureDetailView.as_view()),

    # ── Teacher — sessions ────────────────────────────────────────────────────
    path("teacher/sessions/",                               TeacherSessionsView.as_view()),
    path("teacher/sessions/<uuid:session_id>/confirm/",     TeacherConfirmSessionView.as_view()),
    path("teacher/sessions/<uuid:session_id>/complete/",    TeacherCompleteSessionView.as_view()),

    # ── Student skill dashboard ───────────────────────────────────────────────
    path("student/dashboard/",  StudentSkillDashboardView.as_view()),
    path("student/experts/",    StudentSkillExpertsView.as_view()),

    # ── Teacher — extra endpoints ─────────────────────────────────────────────
    path("teacher/dashboard/",                               TeacherDashboardView.as_view()),
    path("teacher/earnings/",                                TeacherEarningsView.as_view()),
    path("teacher/availability/",                            TeacherAvailabilityView.as_view()),
    path("teacher/sessions/<uuid:session_id>/decline/",      TeacherDeclineSessionView.as_view()),
    path("teacher/sessions/<uuid:session_id>/reschedule/",   TeacherRescheduleSessionView.as_view()),
    path("teacher/profile/",                                 TeacherProfileUpdateView.as_view()),
    path("teacher/intro-video/create/",                      CreateIntroVideoSlotView.as_view()),
    path("teacher/intro-video/upload-url/",                  IntroVideoSignedUploadUrlView.as_view()),
    path("teacher/intro-video/save/",                        SaveIntroVideoView.as_view()),
    path("teacher/intro-video/status/",                      IntroVideoStatusView.as_view()),

    # ── Guest-expert advertising subscription ─────────────────────────────────
    path("subscription/",                 ExpertSubscriptionView.as_view()),
    path("subscription/submit-payment/",  ExpertSubscriptionSubmitPaymentView.as_view()),

    # ── Admin ─────────────────────────────────────────────────────────────────
    path("admin/interview-queue/",                               ReviewQueueView.as_view()),
    path("admin/sessions/",                                      AdminSessionListView.as_view()),
    path("admin/experts/",                                       AdminExpertListView.as_view()),
    path("admin/experts/<uuid:expert_id>/",                      AdminExpertDetailView.as_view()),
    path("admin/experts/<uuid:expert_id>/suspend/",              AdminExpertSuspendView.as_view()),
    path("admin/users/<uuid:user_id>/skill-profile/",            AdminUserSkillProfileView.as_view()),
    path("admin/interviews/<uuid:application_id>/evaluation/",   SubmitEvaluationView.as_view()),
    path("admin/courses/",                                       AdminSkillCourseQueueView.as_view()),
    path("admin/courses/<uuid:course_id>/review/",               AdminSkillCourseReviewView.as_view()),
    path("admin/ad-subscriptions/",                              AdminAdSubscriptionQueueView.as_view()),
    path("admin/ad-subscriptions/<uuid:sub_id>/approve/",        AdminAdSubscriptionApproveView.as_view()),
    path("admin/ad-subscriptions/<uuid:sub_id>/reject/",         AdminAdSubscriptionRejectView.as_view()),
]
