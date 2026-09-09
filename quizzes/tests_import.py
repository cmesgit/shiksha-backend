"""Cover for `manage.py import_question_bank`.

This file is separate from tests.py purely to keep a large, self-contained
subject in its own module; Django's discovery picks up `test*.py` either way.

The thing these tests exist to prevent is a SILENT bad import. A question
bank whose answers are wrong is worse than an empty one — a learner has no
way to tell, and the whole proposition of the public hub is that the answer
and the explanation can be trusted. So the assertions below are mostly about
what the importer REFUSES to do.
"""
import json
import tempfile
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from quizzes.models import Choice, Question, QuestionTag


def row(**over):
    base = {
        "stem": "Which Harappan site is best known for its dockyard?",
        "options": ["Kalibangan", "Lothal", "Dholavira", "Ropar"],
        "answer_index": 1,
        "explanation": "Lothal in Gujarat has the earliest known dockyard.",
        "subject": "History",
        "topic": "Indus Valley",
        "exam_names": ["SSC CGL"],
        "years": [2019],
        "difficulty": "medium",
    }
    base.update(over)
    return base


def write(rows):
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"questions": rows}, fh)
    fh.close()
    return fh.name


def run(path, *args):
    out = StringIO()
    call_command("import_question_bank", path, *args, stdout=out)
    return out.getvalue()


class ImportHappyPathTest(TestCase):

    def test_it_creates_a_standalone_question_with_choices_and_tags(self):
        run(write([row()]))
        q = Question.objects.get()
        self.assertIsNone(q.quiz_id)
        self.assertEqual(q.source, Question.SOURCE_IMPORT)
        self.assertEqual(q.year, 2019)
        self.assertEqual(q.question_type, Question.TYPE_SINGLE)
        self.assertEqual(q.choices.count(), 4)
        self.assertEqual(q.choices.filter(is_correct=True).count(), 1)
        self.assertEqual(q.choices.get(is_correct=True).text, "Lothal")
        self.assertEqual(
            {t.kind for t in q.tags.all()},
            {QuestionTag.KIND_SUBJECT, QuestionTag.KIND_EXAM, QuestionTag.KIND_TOPIC},
        )

    def test_nothing_is_imported_as_accepted(self):
        """THE rule. A parser cannot confer trust; an admin promotes."""
        run(write([row()]))
        self.assertEqual(Question.objects.get().bank_state,
                         Question.BANK_STATE_SUGGESTED)
        self.assertEqual(
            Question.objects.filter(bank_state=Question.BANK_STATE_ACCEPTED).count(), 0)

    def test_new_rails_default_to_soon_not_live(self):
        """An import must never make a subject chip clickable by itself —
        the questions behind it have not been reviewed yet."""
        run(write([row()]))
        for tag in QuestionTag.objects.all():
            self.assertEqual(tag.status, QuestionTag.STATUS_SOON)

    def test_a_leading_source_number_is_stripped_from_the_stem(self):
        run(write([row(stem="1193. Which of these is a dockyard site?")]))
        self.assertTrue(Question.objects.get().text.startswith("Which"))

    def test_dry_run_writes_nothing(self):
        out = run(write([row()]), "--dry-run")
        self.assertIn("DRY RUN", out)
        self.assertEqual(Question.objects.count(), 0)


class ImportRefusalTest(TestCase):
    """Every one of these would be a wrong or unusable question on a page
    that promises a trustworthy answer and an explanation."""

    def _assert_refused(self, bad_row):
        run(write([bad_row]))
        self.assertEqual(Question.objects.count(), 0)

    def test_no_answer_is_refused(self):
        self._assert_refused(row(answer_index=None))

    def test_no_explanation_is_refused(self):
        self._assert_refused(row(explanation=""))

    def test_an_answer_index_out_of_range_is_refused(self):
        self._assert_refused(row(answer_index=7))

    def test_fewer_than_two_options_is_refused(self):
        self._assert_refused(row(options=["Only one"]))

    def test_duplicate_option_text_is_refused(self):
        """The signature of a bad parse: when two options read the same, the
        answer index is ambiguous and the row cannot be graded honestly even
        though it looks complete."""
        self._assert_refused(
            row(options=["Lothal", "Kalibangan", "Lothal", "Ropar"]))

    def test_include_imperfect_still_refuses_a_missing_answer(self):
        """--include-imperfect widens the SOFT gates only. The hard ones are
        not negotiable, or the flag becomes a way to import garbage."""
        run(write([row(answer_index=None)]), "--include-imperfect")
        self.assertEqual(Question.objects.count(), 0)

    def test_a_messy_stem_is_held_back_by_default_and_admitted_on_request(self):
        messy = row(stem="was the Mahajanapada capital of Vajji")
        run(write([messy]))
        self.assertEqual(Question.objects.count(), 0)
        run(write([messy]), "--include-imperfect")
        self.assertEqual(Question.objects.count(), 1)


class ImportIdempotencyTest(TestCase):

    def test_running_twice_does_not_duplicate(self):
        path = write([row()])
        run(path)
        run(path)
        self.assertEqual(Question.objects.count(), 1)

    def test_the_same_stem_with_different_options_is_a_different_question(self):
        """Previous-year papers reuse a stem with a changed option set, and to
        a learner those are genuinely different questions. Fingerprinting on
        the stem alone would silently swallow the second one."""
        run(write([row()]))
        run(write([row(options=["Lothal", "Surkotada", "Rakhigarhi", "Banawali"])]))
        self.assertEqual(Question.objects.count(), 2)

    def test_reordered_options_collapse_onto_one_row(self):
        """The same question printed with its options shuffled is one
        question, not two — hence the sorted option set in the fingerprint."""
        run(write([row()]))
        run(write([row(options=["Ropar", "Dholavira", "Lothal", "Kalibangan"],
                       answer_index=2)]))
        self.assertEqual(Question.objects.count(), 1)

    def test_dedup_survives_an_admin_editing_the_text(self):
        """Fingerprints are computed from live rows, not stored, precisely so
        an edit cannot orphan the dedup key. Editing a stem legitimately makes
        the row importable again — what must NOT happen is a crash or a
        duplicate of the UNEDITED row."""
        path = write([row()])
        run(path)
        q = Question.objects.get()
        q.text = "Reworded by an admin"
        q.save()
        run(path)
        self.assertEqual(Question.objects.count(), 2)
        run(path)
        self.assertEqual(Question.objects.count(), 2)


class ImportUndoTest(TestCase):

    def test_undo_removes_imported_rows(self):
        run(write([row()]))
        run(write([row()]), "--undo")
        self.assertEqual(Question.objects.count(), 0)

    def test_undo_keeps_a_question_someone_has_answered(self):
        """Deleting an answered question cascades its StudentAnswer rows and
        silently rewrites a real person's past attempt and score."""
        from accounts.models import User
        from courses.models import Course, Subject
        from quizzes.models import Quiz, QuizAttempt, StudentAnswer

        run(write([row()]))
        q = Question.objects.get()
        user = User.objects.create_user(
            username="s", email="s@example.com", password="x")
        course = Course.objects.create(title="C")
        subject = Subject.objects.create(course=course, name="S")
        quiz = Quiz.objects.create(subject=subject, title="Q")
        attempt = QuizAttempt.objects.create(quiz=quiz, student=user)
        StudentAnswer.objects.create(
            attempt=attempt, question=q, selected_choice=q.choices.first())

        out = run(write([row()]), "--undo")
        self.assertIn("1 (kept)", out)
        self.assertEqual(Question.objects.count(), 1)

    def test_undo_does_not_touch_teacher_authored_questions(self):
        """--undo is scoped to source='import' AND quiz IS NULL. A teacher's
        question on a real quiz must be untouchable by it."""
        from courses.models import Course, Subject
        from quizzes.models import Quiz

        course = Course.objects.create(title="C")
        subject = Subject.objects.create(course=course, name="S")
        quiz = Quiz.objects.create(subject=subject, title="Q")
        Question.objects.create(quiz=quiz, text="Teacher's own question")

        run(write([row()]))
        run(write([row()]), "--undo")
        self.assertEqual(Question.objects.count(), 1)
        self.assertIsNotNone(Question.objects.get().quiz_id)


class ImportReportingTest(TestCase):

    def test_every_skip_is_reported_with_a_reason(self):
        """Silent truncation is how a partial import gets mistaken for a
        complete one."""
        out = run(write([
            row(),
            row(stem="A", answer_index=None),
            row(stem="B", explanation=""),
        ]))
        self.assertIn("Read      : 3", out)
        self.assertIn("Importable: 1", out)
        self.assertIn("no answer in the source", out)
        self.assertIn("no explanation in the source", out)


class ImportedQuestionsAreReachableTest(TestCase):
    """THE SEAM between the importer and the admin screens.

    The importer lands rows as `suggested`. The pre-existing admin review
    queue deliberately excludes `quiz__isnull=True` rows, so if the NEW admin
    bank list also filtered by state, thousands of imported questions would
    be invisible everywhere and the import would look like it silently did
    nothing. These two pieces were built independently; this test is what
    stops them drifting apart.
    """

    def setUp(self):
        from accounts.models import User
        from global_settings.models import GlobalSettings
        from rest_framework.test import APIClient

        GlobalSettings.load()
        GlobalSettings.objects.filter(pk=1).update(public_quiz_hub_enabled=True)
        self.admin = User.objects.create_user(
            username="a", email="a@example.com", password="x", is_staff=True)
        self.client_ = APIClient()
        self.client_.force_authenticate(user=self.admin)
        run(write([row()]))

    def test_an_imported_question_appears_on_the_admin_bank_screen(self):
        res = self.client_.get("/api/quizzes/admin/bank/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["count"], 1)

    def test_it_is_findable_by_its_suggested_state(self):
        res = self.client_.get("/api/quizzes/admin/bank/?state=suggested")
        self.assertEqual(res.json()["count"], 1)

    def test_it_does_NOT_appear_in_the_teacher_suggestion_queue(self):
        """Deliberate. That queue is what teachers are waiting on an answer
        about; thousands of imported rows would bury it."""
        res = self.client_.get("/api/quizzes/admin/question-bank/queue/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(res.json()["results"]), 0)
        self.assertEqual(res.json()["counts"]["suggested"], 0)

    def test_it_is_not_publishable_until_an_admin_accepts_it(self):
        """The end-to-end guarantee: importing does not put a question in
        front of a learner."""
        self.assertEqual(Question.objects.publishable().count(), 0)
        q = Question.objects.get()
        q.bank_state = Question.BANK_STATE_ACCEPTED
        q.save()
        self.assertEqual(Question.objects.publishable().count(), 1)


class SplicedStemTest(TestCase):
    """A stem that swallowed the NEXT question's text.

    This is the corruption that survived the first real 3,821-row import and
    was only caught by auditing the DB afterwards. The two-column layout let
    one question's block run into the next, so the options and the answer key
    describe a different question than the stem asks about — and the row
    looks perfectly well-formed: four distinct options, an in-range answer
    index, a real explanation. Nothing in the pre-existing gates fired.

    These are hard skips with no --include-imperfect escape hatch, because
    there is no version of such a row that is safe to serve.
    """

    def test_a_stem_carrying_option_markers_is_refused(self):
        out = run(write([row(
            stem=("How many monasteries are there in Ellora caves? (a) 33 "
                  "(b) 32 (c) 34 (d) 31 Which Pallava king took the title "
                  "Vatapikonda?"),
        )]))
        self.assertEqual(Question.objects.count(), 0)
        self.assertIn("merges two questions", out)

    def test_a_stem_with_two_question_marks_is_refused(self):
        run(write([row(
            stem="Who founded the Chalukya dynasty? Which city was its capital?",
        )]))
        self.assertEqual(Question.objects.count(), 0)

    def test_a_stem_with_the_sources_numbering_mid_sentence_is_refused(self):
        run(write([row(
            stem=("Sanchi joined the World Heritage list in 1989. 47. Group "
                  "of Monuments at Pattadakal is in which state?"),
        )]))
        self.assertEqual(Question.objects.count(), 0)

    def test_include_imperfect_does_NOT_override_a_spliced_stem(self):
        """The whole point of the hard/soft split. --include-imperfect exists
        for rows that READ badly; it must never let through a row whose
        answer belongs to another question."""
        run(write([row(stem="A? B? (a) x (b) y")]), "--include-imperfect")
        self.assertEqual(Question.objects.count(), 0)

    def test_one_option_marker_alone_is_NOT_treated_as_a_splice(self):
        """A single parenthesised letter is ordinary prose — "(a) type of
        rock". Only two or more mean an option list leaked in. Guards the
        threshold against being tightened into a false positive."""
        run(write([row(stem="Which of these is (a) type of igneous rock?")]))
        self.assertEqual(Question.objects.count(), 1)

    def test_a_decimal_mid_stem_is_NOT_treated_as_numbering(self):
        """Requires a capital after the number, so "increased by 1. 5 times"
        does not read as the source's question numbering."""
        run(write([row(stem="Output increased by 1. 5 times over the decade?")]))
        self.assertEqual(Question.objects.count(), 1)


class AnswerContradictsExplanationTest(TestCase):
    """The explanation names a different option than the marked answer.

    Measured on the real corpus at ~35% genuinely wrong against a ~5% base
    rate. That is a strong signal but a clear MAJORITY of these rows are
    still correct — mostly "all of the following EXCEPT" questions, whose
    explanation properly discusses the options that do apply and never names
    the odd one out. So this warns and imports; it must not skip.
    """

    def _warned_row(self):
        return row(
            stem="Which is the biggest building at Mohenjodaro?",
            options=["Great Granary", "Assembly Hall", "Warehouse", "Citadel"],
            answer_index=0,
            explanation=("Major findings at the site include a Great bath, "
                         "an Assembly Hall and the idol of a Mother Goddess."),
        )

    def test_the_row_is_still_imported(self):
        run(write([self._warned_row()]))
        self.assertEqual(Question.objects.count(), 1)

    def test_it_carries_the_warning_in_bank_feedback(self):
        run(write([self._warned_row()]))
        q = Question.objects.get()
        self.assertIn("names a different option", q.bank_feedback)

    def test_the_command_reports_how_many_were_flagged(self):
        out = run(write([self._warned_row()]))
        self.assertIn("marked correct", out)

    def test_a_row_whose_explanation_names_the_answer_is_not_flagged(self):
        run(write([row()]))
        self.assertEqual(Question.objects.get().bank_feedback, "")

    def test_accepting_the_question_clears_its_own_warning(self):
        """_apply_bank_review blanks bank_feedback on accept, so the "check
        these" queue empties as it is curated rather than needing a second
        cleanup pass."""
        run(write([self._warned_row()]))
        q = Question.objects.get()
        self.assertTrue(q.bank_feedback)
        from quizzes.views import _apply_bank_review
        from accounts.models import User
        admin = User.objects.create_user(
            username="rev", email="rev@example.com", password="x", is_staff=True)
        _apply_bank_review(q, action="accept", feedback="", admin=admin)
        q.refresh_from_db()
        self.assertEqual(q.bank_feedback, "")

    def test_a_short_option_appearing_by_coincidence_does_not_flag(self):
        """"Red" turns up in prose constantly. Below four characters the
        substring test means nothing, so it is not applied."""
        run(write([row(
            stem="Which colour has the longest wavelength?",
            options=["Red", "Blue", "Green", "Violet"],
            answer_index=0,
            explanation="Blue light scatters most in the atmosphere.",
        )]))
        self.assertEqual(Question.objects.get().bank_feedback, "")


class UnusableTopicLabelTest(TestCase):
    """A mangled section heading becomes a public filter chip.

    All three topic tags the real SSC import produced were wreckage — a whole
    spliced question used as a label. Unlike a damaged question there is
    nothing for a human to salvage in a heading, and one bad row is visible to
    every visitor, so this REFUSES the label and keeps the question.
    """

    def _tag_labels(self, kind):
        return set(QuestionTag.objects.filter(kind=kind)
                   .values_list("label", flat=True))

    def test_a_topic_carrying_option_markers_is_dropped(self):
        run(write([row(topic=("Prehistoric Period pottery was discovered? "
                              "Neolithic (b) Chalcolithic (c) Palaeolithic"))]))
        self.assertEqual(self._tag_labels(QuestionTag.KIND_TOPIC), set())

    def test_a_topic_with_the_sources_own_numbering_is_dropped(self):
        run(write([row(topic="1. ____ Art & Culture")]))
        self.assertEqual(self._tag_labels(QuestionTag.KIND_TOPIC), set())

    def test_an_overlong_topic_is_dropped(self):
        run(write([row(topic="Unit / Measurement / Measuring Instrument / "
                             "Physical Quantities Unit and more besides")]))
        self.assertEqual(self._tag_labels(QuestionTag.KIND_TOPIC), set())

    def test_the_question_survives_a_dropped_topic(self):
        """The topic is optional; subject and exam are what the rails use."""
        run(write([row(topic="1. ____ Art & Culture")]))
        q = Question.objects.get()
        self.assertEqual(
            {t.kind for t in q.tags.all()},
            {QuestionTag.KIND_SUBJECT, QuestionTag.KIND_EXAM},
        )

    def test_a_clean_topic_is_still_kept(self):
        run(write([row(topic="Indus Valley")]))
        self.assertEqual(self._tag_labels(QuestionTag.KIND_TOPIC),
                         {"Indus Valley"})


class SplicedExplanationTest(TestCase):
    """The two-column splice landing in the EXPLANATION.

    Found by browser-driving the admin bank screen, not by any audit: a row
    about the Lalit Kala Akademi rendered a correctly-keyed answer (1954)
    under prose about Assamese culture. The stem and option gates were clean,
    because the damage was in a third field nobody had checked.

    142 of the 3,793 imported rows trip this; 123 were not already caught by
    the contradiction check.
    """

    def test_an_explanation_starting_mid_word_is_flagged(self):
        run(write([row(explanation=(
            "samese traditions and values of Assam; hence, it is a symbol "
            "of unity and pride in culture."))]))
        self.assertIn("spliced", Question.objects.get().bank_feedback)

    def test_an_explanation_carrying_the_next_questions_options_is_flagged(self):
        run(write([row(explanation=(
            "The Chalukya dynasty. Who succeeded him? (a) PulakesinI "
            "(b) Kirtivarman (c) Narasimhavarman (d) Mangalesa"))]))
        self.assertIn("spliced", Question.objects.get().bank_feedback)

    def test_the_row_is_still_imported_and_keeps_its_answer(self):
        """The marked answer is usually fine — dropping the row would throw
        away a good question over bad prose."""
        run(write([row(explanation="samese traditions and values of Assam.")]))
        q = Question.objects.get()
        self.assertEqual(q.choices.filter(is_correct=True).count(), 1)
        self.assertEqual(q.choices.get(is_correct=True).text, "Lothal")

    def test_a_clean_explanation_is_not_flagged(self):
        run(write([row()]))
        self.assertEqual(Question.objects.get().bank_feedback, "")

    def test_the_command_reports_how_many_look_spliced(self):
        out = run(write([row(explanation="samese traditions of Assam.")]))
        self.assertIn("SPLICED", out)

    def test_both_warnings_can_land_on_one_row(self):
        """A row can be both contradicted and spliced; it is one queue, so
        the notes are joined rather than one silently winning."""
        run(write([row(
            stem="Which is the biggest building at Mohenjodaro?",
            options=["Great Granary", "Assembly Hall", "Warehouse", "Citadel"],
            answer_index=0,
            explanation=("major findings include a Great bath and an "
                         "Assembly Hall at the site."),
        )]))
        feedback = Question.objects.get().bank_feedback
        self.assertIn("names a different option", feedback)
        self.assertIn("spliced", feedback)
