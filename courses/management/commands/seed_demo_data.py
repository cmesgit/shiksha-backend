"""
courses/management/commands/seed_demo_data.py

Demo/test rows for manually clicking through the platform end-to-end:
    python manage.py seed_demo_data

Idempotent — keyed on email/code/title, safe to run repeatedly (re-running
updates passwords back to the demo password and leaves everything else as
the get_or_create default already created).

Creates ONE fully-populated ACADEMIC course ("Class 11 Science") with two
subjects (Physics, Chemistry), one batch, one teacher assigned to both
subjects (SubjectTeacher + TeachingAssignment, since different parts of the
app still read one or the other — see accounts/settings_views.py-adjacent
courses.TeacherMyClassesView vs the batch-scoped delivery apps), two
students enrolled (one multi-profile parent account + one single-profile
account), plus:
  - 1 assignment per subject (with a real file, one has a real submission)
  - 1 approved+published quiz per subject (with questions/choices, one has
    a real submitted attempt so a score shows up)
  - 1 study material per subject
  - 3 live sessions across the course: scheduled / live-right-now / completed
  - 1 finished, published recording of the completed live session
  - 3 more teacher accounts covering the other track states (approved
    Expert/Skill-Dev, pending Academy application, rejected Academy
    application) so Teacher identity / admin-approval screens have
    something to show.

Order matters for activity/signals.py's post_save hooks (Assignment,
published Quiz, LiveSession all notify every ACTIVE enrollment in that
course): enrollments are created before assignments/quizzes/live sessions
so the demo students' notification bell isn't empty.
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import LearnerProfile, Role, TeacherProfile, UserRole
from assignments.models import Assignment, AssignmentFile, AssignmentSubmission
from courses.models import (
    Batch, Chapter, Course, Subject, SubjectTeacher, TeachingAssignment,
)
from courses.models_recordings import SessionRecording
from enrollments.models import Enrollment, Subscription
from livestream.models import LiveSession
from materials.models import MaterialFile, StudyMaterial
from quizzes.models import Choice, Question, Quiz, QuizAttempt, StudentAnswer

User = get_user_model()

DEMO_PASSWORD = "ShikshaDemo@2026"


class Command(BaseCommand):
    help = "Seed demo accounts + a fully working course/batch/quiz/assignment/livestream for manual testing."

    def handle(self, *args, **options):
        with transaction.atomic():
            course, subjects, batch = self._make_course()
            self.stdout.write("course/subjects/batch OK"); self.stdout.flush()
            faculty = self._make_faculty(subjects, batch)
            self.stdout.write("faculty OK"); self.stdout.flush()
            self._make_other_teachers()
            self.stdout.write("other teachers OK"); self.stdout.flush()
            parent, child, student = self._make_students()
            self.stdout.write("students OK"); self.stdout.flush()

            # Enrollments before content, so the activity feed isn't empty.
            for profile in (child, student):
                self._enroll(profile, course, batch)
            self.stdout.write("enrollments OK"); self.stdout.flush()

            chapters = self._make_chapters(subjects)
            self.stdout.write("chapters OK"); self.stdout.flush()
            self._make_assignments(chapters, faculty, child, student)
            self.stdout.write("assignments OK"); self.stdout.flush()
            self._make_quizzes(subjects, faculty, batch, child)
            self.stdout.write("quizzes OK"); self.stdout.flush()
            self._make_materials(chapters, faculty)
            self.stdout.write("materials OK"); self.stdout.flush()
            self._make_live_sessions(course, subjects, batch, faculty)
            self.stdout.write("live sessions OK"); self.stdout.flush()

        self.stdout.write(self.style.SUCCESS("Demo data seeded."))
        self.stdout.write(f"Shared password for every account below: {DEMO_PASSWORD}")
        self.stdout.write("Accounts: demo.parent, demo.student, demo.faculty, "
                           "demo.expert, demo.faculty.pending, demo.faculty.rejected "
                           "@shiksha.test")

    # ── helpers ──────────────────────────────────────────────────────────

    def _user(self, email, username, first, last):
        user, _ = User.objects.get_or_create(
            email=email,
            defaults={"username": username, "first_name": first, "last_name": last},
        )
        user.set_password(DEMO_PASSWORD)
        user.is_verified = True
        user.verified_at = user.verified_at or timezone.now()
        user.is_active = True
        user.save()
        return user

    def _give_role(self, user, role_name):
        role, _ = Role.objects.get_or_create(name=role_name)
        UserRole.objects.get_or_create(
            user=user, role=role, defaults={"is_active": True, "is_primary": True})

    def _placeholder_file(self, name, text):
        return ContentFile(text.encode(), name=name)

    # ── course / subjects / batch ───────────────────────────────────────

    def _make_course(self):
        course, _ = Course.objects.get_or_create(
            title="Class 11 Science — Demo Course",
            stream=None, board=None,
            defaults={
                "description": "Seeded demo course for manual QA — Physics & Chemistry, one live batch.",
                "kind": Course.KIND_ACADEMIC,
                "status": Course.STATUS_PUBLISHED,
                "class_level": 11,
                "price": 0,
                "subscription_duration_days": 365,
            },
        )
        subjects = {}
        for name, textbook in (("Physics", "Concepts of Physics"), ("Chemistry", "NCERT Chemistry")):
            subjects[name], _ = Subject.objects.get_or_create(
                course=course, name=name, defaults={"textbook": textbook})

        batch, _ = Batch.objects.get_or_create(
            course=course, code="DEMO-A1",
            defaults={
                "name": "Demo Batch A1",
                "year": timezone.now().year,
                "start_date": timezone.now().date() - timedelta(days=30),
                "is_active": True,
            },
        )
        return course, subjects, batch

    def _make_faculty(self, subjects, batch):
        faculty = self._user("demo.faculty@shiksha.test", "demo.faculty", "Kavita", "Iyer")
        self._give_role(faculty, "TEACHER")
        LearnerProfile.objects.get_or_create(
            account=faculty, relationship=LearnerProfile.RELATIONSHIP_SELF,
            defaults={"display_name": "Kavita Iyer", "is_default": True})

        tp, _ = TeacherProfile.objects.get_or_create(
            user=faculty,
            defaults={
                "teacher_type": TeacherProfile.TYPE_FACULTY,
                "academy_status": TeacherProfile.TRACK_APPROVED,
                "is_approved": True,
                "bio": "PGT Physics & Chemistry, 10 years — seeded demo teacher.",
                "subject": TeacherProfile.SUBJECT_CHOICES[1][0],  # physics
            },
        )
        if tp.academy_status != TeacherProfile.TRACK_APPROVED:
            tp.teacher_type = TeacherProfile.TYPE_FACULTY
            tp.academy_status = TeacherProfile.TRACK_APPROVED
            tp.is_approved = True
            tp.save()

        # Both roster mechanisms: SubjectTeacher (TeacherMyClassesView reads
        # this today) and TeachingAssignment (batch-scoped delivery apps).
        for subject in subjects.values():
            SubjectTeacher.objects.get_or_create(subject=subject, teacher=faculty)
            TeachingAssignment.objects.get_or_create(
                batch=batch, subject=subject, teacher=faculty,
                defaults={"role": TeachingAssignment.ROLE_PRIMARY, "is_active": True},
            )
        return faculty

    def _make_other_teachers(self):
        expert = self._user("demo.expert@shiksha.test", "demo.expert", "Arjun", "Mehta")
        self._give_role(expert, "TEACHER")
        LearnerProfile.objects.get_or_create(
            account=expert, relationship=LearnerProfile.RELATIONSHIP_SELF,
            defaults={"display_name": "Arjun Mehta", "is_default": True})
        TeacherProfile.objects.get_or_create(
            user=expert,
            defaults={
                "teacher_type": TeacherProfile.TYPE_GUEST,
                "skill_status": TeacherProfile.TRACK_APPROVED,
                "is_approved": True,
                "bio": "Public-speaking & interview coach — seeded demo expert.",
            },
        )

        pending = self._user("demo.faculty.pending@shiksha.test", "demo.faculty.pending", "Sonal", "Deshmukh")
        self._give_role(pending, "TEACHER")
        LearnerProfile.objects.get_or_create(
            account=pending, relationship=LearnerProfile.RELATIONSHIP_SELF,
            defaults={"display_name": "Sonal Deshmukh", "is_default": True})
        TeacherProfile.objects.get_or_create(
            user=pending,
            defaults={
                "teacher_type": TeacherProfile.TYPE_FACULTY,
                "academy_status": TeacherProfile.TRACK_PENDING,
                "bio": "Applied to teach Mathematics — seeded demo (pending review).",
            },
        )

        rejected = self._user("demo.faculty.rejected@shiksha.test", "demo.faculty.rejected", "Vikram", "Rao")
        self._give_role(rejected, "TEACHER")
        LearnerProfile.objects.get_or_create(
            account=rejected, relationship=LearnerProfile.RELATIONSHIP_SELF,
            defaults={"display_name": "Vikram Rao", "is_default": True})
        TeacherProfile.objects.get_or_create(
            user=rejected,
            defaults={
                "teacher_type": TeacherProfile.TYPE_FACULTY,
                "academy_status": TeacherProfile.TRACK_REJECTED,
                "academy_rejection_reason": "ID proof image was unreadable — please re-upload and resubmit.",
                "bio": "Seeded demo (rejected application).",
            },
        )

    # ── students ─────────────────────────────────────────────────────────

    def _make_students(self):
        parent = self._user("demo.parent@shiksha.test", "demo.parent", "Meera", "Nair")
        self._give_role(parent, "STUDENT")
        child, _ = LearnerProfile.objects.get_or_create(
            account=parent, relationship=LearnerProfile.RELATIONSHIP_SELF,
            defaults={"display_name": "Meera Nair", "is_default": True,
                      "first_name": "Meera", "last_name": "Nair",
                      "currently_studying": "no"},
        )
        dependent, _ = LearnerProfile.objects.get_or_create(
            account=parent, display_name="Aryan Nair",
            defaults={"relationship": LearnerProfile.RELATIONSHIP_DEPENDENT,
                      "first_name": "Aryan", "last_name": "Nair",
                      "currently_studying": "yes", "current_class": "11",
                      "stream": "science", "board": "cbse"},
        )

        student = self._user("demo.student@shiksha.test", "demo.student", "Rahul", "Verma")
        self._give_role(student, "STUDENT")
        student_profile, _ = LearnerProfile.objects.get_or_create(
            account=student, relationship=LearnerProfile.RELATIONSHIP_SELF,
            defaults={"display_name": "Rahul Verma", "is_default": True,
                      "first_name": "Rahul", "last_name": "Verma",
                      "currently_studying": "yes", "current_class": "11",
                      "stream": "science", "board": "cbse"},
        )
        return parent, dependent, student_profile

    def _enroll(self, learner_profile, course, batch):
        enrollment, _ = Enrollment.objects.get_or_create(
            learner_profile=learner_profile, course=course,
            defaults={"user": learner_profile.account, "status": Enrollment.STATUS_ACTIVE,
                      "batch": batch, "batch_code": batch.code},
        )
        if enrollment.batch_id != batch.id:
            enrollment.batch = batch
            enrollment.batch_code = batch.code
            enrollment.save()

        active_sub = Subscription.objects.filter(
            learner_profile=learner_profile, course=course,
            status=Subscription.STATUS_ACTIVE, expires_at__gt=timezone.now(),
        ).first()
        if not active_sub:
            Subscription.objects.create(
                user=learner_profile.account, learner_profile=learner_profile, course=course,
                starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=365),
                status=Subscription.STATUS_ACTIVE,
            )
        return enrollment

    # ── chapters ─────────────────────────────────────────────────────────

    def _make_chapters(self, subjects):
        chapters = {}
        titles = {
            "Physics": ["Laws of Motion", "Work, Energy and Power"],
            "Chemistry": ["Some Basic Concepts of Chemistry", "Structure of Atom"],
        }
        for name, subject in subjects.items():
            chapters[name] = []
            for i, title in enumerate(titles[name], start=1):
                ch, _ = Chapter.objects.get_or_create(
                    subject=subject, title=title, defaults={"order": i})
                chapters[name].append(ch)
        return chapters

    # ── assignments ──────────────────────────────────────────────────────

    def _make_assignments(self, chapters, faculty, child, student):
        due = timezone.now() + timedelta(days=7)
        for name, chs in chapters.items():
            assignment, created = Assignment.objects.get_or_create(
                chapter=chs[0], title=f"{name} — Chapter 1 problem set",
                defaults={"description": f"Solve the end-of-chapter problems for {chs[0].title}.",
                          "due_date": due},
            )
            if created:
                AssignmentFile.objects.get_or_create(
                    assignment=assignment,
                    defaults={"file": self._placeholder_file(
                        f"{name.lower()}-problem-set.txt",
                        f"{name} Chapter 1 problem set — seeded demo assignment brief.")},
                )

        # One real submission from the single-profile student, on Physics.
        physics_assignment = Assignment.objects.get(
            chapter=chapters["Physics"][0], title="Physics — Chapter 1 problem set")
        AssignmentSubmission.objects.get_or_create(
            assignment=physics_assignment, learner_profile=student,
            defaults={"student": student.account,
                      "submitted_file": self._placeholder_file(
                          "rahul-physics-submission.txt",
                          "Rahul Verma's seeded demo submission.")},
        )

    # ── quizzes ──────────────────────────────────────────────────────────

    def _make_quizzes(self, subjects, faculty, batch, child):
        bank = {
            "Physics": [
                ("Newton's first law is also known as the law of:", ["Inertia", "Gravitation", "Conservation of energy", "Friction"], 0),
                ("SI unit of force is:", ["Newton", "Joule", "Watt", "Pascal"], 0),
                ("Which quantity is a vector?", ["Velocity", "Speed", "Mass", "Time"], 0),
            ],
            "Chemistry": [
                ("Atomic number equals the number of:", ["Protons", "Neutrons", "Electrons + neutrons", "Isotopes"], 0),
                ("Which is a noble gas?", ["Neon", "Chlorine", "Oxygen", "Nitrogen"], 0),
                ("Avogadro's number is approximately:", ["6.022×10²³", "3.14×10²³", "9.8×10²³", "1×10²³"], 0),
            ],
        }
        for name, subject in subjects.items():
            quiz, created = Quiz.objects.get_or_create(
                subject=subject, title=f"{name} — Chapter 1 quick check",
                defaults={"created_by": faculty, "batch": batch,
                          "quiz_type": Quiz.TYPE_MOCK, "time_limit_minutes": 10,
                          "is_published": True, "review_status": Quiz.REVIEW_APPROVED,
                          "reviewed_by": faculty, "reviewed_at": timezone.now(),
                          "submitted_for_review_at": timezone.now()},
            )
            if created:
                total = 0
                questions = []
                for text, options, correct_idx in bank[name]:
                    q = Question.objects.create(quiz=quiz, text=text, marks=1)
                    for i, opt in enumerate(options):
                        Choice.objects.create(question=q, text=opt, is_correct=(i == correct_idx))
                    questions.append(q)
                    total += q.marks
                quiz.total_marks = total
                quiz.save(update_fields=["total_marks"])

                if name == "Physics":
                    # One real submitted attempt (mostly correct) so a score shows up.
                    attempt = QuizAttempt.objects.create(
                        quiz=quiz, student=child.account, learner_profile=child,
                        status=QuizAttempt.STATUS_SUBMITTED, submitted_at=timezone.now(),
                    )
                    correct_count = 0
                    for i, q in enumerate(questions):
                        choice = q.choices.filter(is_correct=(i != len(questions) - 1)).first() \
                            or q.choices.first()
                        StudentAnswer.objects.create(
                            attempt=attempt, question=q, selected_choice=choice,
                            is_correct=choice.is_correct,
                        )
                        correct_count += int(choice.is_correct)
                    attempt.score = round(100 * correct_count / len(questions), 1)
                    attempt.save(update_fields=["score"])
                # Chemistry quiz is left unattempted — the student picks it up live.

    # ── materials ────────────────────────────────────────────────────────

    def _make_materials(self, chapters, faculty):
        for name, chs in chapters.items():
            material, created = StudyMaterial.objects.get_or_create(
                chapter=chs[0], title=f"{name} — Chapter 1 notes",
                defaults={"description": "Seeded demo revision notes.", "uploaded_by": faculty},
            )
            if created:
                MaterialFile.objects.get_or_create(
                    material=material,
                    defaults={"uploaded_by": faculty, "file": self._placeholder_file(
                        f"{name.lower()}-ch1-notes.txt", f"{name} Chapter 1 — seeded demo notes.")},
                )

    # ── live sessions + recording ───────────────────────────────────────

    def _make_live_sessions(self, course, subjects, batch, faculty):
        now = timezone.now()
        physics = subjects["Physics"]
        chemistry = subjects["Chemistry"]

        LiveSession.objects.get_or_create(
            room_name="demo-physics-scheduled",
            defaults={"course": course, "subject": physics, "batch": batch,
                      "title": "Physics — upcoming live class",
                      "start_time": now + timedelta(days=2),
                      "end_time": now + timedelta(days=2, hours=1),
                      "status": LiveSession.STATUS_SCHEDULED, "created_by": faculty},
        )

        LiveSession.objects.get_or_create(
            room_name="demo-chemistry-live",
            defaults={"course": course, "subject": chemistry, "batch": batch,
                      "title": "Chemistry — live right now",
                      "start_time": now - timedelta(minutes=10),
                      "end_time": now + timedelta(minutes=50),
                      "status": LiveSession.STATUS_LIVE, "created_by": faculty},
        )

        completed, _ = LiveSession.objects.get_or_create(
            room_name="demo-physics-completed",
            defaults={"course": course, "subject": physics, "batch": batch,
                      "title": "Physics — last week's recorded class",
                      "start_time": now - timedelta(days=5),
                      "end_time": now - timedelta(days=5) + timedelta(hours=1),
                      "status": LiveSession.STATUS_COMPLETED, "created_by": faculty,
                      "actual_started_at": now - timedelta(days=5),
                      "actual_ended_at": now - timedelta(days=5) + timedelta(hours=1),
                      "peak_viewers": 2},
        )

        SessionRecording.objects.get_or_create(
            live_session=completed,
            defaults={"subject": physics, "chapter": None, "batch": batch,
                      "title": completed.title,
                      "description": "Seeded demo recording.",
                      "session_date": completed.start_time.date(),
                      "duration_seconds": 3600,
                      "bunny_video_id": f"demo-{uuid.uuid4().hex[:12]}",
                      "status": 4,  # Finished
                      "uploaded_by": faculty, "is_published": True},
        )
