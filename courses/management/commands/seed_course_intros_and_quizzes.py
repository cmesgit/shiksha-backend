"""
courses/management/commands/seed_course_intros_and_quizzes.py

Populates the homepage hero's "just published" feed
(content/views.py HomeHeroHighlightsView) with real content: one course-level
introduction PDF per target course, and one practice quiz per subject in
those courses. Both are bounded by NEW_WINDOW_DAYS=30 in that view, so
creating them now is what makes them "new".

SCOPE: 8 courses (Class 10 + Class 11, CBSE + MBSE), 79 subjects total. Not
every course in the catalog — see the content-and-data handoff this shipped
with for why (only these 19 of 35 public courses have any subjects staffed
at all, and this run further narrows to Class 10/11 per an explicit user
scope decision).

    python manage.py seed_course_intros_and_quizzes --dry-run
    python manage.py seed_course_intros_and_quizzes
    python manage.py seed_course_intros_and_quizzes --course-id <uuid>  # one course only

IDEMPOTENCY / NAMESPACE
-----------------------
This reuses the courses/management/commands/seed_academy_launch.py pattern
(derived UUID primary keys via uuid5, so re-running finds its own rows
instead of duplicating) but with ITS OWN namespace and kind strings —
NOT seed_academy_launch's SEED_NS. That command already created one
StudyMaterial and one (unassigned) Quiz per subject keyed on
uuid5(SEED_NS, f"material:{subject.id}") / f"quiz:{subject.id}"; reusing
that scheme here would silently return THEIR rows via get_or_create instead
of creating new ones.

NOTIFICATIONS — DELIBERATE, NOT AN OVERSIGHT
---------------------------------------------
Quiz.is_assigned is created True here (not staged False→True later). That
IS what fires activity/signals.py's quiz_published post_save signal, which
notifies every active enrollment in the course (course-wide, since `batches`
stays empty — see quizzes/visibility.py on why empty must mean "everyone").
Checked against prod before running: the 8 target courses carry a combined
~11 real (non-seed) active enrollments, so the realistic total is on the
order of 100-ish in-app notifications ("new quiz available"), not thousands.
No email/SMS/push — those verbs are policy-OFF platform-wide regardless.
StudyMaterial has no such signal (materials/views.py is the only notifier,
and this is a command), so the 8 intro PDFs notify nobody.

UPLOADED_BY / CREATED_BY
-------------------------
Both material and quiz are attributed to bualtenghruaizela@gmail.com — a
real (non-seed-domain), active teaching account on prod, already the
earliest real StudyMaterial uploader (established precedent for "the
account real content gets filed under"). This choice is invisible to
visitors either way: HomeHeroHighlightsView's new_material/new_quiz feed
items carry no author name, only subject (+ chapter) and course.
"""

import json
import os
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from courses.models import Course, Subject
from materials.models import MaterialFile, StudyMaterial
from quizzes.models import Choice, Question, Quiz

SEED_DATA_DIR = os.path.join(os.path.dirname(__file__), "_course_intro_seed_data")
PDF_DIR = os.path.join(SEED_DATA_DIR, "pdfs")
QUIZ_DIR = os.path.join(SEED_DATA_DIR, "quizzes")

# Distinct from seed_academy_launch.SEED_NS on purpose — see module docstring.
SEED_NS = uuid.UUID("3c7b9f10-8e2d-4a61-9b55-2d6f0a7e3c14")

UPLOADER_EMAIL = "bualtenghruaizela@gmail.com"

# course_id -> anchor subject id (where the course-overview PDF is filed).
# Picked as the flagship/compulsory subject for that stream — see the
# content handoff for the per-course reasoning.
ANCHOR_SUBJECT = {
    "41ec43ac-bac7-4a68-b5d6-eda2acd85585": "d95066dc-61e1-4a96-8d6a-d6a142392770",  # Class 10 CBSE -> Science
    "b6b1e3b0-6b34-4b1a-8b1a-000000000003": "1faf661a-612a-4a20-a6bf-e1f919461b2b",  # Class 11 Arts CBSE -> History
    "b6b1e3b0-6b34-4b1a-8b1a-000000000002": "1e6265e9-4153-4e71-a248-1493c923250f",  # Class 11 Commerce CBSE -> Accountancy
    "b6b1e3b0-6b34-4b1a-8b1a-000000000001": "a0e3ee91-0a55-4ba3-814a-eade61e0c031",  # Class 11 Science CBSE -> Physics
    "cfe07ab8-1508-4c14-8181-8ba21d4cb331": "8bc664b1-c349-439b-95dd-74459d679bbc",  # Class 10 MBSE -> Science
    "24056c0b-1d46-411a-912d-5fecd2b8d90f": "89ec91eb-6803-4a03-acf5-81e2b3f71933",  # Class 11 Arts MBSE -> History
    "51724c07-b13a-4413-85d8-d7cf2561fabb": "a1e10719-a7fb-4b20-9e3e-362afc3bb587",  # Class 11 Commerce MBSE -> Accountancy
    "eb7700aa-a95b-4eeb-a4e4-cdffe9c27a73": "87aecc3c-fa7d-4a7f-87ec-d83db7dc3de0",  # Class 11 Science MBSE -> Physics
}

COURSE_TITLES = {
    "41ec43ac-bac7-4a68-b5d6-eda2acd85585": "Class 10 (CBSE)",
    "b6b1e3b0-6b34-4b1a-8b1a-000000000003": "Class 11 Arts (CBSE)",
    "b6b1e3b0-6b34-4b1a-8b1a-000000000002": "Class 11 Commerce (CBSE)",
    "b6b1e3b0-6b34-4b1a-8b1a-000000000001": "Class 11 Science (CBSE)",
    "cfe07ab8-1508-4c14-8181-8ba21d4cb331": "Class 10 (MBSE, Mizoram)",
    "24056c0b-1d46-411a-912d-5fecd2b8d90f": "Class 11 Arts (MBSE, Mizoram)",
    "51724c07-b13a-4413-85d8-d7cf2561fabb": "Class 11 Commerce (MBSE, Mizoram)",
    "eb7700aa-a95b-4eeb-a4e4-cdffe9c27a73": "Class 11 Science (MBSE, Mizoram)",
}


class _DryRunRollback(Exception):
    """Raised to unwind the atomic() block on --dry-run. Never caught by
    anything except this command's own handle() — see rule #1 in
    seed_academy_launch's module docstring on why a plain savepoint() outside
    atomic() is NOT a substitute for this."""


def _seed_pk(kind, key):
    return uuid.uuid5(SEED_NS, f"{kind}:{key}")


class Command(BaseCommand):
    help = (
        "Seed one course-intro PDF per target course and one practice quiz "
        "per subject, for the 8 Class 10/11 CBSE+MBSE courses."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing.")
        parser.add_argument(
            "--course-id", default=None,
            help="Limit to one course id (for staged rollout / re-testing).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        only_course = options["course_id"]

        User = get_user_model()
        try:
            uploader = User.objects.get(email__iexact=UPLOADER_EMAIL)
        except User.DoesNotExist:
            raise CommandError(f"Uploader account {UPLOADER_EMAIL} not found on this database.")

        course_ids = [only_course] if only_course else list(ANCHOR_SUBJECT)

        self.stdout.write(self.style.NOTICE(
            f"{'[DRY RUN] ' if dry_run else ''}Seeding {len(course_ids)} course(s)."
        ))

        materials_created = 0
        materials_existing = 0
        quizzes_created = 0
        quizzes_existing = 0
        questions_created = 0

        try:
            with transaction.atomic():
                for course_id in course_ids:
                    course = Course.objects.select_related("board").get(id=course_id)
                    m_created = self._make_course_material(course, uploader)
                    materials_created += int(m_created)
                    materials_existing += int(not m_created)

                    quiz_data = self._load_quiz_json(course_id)
                    for entry in quiz_data:
                        subject = Subject.objects.get(id=entry["subject_id"])
                        q_created, n_questions = self._make_subject_quiz(subject, entry, uploader)
                        quizzes_created += int(q_created)
                        quizzes_existing += int(not q_created)
                        questions_created += n_questions if q_created else 0

                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass

        self.stdout.write(self.style.SUCCESS(
            f"Materials: {materials_created} created, {materials_existing} already existed. "
            f"Quizzes: {quizzes_created} created ({questions_created} questions), "
            f"{quizzes_existing} already existed."
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing was actually written."))

    # ── materials ──────────────────────────────────────────────────────

    def _make_course_material(self, course, uploader):
        """Returns True if a new StudyMaterial+MaterialFile was created."""
        pk = _seed_pk("course-intro-material", course.id)
        if StudyMaterial.objects.filter(pk=pk).exists():
            return False

        anchor_subject_id = ANCHOR_SUBJECT[str(course.id)]
        subject = Subject.objects.get(id=anchor_subject_id)
        course_label = COURSE_TITLES[str(course.id)]

        pdf_path = os.path.join(PDF_DIR, f"{course.id}.pdf")
        if not os.path.exists(pdf_path):
            raise CommandError(f"Missing PDF for course {course.id} at {pdf_path}")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        material = StudyMaterial.objects.create(
            pk=pk,
            subject=subject,
            chapter=None,
            batch=None,
            title=f"{course_label} — Full Course Overview",
            description=(
                f"A one-page orientation to everything {course_label} covers this "
                "year — every subject, briefly. Start here before diving into "
                "individual chapters."
            ),
            uploaded_by=uploader,
        )
        MaterialFile.objects.create(
            material=material,
            uploaded_by=uploader,
            file=ContentFile(pdf_bytes, name=f"{course.slug}-course-overview.pdf"),
        )
        return True

    # ── quizzes ────────────────────────────────────────────────────────

    def _load_quiz_json(self, course_id):
        path = os.path.join(QUIZ_DIR, f"{course_id}.json")
        if not os.path.exists(path):
            raise CommandError(f"Missing quiz content for course {course_id} at {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data

    def _make_subject_quiz(self, subject, entry, uploader):
        """Returns (created: bool, n_questions: int)."""
        pk = _seed_pk("subject-practice-quiz", subject.id)
        if Quiz.objects.filter(pk=pk).exists():
            return False, 0

        quiz = Quiz.objects.create(
            pk=pk,
            subject=subject,
            title=entry["quiz_title"],
            description=f"A quick practice set on {subject.name}, drawn from this year's syllabus.",
            created_by=uploader,
            quiz_type=Quiz.TYPE_PRACTICE,
            time_limit_minutes=None,
            is_assigned=True,
        )
        # `batches` stays empty — course-wide is the only visibility this quiz
        # can have that reaches an un-batched learner or an anonymous visitor.
        # See quizzes/visibility.py; never add a batch here.

        total_marks = 0
        for q in entry["questions"]:
            question = Question.objects.create(
                quiz=quiz,
                text=q["text"],
                marks=1,
                explanation=q.get("explanation", ""),
                topic=q.get("topic", "") or "",
                difficulty=q.get("difficulty", Question.DIFFICULTY_MEDIUM),
            )
            correct_index = q["correct_index"]
            for i, option_text in enumerate(q["options"]):
                Choice.objects.create(
                    question=question,
                    text=option_text,
                    is_correct=(i == correct_index),
                )
            total_marks += question.marks

        quiz.total_marks = total_marks
        quiz.save(update_fields=["total_marks"])
        return True, len(entry["questions"])
