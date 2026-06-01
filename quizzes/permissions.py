from rest_framework.permissions import BasePermission
from django.utils import timezone
from enrollments.models import Enrollment, Subscription
from .models import SubjectTeacher, Quiz, QuizAttempt


# -------------------------------------------------------
# ✅ 1️⃣ Teacher Role Check
# -------------------------------------------------------

class IsTeacher(BasePermission):
    """
    Allows access only to users with active teacher role.
    """

    def has_permission(self, request, view):
        return request.user.is_authenticated and \
            request.user.has_role("teacher")


# -------------------------------------------------------
# ✅ 2️⃣ Is Assigned To Subject
# -------------------------------------------------------

class IsAssignedSubjectTeacher(BasePermission):
    """
    Allows access only if user is assigned to the subject.
    Used when subject_id is provided in request.data.
    """

    def has_permission(self, request, view):
        subject_id = request.data.get("subject_id")

        if not subject_id:
            return False

        return SubjectTeacher.objects.filter(
            subject_id=subject_id,
            teacher=request.user
        ).exists()


# -------------------------------------------------------
# ✅ 3️⃣ Is Quiz Owner (Teacher who created it)
# -------------------------------------------------------

class IsQuizCreator(BasePermission):
    """
    Only quiz creator can edit/delete.
    """

    def has_object_permission(self, request, view, obj):
        return obj.created_by == request.user


# -------------------------------------------------------
# ✅ 4️⃣ Student Must Be Enrolled In Course
# -------------------------------------------------------

class IsEnrolledStudent(BasePermission):
    """
    Allows access only if the student has an ACTIVE, non-expired subscription
    for the course related to the quiz. Renamed-in-spirit but kept under the
    original class name so existing imports keep working.
    """

    message = "Your subscription for this course has expired."

    def has_object_permission(self, request, view, obj: Quiz):
        return Subscription.objects.filter(
            user=request.user,
            course=obj.subject.course,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=timezone.now(),
        ).exists()


# -------------------------------------------------------
# ✅ 5️⃣ Quiz Must Be Published & Not Expired
# -------------------------------------------------------

class IsPublishedAndActive(BasePermission):
    """
    Ensures quiz is published and not expired.
    """

    def has_object_permission(self, request, view, obj: Quiz):
        return obj.is_published and obj.due_date > timezone.now()


# -------------------------------------------------------
# ✅ 6️⃣ Prevent Multiple Attempts
# -------------------------------------------------------

class HasNotSubmittedQuiz(BasePermission):
    """
    Prevents student from submitting quiz multiple times.
    """

    def has_object_permission(self, request, view, obj: Quiz):
        return not QuizAttempt.objects.filter(
            quiz=obj,
            student=request.user,
            status=QuizAttempt.STATUS_SUBMITTED
        ).exists()
