"""
courses/management/commands/seed_academy_launch.py

Launch groundwork for the Academy. Three separate jobs, in one command so
they share the same idempotency and the same undo:

  1. STRUCTURE (every PUBLISHED course) — one default batch, plus an example
     faculty roster staffed onto every subject. This is the part you actually
     keep: a course with no batch and no teacher cannot be enrolled into or
     taught, and every delivery-plane model in this codebase scopes through
     one or the other.

  2. CONTENT (flagship courses only) — notes, an assignment, a quiz and a
     recording per subject, so a walk-through hits a populated screen instead
     of six consecutive empty states.

  3. A way to undo all of it.

    python manage.py seed_academy_launch --dry-run   # report only, writes nothing
    python manage.py seed_academy_launch             # create, staged (invisible)
    python manage.py seed_academy_launch --go-live   # reveal the staged rows
    python manage.py seed_academy_launch --undo      # remove everything it made

WHY "STAGED" IS THE DEFAULT
---------------------------
This is meant to be safe to run against a database that already has real
students on it. Three things in this codebase reach a real user the instant a
row is written, so the default posture switches all three off:

  * `Assignment.is_published` and `Quiz.is_assigned` fire post_save signals
    (activity/signals.py:254 and :403) that write a bell row for EVERY active
    enrollee in the course. A management command triggers those exactly like a
    view does. Seeded as drafts, they notify nobody.
  * `SessionRecording.is_published` defaults to True
    (courses/models_recordings.py:111) — a recording is student-visible the
    moment it exists, before anyone decides it should be.
  * `TeacherProfile.academy_status == "approved"` is the ONLY filter on the
    chat directory (chat/services.py:1432-1437 — no role check, no is_active
    check), so an approved example teacher is immediately DM-able and globally
    searchable by every real student. Seeded as "pending" instead.

`--go-live` flips the first three. It deliberately does NOT publish recordings;
see RECORDINGS below.

RECORDINGS CANNOT BE FAKED
--------------------------
`bunny_embed_url` (config/bunny_signing.py:137-138) never contacts Bunny — it
only checks that the id is non-empty. So a made-up `bunny_video_id` returns
HTTP 200 with a real-looking signed URL, and the student dashboard renders a
live <iframe> at a video that does not exist. The "Can't play this recording"
error branch (RecordingDetail.jsx:215) is unreachable, because it only runs
when the API call itself throws. A fake recording is therefore a silently
broken player, which is worse than no recording at all.

The rows are created so the teacher-side list screens have something in them,
but they stay `is_published=False` permanently and `--go-live` skips them. A
real recording has to come from an actual upload or live class.

HOW ROWS ARE IDENTIFIED FOR --undo
----------------------------------
No model here has an `is_seed` field and adding one would be a migration
across six apps, so identification rides on fields that already exist:

  * users        — email ends with SEED_EMAIL_DOMAIN
  * batches      — code == BATCH_CODE, on a published course
  * materials    — primary key is uuid5(SEED_NS, "material:<subject id>")
  * quizzes      — primary key is uuid5(SEED_NS, "quiz:<subject id>")
  * recordings   — primary key is uuid5(SEED_NS, "recording:<subject id>")
  * assignments  — `idempotency_key` (unique, nullable), same uuid5 scheme.
                   Assignment's own PK is a UUID too, but this field is the
                   model's existing dedupe key, so using it keeps the seed
                   honest about what that field is for.

All four are derived, not stored-and-looked-up, which means re-running the
command finds its own rows instead of duplicating them, and --undo knows
exactly what to remove without a marker column.

Deliberately NOT anchored on uploaded_by/created_by: content is attributed to
the subject's real primary teacher when there is one (see
_content_author_for), because every teacher-side list screen scopes through
TeachingAssignment and a row filed under someone with no assignment there is
a row nobody can see. An author-based anchor would then miss precisely the
rows sitting on a real teacher's screen.

--undo is conservative on purpose. It refuses to delete a batch that has
acquired any enrollment, or any teaching assignment belonging to a real
teacher, because `Enrollment.batch` is SET_NULL (enrollments/models.py:142)
and an un-batched student then sees every assignment in the course, while
`TeachingAssignment.batch` is CASCADE (courses/models.py:505) and would take a
real teacher's access with it. It reports those instead of touching them.

It also deletes the `Activity` and `notifications.Notification` rows pointing
at whatever it removed. Nothing else does: Activity reaches its target through
a GenericForeignKey whose `object_id` is a plain UUIDField, and Notification
only keeps the id inside a JSON payload — so neither has a cascade, and both
would otherwise sit in real students' bells linking to a 404.
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import LearnerProfile, Role, TeacherProfile, UserRole
from activity.models import Activity
from assignments.models import Assignment, AssignmentFile
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment
from courses.models_recordings import SessionRecording
from materials.models import MaterialFile, StudyMaterial
from notifications.models import Notification
from quizzes.models import Choice, Question, Quiz

User = get_user_model()

# Fixed namespace: every derived key below is a uuid5 off this, so re-running
# the command produces the same keys and updates rather than duplicates.
SEED_NS = uuid.UUID("6f2a1c84-0f4b-5c9e-9a3d-7b1e0c5d2a68")

# Every account this command creates lives on this domain. It is not a real
# mail domain, which is the point: it makes the accounts obvious in the admin
# user list and gives --undo an exact anchor.
SEED_EMAIL_DOMAIN = "example.shikshacom.com"

SEED_PASSWORD = "ShikshaLaunch@2026"

# The default batch every published course gets. `code` is unique per course
# (courses/models.py unique_batch_code_per_course) and capped at 20 chars.
BATCH_CODE = "2026-27"
BATCH_NAME = "Batch 2026–27"

# Courses that get the full content treatment, as (title, board name). Both
# parts are needed: prod carries a CBSE *and* an MBSE course under several
# identical titles, so matching on title alone would hit the wrong one or
# raise MultipleObjectsReturned.
FLAGSHIP_COURSES = [
    ("Class 10", "CBSE"),
    ("Class 11 Science", "CBSE"),
    ("Class 12 Science", "CBSE"),
]

# Example faculty.
#
# `match` is specific tokens; `broad` is the catch-all tokens that only apply
# when nothing specific hit. Both are lowercase substrings tested against the
# subject name. Substrings rather than exact names is deliberate — the live
# subject vocabulary is inconsistent ("Hindi (Grammer)", "English ( Footrpints
# Without Feet)", "3B: Social Science – Geography"), so an exact-name table
# would silently staff almost nothing.
#
# ⚠ SEVERAL TEACHERS SHARE A SPECIALISM ON PURPOSE. One teacher per subject
# area produced a roster where a single English teacher held 100 subjects and
# one social-science teacher held 73, because so much of the catalogue is
# English/Hindi/Social-Science variants. Subjects are spread across everyone
# who matches (see _teacher_for_subject), so the count per person stays
# plausible for a real timetable.
FACULTY = [
    {
        "first": "Ananya", "last": "Sharma", "subject": "physics",
        "match": ["physics"], "broad": [],
        "qualification": "M.Sc. Physics, B.Ed.",
        "bio": "Physics faculty. Teaches mechanics and electromagnetism for Classes 11 and 12.",
    },
    {
        "first": "Rohit", "last": "Deshmukh", "subject": "chemistry",
        "match": ["chemistry"], "broad": [],
        "qualification": "M.Sc. Chemistry",
        "bio": "Chemistry faculty, with a focus on physical and organic chemistry.",
    },
    {
        "first": "Priya", "last": "Menon", "subject": "biology",
        "match": ["biology", "botany", "zoology"], "broad": [],
        "qualification": "M.Sc. Botany, B.Ed.",
        "bio": "Biology faculty. Handles Classes 11 and 12 board batches.",
    },
    {
        "first": "Sanjay", "last": "Kulkarni", "subject": "mathematics",
        "match": ["math"], "broad": [],
        "qualification": "M.Sc. Mathematics",
        "bio": "Mathematics faculty for the senior classes.",
    },
    {
        "first": "Deepa", "last": "Iyer", "subject": "mathematics",
        "match": ["math"], "broad": [],
        "qualification": "M.Sc. Mathematics, B.Ed.",
        "bio": "Mathematics faculty for Classes 8 to 10.",
    },
    {
        "first": "Meera", "last": "Krishnan", "subject": "english",
        "match": ["english"], "broad": [],
        "qualification": "M.A. English Literature, B.Ed.",
        "bio": "English faculty — main reader and literature.",
    },
    {
        "first": "Joseph", "last": "Lalrin", "subject": "english",
        "match": ["english"], "broad": [],
        "qualification": "M.A. English",
        "bio": "English faculty — grammar and writing skills.",
    },
    {
        "first": "Rebecca", "last": "Zothan", "subject": "english",
        "match": ["english"], "broad": [],
        "qualification": "M.A. English, B.Ed.",
        "bio": "English faculty — supplementary readers and spoken English.",
    },
    {
        "first": "Vikas", "last": "Chauhan", "subject": "hindi",
        "match": ["hindi"], "broad": ["mil"],
        "qualification": "M.A. Hindi",
        "bio": "Hindi faculty — main reader and supplementary texts.",
    },
    {
        "first": "Sunita", "last": "Yadav", "subject": "hindi",
        "match": ["hindi"], "broad": ["mil"],
        "qualification": "M.A. Hindi, B.Ed.",
        "bio": "Hindi faculty — grammar and composition.",
    },
    {
        "first": "Neha", "last": "Bansal", "subject": "economics",
        "match": ["econom", "statistic"], "broad": [],
        "qualification": "M.A. Economics",
        "bio": "Economics faculty — micro, macro and Indian economic development.",
    },
    {
        "first": "Ramesh", "last": "Gupta", "subject": "accountancy",
        "match": ["account", "business", "commerce"], "broad": [],
        "qualification": "M.Com., CA (Inter)",
        "bio": "Commerce faculty — accountancy and business studies.",
    },
    {
        "first": "Arun", "last": "Thapa", "subject": "history",
        "match": ["history"], "broad": ["social"],
        "qualification": "M.A. History, B.Ed.",
        "bio": "History faculty across the school and senior classes.",
    },
    {
        "first": "Lalnunpuii", "last": "Ralte", "subject": "geography",
        "match": ["geograph"], "broad": ["social"],
        "qualification": "M.A. Geography",
        "bio": "Geography faculty — physical and human geography.",
    },
    {
        "first": "David", "last": "Sailo", "subject": "political_science",
        "match": ["civic", "political", "sociolog"], "broad": ["social"],
        "qualification": "M.A. Political Science, B.Ed.",
        "bio": "Political science and civics faculty.",
    },
    {
        "first": "Kavita", "last": "Rao", "subject": "science",
        "match": ["science", "computer", "informatics"], "broad": [],
        # ⚠ "Social Science – Geography" and "Political Science" both contain
        # the substring "science", so without this the general-science teacher
        # silently competes for every social-science and political-science
        # subject in the catalogue — and wins about half of them.
        "avoid": ["social", "political"],
        "qualification": "M.Sc. Education",
        "bio": "General science faculty for the middle-school classes.",
    },
]
# Used when nothing matches at all. Kavita Rao is last and her "science" match
# is the broadest, so she doubles as the fallback.
GENERAL_TEACHER_IDX = len(FACULTY) - 1

# Quiz questions are generic on purpose: this seeds "a quiz exists and can be
# attempted end to end", not a real question bank. A subject-specific bank
# across 171 live subjects would be fabricated academic content presented to
# real students as if a teacher had written it.
GENERIC_QUESTIONS = [
    ("This is an example question. Which option is marked correct?",
     ["The first option", "The second option", "The third option", "The fourth option"], 0),
    ("Example questions like this one are here to show the quiz format.",
     ["Correct", "Incorrect", "Neither", "Both"], 0),
    ("Replace these with your own questions before assigning this quiz.",
     ["Understood", "Not yet", "Maybe", "Skip"], 0),
]


class _DryRunRollback(Exception):
    """Raised to unwind a --dry-run's transaction. Never escapes handle()."""


class Command(BaseCommand):
    help = (
        "Seed launch groundwork: a default batch and faculty on every published "
        "course, plus example content on the flagship courses. Staged (invisible) "
        "by default; see --go-live and --undo."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change and write nothing.",
        )
        parser.add_argument(
            "--go-live", action="store_true",
            help="Publish staged assignments/quizzes and approve the example "
                 "teachers. Does NOT publish recordings (they have no real video).",
        )
        parser.add_argument(
            "--undo", action="store_true",
            help="Delete everything this command created, plus the orphaned "
                 "activity and notification rows that no cascade cleans up.",
        )
        parser.add_argument(
            "--with-live-sessions", action="store_true",
            help="Also create example live classes. OFF by default: LiveSession's "
                 "post_save signal notifies every active enrollee in the course "
                 "with no batch filter at all (activity/signals.py:496-500), so on "
                 "a database with real students this one is not silent.",
        )
        parser.add_argument(
            "--structure-only", action="store_true",
            help="Batches and teaching assignments only — no example content.",
        )
        parser.add_argument(
            "--all-courses", action="store_true",
            help="Put example content on EVERY published course, not just the "
                 "flagship three.",
        )
        parser.add_argument(
            "--rebalance", action="store_true",
            help="Reassign example-owned teaching assignments to match the "
                 "current roster. Only touches rows held by an example "
                 "teacher — a real teacher is never moved.",
        )
        parser.add_argument(
            "--repair-teacher-profiles", action="store_true",
            help="Give a TeacherProfile to anyone who holds the TEACHER role "
                 "and real teaching assignments but has no profile row, which "
                 "keeps them off the faculty list entirely.",
        )
        parser.add_argument(
            "--flagship", action="append", default=None, metavar="COURSE_ID",
            help="Course id to treat as a flagship, repeatable. Overrides the "
                 "built-in title list, which is matched against production's "
                 "exact course titles and will not resolve on a database whose "
                 "titles differ.",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        modes = [options["go_live"], options["undo"]]
        if all(modes):
            raise CommandError("--go-live and --undo are mutually exclusive.")

        if options["undo"]:
            return self._undo()
        if options["go_live"]:
            return self._go_live()
        if options["rebalance"]:
            return self._rebalance()
        if options["repair_teacher_profiles"]:
            return self._repair_teacher_profiles()
        return self._create(
            structure_only=options["structure_only"],
            with_live_sessions=options["with_live_sessions"],
            flagship_ids=options["flagship"],
            all_courses=options["all_courses"],
        )

    # ── output helpers ───────────────────────────────────────────────────

    def _say(self, msg):
        self.stdout.write(msg)

    def _ok(self, msg):
        self.stdout.write(self.style.SUCCESS(msg))

    def _warn(self, msg):
        self.stdout.write(self.style.WARNING(msg))

    def _plan(self, msg):
        """A line that describes an intended write. Prefixed in dry-run."""
        self.stdout.write(("would: " if self.dry_run else "") + msg)

    # ── shared lookups ───────────────────────────────────────────────────

    def _published_courses(self):
        return (
            Course.objects.filter(status=Course.STATUS_PUBLISHED)
            .select_related("board")
            .order_by("title")
        )

    def _course_label(self, course):
        board = course.board.name if course.board_id else "no board"
        return f"{course.title} ({board})"

    def _seed_users(self):
        return User.objects.filter(email__endswith=f"@{SEED_EMAIL_DOMAIN}")

    def _seed_pk(self, kind, subject_id):
        """Deterministic primary key for a seeded content row.

        StudyMaterial, Quiz and SessionRecording all use a UUID primary key
        with a uuid4 default, which can simply be supplied instead. Deriving
        it from (kind, subject) buys two things at once: re-running the
        command finds the existing row rather than making a second one, and
        --undo can identify exactly what it wrote without depending on who the
        row is attributed to. That last part matters because content is filed
        under the subject's real teacher when there is one, so an
        uploaded_by/created_by anchor would miss precisely the rows sitting on
        somebody else's screen.
        """
        return uuid.uuid5(SEED_NS, f"{kind}:{subject_id}")

    def _seeded_content_ids(self):
        """Every content id this command could have written, by construction."""
        subject_ids = list(Subject.objects.values_list("id", flat=True))
        return {
            "material": [self._seed_pk("material", s) for s in subject_ids],
            "quiz": [self._seed_pk("quiz", s) for s in subject_ids],
            "recording": [self._seed_pk("recording", s) for s in subject_ids],
            "assignment": [
                uuid.uuid5(SEED_NS, f"assignment:{s}") for s in subject_ids
            ],
        }

    def _flagship_courses(self, flagship_ids=None):
        """Resolve the flagship courses, reporting any that miss.

        Returns only what actually exists — a renamed course should degrade to
        "no example content there" with a visible warning, not a crash on a
        production database.
        """
        if flagship_ids:
            found = []
            for raw in flagship_ids:
                match = Course.objects.filter(pk=raw).select_related("board").first()
                if match is None:
                    self._warn(f"  --flagship {raw}: no such course, skipping")
                    continue
                if match.status != Course.STATUS_PUBLISHED:
                    self._warn(
                        f"  --flagship {raw}: {match.title!r} is {match.status}, "
                        "not PUBLISHED — it has no batch and will get no content"
                    )
                    continue
                found.append(match)
            return found

        found = []
        for title, board_name in FLAGSHIP_COURSES:
            match = (
                Course.objects.filter(
                    title=title,
                    board__name=board_name,
                    status=Course.STATUS_PUBLISHED,
                )
                .select_related("board")
                .first()
            )
            if match is None:
                self._warn(
                    f"  flagship course not found, skipping content for it: "
                    f"{title!r} ({board_name})"
                )
                continue
            found.append(match)
        return found

    def _teacher_for_subject(self, subject, teachers):
        """Which example teacher should hold this subject.

        Two tiers, then an even spread:

        1. Specific tokens win. "3B: Social Science – Geography" contains both
           "social" and "geograph"; only the geography teacher should get it,
           so `broad` tokens are consulted only when nothing specific matched.
        2. Among everyone who matched at the winning tier, the subject is
           placed by `uuid % count`. Several teachers deliberately share a
           specialism — with one per area, a single English teacher ended up
           holding 100 subjects.

        Keyed on the subject UUID rather than a running counter so the answer
        is the same on every run regardless of iteration order. That is what
        lets --rebalance be idempotent instead of reshuffling the roster each
        time it is called.
        """
        lowered = subject.name.lower()

        def hits(spec, tokens):
            if any(bad in lowered for bad in spec.get("avoid", [])):
                return False
            return any(tok in lowered for tok in tokens)

        specific = [
            i for i, spec in enumerate(FACULTY) if hits(spec, spec["match"])
        ]
        candidates = specific or [
            i for i, spec in enumerate(FACULTY)
            if hits(spec, spec.get("broad", []))
        ]
        if not candidates:
            return teachers[GENERAL_TEACHER_IDX]

        return teachers[candidates[subject.id.int % len(candidates)]]

    def _content_author_for(self, subject, teachers):
        """Whoever actually teaches this subject, else the roster match.

        Content is attributed to the subject's real active primary teacher
        when there is one. Staffing runs before content, so on an unstaffed
        subject that is the example teacher anyway — but on a subject a real
        teacher already holds, attributing the material to an example teacher
        who has no assignment there would file it under someone who cannot
        see it: every teacher-side list screen scopes through
        TeachingAssignment.
        """
        base = TeachingAssignment.objects.filter(
            subject=subject, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY,
            # ⚠ teacher is null=True with on_delete=SET_NULL, so a subject can
            # carry an active PRIMARY row with NOBODY in it — production has
            # these, left behind when a teacher account was deleted. Without
            # this filter the row still satisfies "has a primary", and the
            # content below is created with uploaded_by=None, which is a
            # not-null violation on StudyMaterial.
            teacher__isnull=False,
        ).select_related("teacher")

        # Course-wide first, then any batch-scoped one. Two queries rather
        # than order_by("batch__isnull"), which is not a thing Django accepts
        # in order_by and raises FieldError at runtime.
        primary = base.filter(batch__isnull=True).first() or base.first()
        if primary is not None:
            return primary.teacher
        return self._teacher_for_subject(subject, teachers)

    # ── create ───────────────────────────────────────────────────────────

    def _create(self, structure_only, with_live_sessions, flagship_ids=None,
                all_courses=False):
        courses = list(self._published_courses())
        if not courses:
            raise CommandError(
                "No PUBLISHED courses found — nothing to seed. Check that you are "
                "pointed at the right database."
            )

        self._say(f"{len(courses)} published course(s) in scope.")
        self._say("")

        if self.dry_run:
            # A dry run still needs the real objects to describe accurate
            # counts, so it does the work for real and then throws it away.
            # Keeping the reporting path and the writing path identical is the
            # point — a dry run that took a different code path would not be
            # telling you about the real one.
            #
            # The rollback MUST be a raise out of transaction.atomic(). The
            # obvious-looking transaction.savepoint() / savepoint_rollback()
            # pair does nothing at all in autocommit mode: savepoint() returns
            # None and the rollback is silently a no-op, so --dry-run commits
            # everything it claims to be discarding. That is not hypothetical,
            # it is what the first version of this command did.
            try:
                with transaction.atomic():
                    self._do_create(courses, structure_only, with_live_sessions,
                                    flagship_ids, all_courses)
                    raise _DryRunRollback()
            except _DryRunRollback:
                pass
            self._say("")
            self._warn("DRY RUN — everything above was rolled back. Nothing was written.")
            return

        with transaction.atomic():
            self._do_create(courses, structure_only, with_live_sessions,
                            flagship_ids, all_courses)

        self._say("")
        self._ok("Done. Everything is staged and invisible to students.")
        self._say(f"Example accounts: <name>@{SEED_EMAIL_DOMAIN} / {SEED_PASSWORD}")
        self._say("Next: --go-live to reveal it, or --undo to remove it.")

    def _do_create(self, courses, structure_only, with_live_sessions,
                   flagship_ids=None, all_courses=False):
        teachers = self._make_faculty()
        self._say("")

        for course in courses:
            self._make_batch(course)

        self._say("")
        self.skipped_staffed = 0
        self.adopted_empty = 0
        self.fixed_quiz_scope = 0
        assigned = 0
        for course in courses:
            assigned += self._staff_course(course, teachers)
        self._plan(f"staff {assigned} previously-unstaffed subject(s) across {len(courses)} course(s)")
        if self.skipped_staffed:
            self._say(
                f"       {self.skipped_staffed} subject(s) already have a teacher — left alone"
            )
        if self.adopted_empty:
            self._say(
                f"       {self.adopted_empty} empty 'primary teacher' slot(s) filled — "
                "these were blocking their subject from ever being staffed"
            )

        if structure_only:
            self._say("")
            self._say("--structure-only: skipping example content.")
            return

        self._say("")
        flagships = courses if all_courses else self._flagship_courses(flagship_ids)
        for course in flagships:
            self._make_content(course, teachers, with_live_sessions)
        if self.fixed_quiz_scope:
            self._say(
                f"       {self.fixed_quiz_scope} quiz(zes) widened to course-wide — "
                "batch-scoped ones are invisible to un-batched students"
            )

    # ── faculty ──────────────────────────────────────────────────────────

    def _make_faculty(self):
        """Create the example teacher accounts, staged as pending.

        `academy_status` stays TRACK_PENDING until --go-live. That is what
        keeps them out of the chat directory, which filters on that field and
        nothing else (chat/services.py:1432-1437).
        """
        role, _ = Role.objects.get_or_create(name="TEACHER")
        teachers = []

        for spec in FACULTY:
            local = f"{spec['first']}.{spec['last']}".lower()
            email = f"{local}@{SEED_EMAIL_DOMAIN}"

            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "username": local,
                    "first_name": spec["first"],
                    "last_name": spec["last"],
                },
            )
            if created:
                user.set_password(SEED_PASSWORD)
                user.is_verified = True
                user.verified_at = timezone.now()
                user.is_active = True
                user.save()
                self._plan(f"create teacher {spec['first']} {spec['last']} <{email}>")

            UserRole.objects.get_or_create(
                user=user, role=role,
                defaults={"is_active": True, "is_primary": True},
            )
            # Teachers need a self LearnerProfile too — the login flow selects a
            # profile before it can hand out a teacher context.
            LearnerProfile.objects.get_or_create(
                account=user,
                relationship=LearnerProfile.RELATIONSHIP_SELF,
                defaults={
                    "display_name": f"{spec['first']} {spec['last']}",
                    "is_default": True,
                    "first_name": spec["first"],
                    "last_name": spec["last"],
                },
            )
            TeacherProfile.objects.get_or_create(
                user=user,
                defaults={
                    "teacher_type": TeacherProfile.TYPE_FACULTY,
                    # Staged. --go-live promotes this to TRACK_APPROVED.
                    "academy_status": TeacherProfile.TRACK_PENDING,
                    "is_approved": False,
                    "subject": spec["subject"],
                    "qualification": spec["qualification"],
                    "bio": spec["bio"],
                },
            )
            teachers.append(user)

        self._plan(f"ensure {len(teachers)} example teacher account(s)")
        return teachers

    # ── batches ──────────────────────────────────────────────────────────

    def _make_batch(self, course):
        existing = Batch.objects.filter(course=course, code=BATCH_CODE).first()
        if existing:
            return existing

        today = timezone.now().date()
        batch = Batch.objects.create(
            course=course,
            code=BATCH_CODE,
            name=BATCH_NAME,
            year=today.year,
            start_date=today,
            is_active=True,
        )
        self._plan(f"batch {BATCH_CODE!r} on {self._course_label(course)}")
        return batch

    def _staff_course(self, course, teachers):
        """Fill the unstaffed subjects on this course. Never displace anyone.

        The model allows exactly one active PRIMARY per subject course-wide,
        and one per (batch, subject) — four constraints at
        courses/models.py Meta. So this can only add a primary where there
        isn't one, and a subject that already has a real teacher is skipped
        rather than fought over.

        batch=NULL means course-wide and a specific batch means batch-scoped;
        they are separate rows under those constraints and admins create both
        in practice, so both are filled when both are free.
        """
        batch = Batch.objects.filter(course=course, code=BATCH_CODE).first()
        added = 0
        for subject in course.subjects.all():
            teacher = self._teacher_for_subject(subject, teachers)

            course_primary = TeachingAssignment.objects.filter(
                subject=subject, batch__isnull=True, is_active=True,
                role=TeachingAssignment.ROLE_PRIMARY,
            ).first()
            if course_primary is None:
                TeachingAssignment.objects.create(
                    subject=subject, teacher=teacher, batch=None,
                    role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
                )
                added += 1
            elif course_primary.teacher_id is None:
                # An active PRIMARY row with nobody in it, left behind by
                # SET_NULL when a teacher account was deleted. It is not
                # staffing — but it does occupy the one-active-primary-per-
                # subject slot, so the subject can never be staffed while it
                # sits there. Adopt it rather than skip: there is no one to
                # displace, and the alternative is a subject that stays
                # permanently unassignable.
                course_primary.teacher = teacher
                course_primary.save(update_fields=["teacher"])
                self.adopted_empty += 1
                added += 1
            else:
                self.skipped_staffed += 1

            if batch is not None:
                batch_primary = TeachingAssignment.objects.filter(
                    subject=subject, batch=batch, is_active=True,
                    role=TeachingAssignment.ROLE_PRIMARY,
                ).first()
                if batch_primary is None:
                    TeachingAssignment.objects.create(
                        subject=subject, teacher=teacher, batch=batch,
                        role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
                    )
                elif batch_primary.teacher_id is None:
                    batch_primary.teacher = teacher
                    batch_primary.save(update_fields=["teacher"])
        return added

    # ── example content ──────────────────────────────────────────────────

    def _make_content(self, course, teachers, with_live_sessions):
        batch = Batch.objects.filter(course=course, code=BATCH_CODE).first()
        subjects = list(course.subjects.all())
        self._plan(
            f"example content on {self._course_label(course)} "
            f"— {len(subjects)} subject(s)"
        )

        for subject in subjects:
            teacher = self._content_author_for(subject, teachers)
            chapter = subject.chapters.order_by("order", "id").first()
            self._make_material(subject, chapter, batch, teacher)
            self._make_assignment(subject, chapter, batch)
            self._make_quiz(subject, batch, teacher)
            self._make_recording(subject, chapter, batch, teacher)

        if with_live_sessions:
            self._make_live_session(course, subjects, batch, teachers)

    def _make_material(self, subject, chapter, batch, teacher):
        material, created = StudyMaterial.objects.get_or_create(
            pk=self._seed_pk("material", subject.id),
            defaults={
                "subject": subject,
                "chapter": chapter,
                "batch": batch,
                "title": f"{subject.name} — example notes",
                "description": (
                    "Example study notes, added while setting the course up. "
                    "Replace this with your own material."
                ),
                "uploaded_by": teacher,
            },
        )
        if created:
            MaterialFile.objects.create(
                material=material,
                uploaded_by=teacher,
                file=ContentFile(
                    (
                        f"{subject.name} — example notes\n"
                        f"{'=' * (len(subject.name) + 18)}\n\n"
                        "This is placeholder content added when the course was "
                        "set up, so the study-materials screen has something in "
                        "it. Replace it with real notes.\n"
                    ).encode(),
                    name=f"example-notes-{uuid.uuid4().hex[:8]}.txt",
                ),
            )
        return material

    def _make_assignment(self, subject, chapter, batch):
        """Create a DRAFT assignment.

        `is_published=False` is what keeps the post_save signal quiet — the
        signal fires on the False→True transition, and a row created already
        published counts as that transition (activity/signals.py:238-261).
        """
        key = uuid.uuid5(SEED_NS, f"assignment:{subject.id}")
        existing = Assignment.objects.filter(idempotency_key=key).first()
        if existing:
            return existing

        assignment = Assignment.objects.create(
            subject=subject,
            chapter=chapter,
            batch=batch,
            title=f"{subject.name} — example assignment",
            description=(
                "Example assignment added while setting the course up. Edit the "
                "brief and the due date, then publish it when it is ready."
            ),
            due_date=timezone.now() + timedelta(days=14),
            max_marks=20,
            is_published=False,
            idempotency_key=key,
        )
        AssignmentFile.objects.create(
            assignment=assignment,
            file=ContentFile(
                (
                    f"{subject.name} — example assignment brief\n\n"
                    "Placeholder brief. Replace with the real task before "
                    "publishing.\n"
                ).encode(),
                name=f"example-brief-{uuid.uuid4().hex[:8]}.txt",
            ),
        )
        return assignment

    def _make_quiz(self, subject, batch, teacher):
        """Create an UNASSIGNED quiz.

        `is_assigned` — not `is_published` — is the flag students filter on
        (quizzes/visibility.py), and it is also the one the notify signal
        watches. Left False so the quiz is invisible and silent.
        """
        pk = self._seed_pk("quiz", subject.id)
        quiz = Quiz.objects.filter(pk=pk).first()
        if quiz:
            # Self-heal rows written before this was understood; see the
            # course-wide note below. Clearing an already-empty M2M is free.
            if quiz.batches.exists():
                quiz.batches.clear()
                self.fixed_quiz_scope += 1
            return quiz

        quiz = Quiz.objects.create(
            pk=pk,
            subject=subject,
            title=f"{subject.name} — example quiz",
            description=(
                "Example quiz added during setup. The questions are placeholders "
                "— replace them before assigning this to a batch."
            ),
            created_by=teacher,
            quiz_type=Quiz.TYPE_MOCK,
            time_limit_minutes=15,
            is_assigned=False,
        )
        # ⚠ LEFT COURSE-WIDE (empty `batches`) ON PURPOSE. Quizzes do not
        # follow the same batch rule as everything else around them:
        #
        #   materials    (materials/views.py _batch_scope_q) -> unplaced
        #   assignments  (assignments/views.py:318-326)         learner sees
        #                                                       EVERYTHING
        #   quizzes      (quizzes/visibility.py batch_scope_q) -> unplaced
        #                                                        learner sees
        #                                                        ONLY
        #                                                        course-wide
        #
        # That asymmetry is deliberate and documented in visibility.py. The
        # consequence here is that a quiz scoped to the seeded batch is
        # invisible to EVERY student — the un-batched ones because they only
        # get course-wide quizzes, and the batched ones because nobody is
        # enrolled in the seeded batch. Example material and assignments
        # showed up fine and only the quizzes were missing, which is a
        # genuinely confusing way to find this out.

        total = 0
        for text, options, correct_idx in GENERIC_QUESTIONS:
            question = Question.objects.create(quiz=quiz, text=text, marks=1)
            for i, option in enumerate(options):
                Choice.objects.create(
                    question=question, text=option, is_correct=(i == correct_idx),
                )
            total += question.marks
        quiz.total_marks = total
        quiz.save(update_fields=["total_marks"])
        return quiz

    def _make_recording(self, subject, chapter, batch, teacher):
        """Create an UNPUBLISHED placeholder recording.

        Permanently unpublished — see the RECORDINGS note in the module
        docstring. There is no real Bunny video behind this id, and publishing
        it would give students a broken player with no error state.
        """
        pk = self._seed_pk("recording", subject.id)
        existing = SessionRecording.objects.filter(pk=pk).first()
        if existing:
            return existing

        return SessionRecording.objects.create(
            pk=pk,
            subject=subject,
            chapter=chapter,
            batch=batch,
            title=f"{subject.name} — example class recording",
            description=(
                "Placeholder row so the recordings screen is not empty. There is "
                "no video behind it — record a live class or upload one to "
                "replace this."
            ),
            session_date=timezone.now().date(),
            duration_seconds=None,
            bunny_video_id=f"placeholder-{uuid.uuid5(SEED_NS, f'rec:{subject.id}').hex[:12]}",
            status=0,  # Created — never reached Finished, because it never existed.
            uploaded_by=teacher,
            is_published=False,
        )

    def _make_live_session(self, course, subjects, batch, teachers):
        from livestream.models import LiveSession

        if not subjects:
            return None
        subject = subjects[0]
        teacher = self._teacher_for_subject(subject, teachers)
        room = f"example-{uuid.uuid5(SEED_NS, f'live:{course.id}').hex[:12]}"
        existing = LiveSession.objects.filter(room_name=room).first()
        if existing:
            return existing

        start = timezone.now() + timedelta(days=3)
        self._plan(f"live session on {self._course_label(course)} (this one notifies)")
        return LiveSession.objects.create(
            course=course, subject=subject, batch=batch,
            title=f"{subject.name} — example scheduled class",
            start_time=start,
            end_time=start + timedelta(hours=1),
            status=LiveSession.STATUS_SCHEDULED,
            created_by=teacher,
        )

    # ── go live ──────────────────────────────────────────────────────────

    def _go_live(self):
        seed_users = list(self._seed_users())
        if not seed_users:
            raise CommandError(
                f"No accounts on @{SEED_EMAIL_DOMAIN} — nothing was seeded here, "
                "so there is nothing to reveal."
            )

        ids = self._seeded_content_ids()
        assignments = Assignment.objects.filter(
            idempotency_key__in=ids["assignment"], is_published=False,
        )
        quizzes = Quiz.objects.filter(pk__in=ids["quiz"], is_assigned=False)
        profiles = TeacherProfile.objects.filter(
            user__in=seed_users, academy_status=TeacherProfile.TRACK_PENDING,
        )
        recordings = SessionRecording.objects.filter(
            pk__in=ids["recording"], is_published=False,
        )

        n_assign, n_quiz, n_prof = assignments.count(), quizzes.count(), profiles.count()

        self._say(f"publish {n_assign} assignment(s)")
        self._say(f"assign {n_quiz} quiz(zes)")
        self._say(f"approve {n_prof} teacher(s)")
        self._warn(
            f"leave {recordings.count()} recording(s) unpublished — they have no "
            "real video behind them and would render a broken player"
        )
        self._warn(
            "publishing assignments and assigning quizzes writes a notification "
            "to every active enrollee in those courses."
        )

        if self.dry_run:
            self._say("")
            self._warn("DRY RUN — nothing was written.")
            return

        with transaction.atomic():
            # One save() each, not a bulk update(): .update() does not fire
            # post_save, and the notification to students IS the point of
            # publishing. A bulk update here would silently publish everything
            # while telling nobody.
            for assignment in assignments:
                assignment.is_published = True
                assignment.save(update_fields=["is_published"])
            for quiz in quizzes:
                quiz.is_assigned = True
                quiz.save(update_fields=["is_assigned"])
            for profile in profiles:
                profile.academy_status = TeacherProfile.TRACK_APPROVED
                profile.is_approved = True
                profile.save(update_fields=["academy_status", "is_approved"])

        self._say("")
        self._ok("Live.")

    # ── rebalance ────────────────────────────────────────────────────────

    def _rebalance(self):
        """Move example-owned assignments onto the current roster.

        Needed because the roster grew: one teacher per subject area gave a
        single English teacher 100 subjects. Adding colleagues changes who
        _teacher_for_subject picks, but the rows already written still point at
        the old answer.

        Only rows whose CURRENT holder is an example teacher are touched. A
        real teacher is never moved — losing a subject you actually teach
        because a seeding script rebalanced itself would be a genuinely bad
        outcome.
        """
        seed_users = list(self._seed_users())
        if not seed_users:
            raise CommandError(
                f"No accounts on @{SEED_EMAIL_DOMAIN} — nothing to rebalance."
            )
        by_email = {u.email: u for u in seed_users}
        teachers = []
        for spec in FACULTY:
            email = f"{spec['first']}.{spec['last']}".lower() + f"@{SEED_EMAIL_DOMAIN}"
            if email not in by_email:
                raise CommandError(
                    f"Roster entry {email} has no account — run the command "
                    "without --rebalance first so the new teachers exist."
                )
            teachers.append(by_email[email])

        rows = (
            TeachingAssignment.objects
            .filter(teacher__in=seed_users, is_active=True)
            .select_related("subject")
        )

        moves = []
        for row in rows:
            wanted = self._teacher_for_subject(row.subject, teachers)
            if wanted.id != row.teacher_id:
                moves.append((row, wanted))

        self._say(f"{rows.count()} example-held assignment(s) in scope")
        self._say(f"{len(moves)} would move")

        if self.dry_run:
            self._say("")
            self._warn("DRY RUN — nothing was written.")
            return

        moved = 0
        with transaction.atomic():
            for row, wanted in moves:
                # uniq_active_teacher_per_batch_subject: the target must not
                # already hold this (batch, subject) on another active row, or
                # the update trips the constraint.
                clash = TeachingAssignment.objects.filter(
                    subject=row.subject, batch=row.batch, teacher=wanted,
                    is_active=True,
                ).exclude(pk=row.pk).exists()
                if clash:
                    continue
                row.teacher = wanted
                row.save(update_fields=["teacher"])
                moved += 1

        reattributed = self._reattribute_content(seed_users, teachers)

        self._say("")
        self._ok(f"Rebalanced {moved} assignment(s).")
        self._say(f"Re-filed {reattributed} content row(s) under the new holder.")
        self._report_distribution(seed_users)

    def _reattribute_content(self, seed_users, teachers):
        """Move seeded content to whoever now holds its subject.

        Rebalancing changes who teaches a subject, but the material, quiz and
        recording rows still point at the previous holder — and every
        teacher-side list screen scopes through TeachingAssignment, so that
        content becomes invisible to the person who now owns the subject and
        stays visible to someone who no longer teaches it.

        Only rows currently attributed to an EXAMPLE teacher are moved. Content
        filed under a real teacher stays put, exactly as _content_author_for
        intended when it put it there.
        """
        seed_ids = {u.id for u in seed_users}
        ids = self._seeded_content_ids()
        moved = 0

        for model, id_key, field in (
            (StudyMaterial, "material", "uploaded_by"),
            (Quiz, "quiz", "created_by"),
            (SessionRecording, "recording", "uploaded_by"),
        ):
            rows = model.objects.filter(pk__in=ids[id_key]).select_related("subject")
            for row in rows:
                if getattr(row, f"{field}_id") not in seed_ids:
                    continue  # a real teacher's row — leave it alone
                wanted = self._content_author_for(row.subject, teachers)
                if wanted.id == getattr(row, f"{field}_id"):
                    continue
                setattr(row, field, wanted)
                row.save(update_fields=[field])
                moved += 1

        return moved

    def _report_distribution(self, seed_users):
        counts = []
        for user in seed_users:
            n = TeachingAssignment.objects.filter(
                teacher=user, is_active=True,
            ).count()
            counts.append((n, f"{user.first_name} {user.last_name}"))
        counts.sort(reverse=True)
        self._say("")
        self._say("subjects held per example teacher:")
        for n, name in counts:
            self._say(f"   {n:4}  {name}")

    # ── repair teacher profiles ──────────────────────────────────────────

    def _repair_teacher_profiles(self):
        """Give a TeacherProfile to people who are teaching without one.

        The faculty list queries TeacherProfile, so someone holding the TEACHER
        role and real teaching assignments but no profile row is invisible on
        every faculty surface while still owning subjects and content. That is
        not a seeding artefact — it is a real account set up half way, and it
        looks exactly like a bug in the teacher list.

        Only touches accounts that already hold ACTIVE teaching assignments, so
        this cannot promote someone who merely has the role.
        """
        candidates = (
            User.objects
            .filter(
                user_roles__role__name="TEACHER",
                user_roles__is_active=True,
                teaching_assignments__is_active=True,
            )
            .exclude(teacher_profile__isnull=False)
            .exclude(email__endswith=f"@{SEED_EMAIL_DOMAIN}")
            .distinct()
        )

        rows = list(candidates)
        if not rows:
            self._ok("Every teaching account already has a profile.")
            return

        for user in rows:
            n = TeachingAssignment.objects.filter(
                teacher=user, is_active=True,
            ).count()
            self._plan(
                f"create an approved TeacherProfile for {user.email} "
                f"({n} active assignment(s))"
            )

        if self.dry_run:
            self._say("")
            self._warn("DRY RUN — nothing was written.")
            return

        with transaction.atomic():
            for user in rows:
                TeacherProfile.objects.create(
                    user=user,
                    teacher_type=TeacherProfile.TYPE_FACULTY,
                    academy_status=TeacherProfile.TRACK_APPROVED,
                    is_approved=True,
                )

        self._say("")
        self._ok(f"Created {len(rows)} teacher profile(s).")
        self._warn(
            "These accounts are now on the faculty list and reachable in chat, "
            "which is what a teaching account is supposed to be."
        )

    # ── undo ─────────────────────────────────────────────────────────────

    def _undo(self):
        seed_users = list(self._seed_users())
        if not seed_users:
            self._warn(
                f"No accounts on @{SEED_EMAIL_DOMAIN} — nothing to undo."
            )
            return

        ids = self._seeded_content_ids()
        materials = StudyMaterial.objects.filter(pk__in=ids["material"])
        recordings = SessionRecording.objects.filter(pk__in=ids["recording"])
        quizzes = Quiz.objects.filter(pk__in=ids["quiz"])
        assignments = Assignment.objects.filter(
            idempotency_key__in=ids["assignment"]
        )

        # Collect ids BEFORE deleting — the orphaned Activity and Notification
        # rows are matched on them afterwards, and once the content rows are
        # gone there is no way back to the ids.
        doomed_ids = set()
        for qs in (materials, recordings, quizzes, assignments):
            doomed_ids.update(str(pk) for pk in qs.values_list("id", flat=True))

        self._say(f"delete {materials.count()} material(s)")
        self._say(f"delete {recordings.count()} recording(s)")
        self._say(f"delete {quizzes.count()} quiz(zes)")
        self._say(f"delete {assignments.count()} assignment(s)")

        # Batches: only ones that are still untouched. See the module docstring
        # for why a batch with real enrollments must not be deleted.
        removable, kept = [], []
        for batch in Batch.objects.filter(code=BATCH_CODE):
            n_enrolled = batch.enrollments.count()
            n_foreign_ta = (
                TeachingAssignment.objects
                .filter(batch=batch)
                .exclude(teacher__in=seed_users)
                .count()
            )
            if n_enrolled or n_foreign_ta:
                kept.append((batch, n_enrolled, n_foreign_ta))
            else:
                removable.append(batch)

        self._say(f"delete {len(removable)} batch(es)")
        for batch, n_enrolled, n_foreign_ta in kept:
            self._warn(
                f"KEEP batch {batch.code!r} on {self._course_label(batch.course)} — "
                f"{n_enrolled} enrollment(s), {n_foreign_ta} non-example teaching "
                "assignment(s). Deleting it would un-batch real students and "
                "revoke a real teacher's access."
            )

        n_activity = Activity.objects.filter(object_id__in=doomed_ids).count()
        n_notif = Notification.objects.filter(
            payload__object_id__in=list(doomed_ids)
        ).count()
        self._say(f"delete {n_activity} orphaned activity row(s)")
        self._say(f"delete {n_notif} orphaned notification(s)")
        self._say(f"delete {len(seed_users)} example account(s)")

        if self.dry_run:
            self._say("")
            self._warn("DRY RUN — nothing was written.")
            return

        with transaction.atomic():
            Activity.objects.filter(object_id__in=doomed_ids).delete()
            Notification.objects.filter(
                payload__object_id__in=list(doomed_ids)
            ).delete()
            materials.delete()
            recordings.delete()
            quizzes.delete()
            assignments.delete()
            TeachingAssignment.objects.filter(teacher__in=seed_users).delete()
            for batch in removable:
                batch.delete()
            # Deleting the account cascades its TeacherProfile and
            # LearnerProfile. Done last so the content queries above still
            # resolved their uploaded_by/created_by anchors.
            User.objects.filter(pk__in=[u.pk for u in seed_users]).delete()

        self._say("")
        self._ok("Undone.")
        if kept:
            self._warn(
                f"{len(kept)} batch(es) were kept because real data now depends "
                "on them — see above."
            )
