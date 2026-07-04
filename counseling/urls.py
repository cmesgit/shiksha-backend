# PLACEMENT: backend/backend/counseling/urls.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/urls.py
# Mounted at /api/counseling/ (see config/urls.py wiring in the README).

from django.urls import path

from .views import (
    ListSpecializationsView, CounselorDirectoryView, CounselorDetailView,
    CounselorSlotsView,
    IntakeView, MatchView,
    CreateAppointmentView, MyAppointmentsView, CancelAppointmentView,
    AssessmentView, SubmitAssessmentView, MyReportsView,
    ApplyCounselorView, CounselorMeView,
    AvailabilityView, AvailabilityDeleteView,
    CounselorAppointmentsView, SetMeetingLinkView, CompleteAppointmentView,
    CounselorStudentView, SessionNotesView, SessionReportView,
    AdminApplicationsView, AdminApplicationActionView, AdminAppointmentsView,
)

urlpatterns = [
    # Public directory
    path("specializations/", ListSpecializationsView.as_view()),
    path("counselors/", CounselorDirectoryView.as_view()),
    path("counselors/<int:counselor_id>/", CounselorDetailView.as_view()),
    path("counselors/<int:counselor_id>/slots/", CounselorSlotsView.as_view()),

    # Student
    path("intake/", IntakeView.as_view()),
    path("match/", MatchView.as_view()),
    path("appointments/create/", CreateAppointmentView.as_view()),
    path("appointments/", MyAppointmentsView.as_view()),
    path("appointments/<int:appointment_id>/cancel/", CancelAppointmentView.as_view()),
    path("appointments/<int:appointment_id>/assessment/", AssessmentView.as_view()),
    path("appointments/<int:appointment_id>/assessment/submit/", SubmitAssessmentView.as_view()),
    path("reports/", MyReportsView.as_view()),

    # Counselor
    path("counselor/apply/", ApplyCounselorView.as_view()),
    path("counselor/me/", CounselorMeView.as_view()),
    path("counselor/availability/", AvailabilityView.as_view()),
    path("counselor/availability/<int:slot_id>/", AvailabilityDeleteView.as_view()),
    path("counselor/appointments/", CounselorAppointmentsView.as_view()),
    path("counselor/appointments/<int:appointment_id>/meeting-link/", SetMeetingLinkView.as_view()),
    path("counselor/appointments/<int:appointment_id>/complete/", CompleteAppointmentView.as_view()),
    path("counselor/appointments/<int:appointment_id>/student/", CounselorStudentView.as_view()),
    path("counselor/appointments/<int:appointment_id>/notes/", SessionNotesView.as_view()),
    path("counselor/appointments/<int:appointment_id>/report/", SessionReportView.as_view()),

    # Admin
    path("admin/applications/", AdminApplicationsView.as_view()),
    path("admin/applications/<int:profile_id>/action/", AdminApplicationActionView.as_view()),
    path("admin/appointments/", AdminAppointmentsView.as_view()),
]
