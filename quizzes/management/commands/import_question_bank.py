"""Import parsed previous-year questions into the standalone question bank.

    manage.py import_question_bank <file.json> --dry-run
    manage.py import_question_bank <file.json>
    manage.py import_question_bank <file.json> --include-imperfect
    manage.py import_question_bank <file.json> --undo

Input is the normalised JSON a parser produces (see
design_handoff_public_quiz_hub/HANDOFF.md for the shape and for the SSC
corpus this was built against).

WHY THIS IS A COMMAND AND NOT AN UPLOAD ENDPOINT
------------------------------------------------
Extraction from real exam papers is iterative and the failure mode is
silent — a parser bug produces rows that look fine and carry a WRONG
answer. That has to be re-runnable, diffable and reviewable by a person
with a terminal, not fired once through a browser with no way to inspect
what happened. An admin-facing upload can be layered on later; the
trustworthy path comes first.

THE RULES THIS COMMAND ENFORCES
-------------------------------
1. **Nothing is imported as ``accepted``.** Every row lands as
   ``suggested`` and an admin promotes it. The bank's whole value is that
   a learner can trust the answer, and a parser cannot confer that.
2. **A question with no answer or no explanation is not imported by
   default.** The public hub's entire promise is an explanation after
   every question; a row without one cannot fulfil it, and a row without
   an answer cannot be graded at all.
3. **Idempotent.** Re-running never duplicates. The key is the
   normalised stem plus its option set, because the same question
   genuinely recurs across papers and across shards of one parse.
4. **Reports what it skipped, by reason.** Silent truncation is how a
   partial import gets mistaken for a complete one.
"""
import hashlib
import json
import re
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from quizzes.models import Choice, Question, QuestionTag

# Rows carrying any of these are held back unless --include-imperfect.
# They are not parser bugs — they are places the SOURCE DOCUMENT was
# messy — but they read badly to a learner, so they wait for a human.
QUALITY_FLAGS = (
    "no_answer",
    "no_explanation",
    "stem_too_short",
    "stem_starts_mid_sentence",
    "explanation_looks_like_a_table",
)

# Leading "1193. " / "42) " from the source's own numbering. Safe to strip:
# it is document furniture, never part of the question.
LEADING_NUMBER = re.compile(r"^\s*\d{1,4}\s*[.)]\s+")
# Collapsed whitespace and the parser's fill-in-blank placeholder runs.
MULTI_SPACE = re.compile(r"[ \t]{2,}")

# ── structural corruption in the STEM ────────────────────────────────────
# These three are the signature of a MISSED BLOCK BOUNDARY: the two-column
# layout let the next question's text run into this one's stem, so the
# options and the answer key belong to a DIFFERENT question than the stem
# asks about. Found by auditing a real 3,821-row import — e.g. the stem
#
#   "How many monasteries and temples are there in Ellora caves?
#    (a) 33 (b) 32 (c) 34 (d) 31 Which of the following Pallava kings…"
#
# carried the marked answer "Narsingh Varman I", which answers the SECOND
# question. A row like that is unusable whatever its answer index says, and
# it is not a judgement call — it is structurally detectable, so it is a
# hard skip rather than a soft flag.
#
# ⚠ These check the STEM. An earlier audit only checked option text for
# "(a)"-style markers, found zero, and read that as "no merged questions" —
# the markers were in the stem the whole time.
STEM_OPTION_MARKER = re.compile(r"\(\s*[abcd]\s*\)", re.I)
# Stamped into Question.bank_feedback for rows whose explanation disagrees
# with the marked answer. A FIXED string on purpose: the admin bank screen
# filters on it to build the "check these first" queue, and _apply_bank_review
# blanks bank_feedback on accept, so curating a row clears its own warning.
IMPORT_WARNING = (
    "Imported: the source explanation names a different option than the one "
    "marked correct. Verify the answer against the explanation before "
    "accepting this question."
)
# The source's own numbering ("… 47. Which of …") surviving mid-stem means
# the block splitter ran past a boundary. Requires a capital after it so an
# ordinary "… by 1. 5 times" style decimal does not trip it.
STEM_MID_NUMBERING = re.compile(r"\S\s+\d{1,4}\s*[.)]\s+[A-Z]")


def clean_stem(text):
    text = LEADING_NUMBER.sub("", text or "")
    text = MULTI_SPACE.sub(" ", text)
    return text.strip()


def clean_explanation(text):
    text = MULTI_SPACE.sub(" ", text or "")
    # The parser emits ____ where the source had a tab-leader gap. In prose
    # it is noise, not meaning.
    text = text.replace("____", " ").strip()
    return MULTI_SPACE.sub(" ", text).strip()


def fingerprint(stem, options):
    """Identity for de-duplication.

    Stem alone is not enough: the same stem legitimately appears with
    different option sets across papers, and those are different questions
    to a learner. Options alone is far too weak. Both, normalised for case
    and whitespace, and the options SORTED so a reshuffled printing of the
    same question collapses onto one row.
    """
    norm = lambda s: re.sub(r"\W+", "", (s or "").lower())
    parts = [norm(stem)] + sorted(norm(o) for o in options)
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def quality_flags(row):
    flags = []
    stem = clean_stem(row.get("stem", ""))
    expl = clean_explanation(row.get("explanation", ""))
    if row.get("answer_index") is None:
        flags.append("no_answer")
    if not expl:
        flags.append("no_explanation")
    if len(stem) < 15:
        flags.append("stem_too_short")
    # A stem that opens lower-case or with a dangling verb lost its opening
    # words to a layout break. The answer is usually still right, but the
    # question reads as broken.
    if stem and (stem[0].islower() or stem.lower().startswith("was ")):
        flags.append("stem_starts_mid_sentence")
    words = expl.split()
    if words and sum(1 for w in words if w.istitle()) / len(words) > 0.55:
        # Explanations that are mostly Title Case are dumped reference
        # TABLES, not prose — a wall of place names with no sentence.
        flags.append("explanation_looks_like_a_table")
    return flags


def structural_defects(stem):
    """Hard, non-judgemental reasons a stem cannot be trusted at all.

    Distinct from quality_flags(): those describe a row that reads badly but
    whose answer is probably still right, so --include-imperfect can let
    them through. These describe a row whose answer key belongs to a
    DIFFERENT question. There is no flag to override them, because there is
    no reading of such a row that is safe to show a learner.
    """
    defects = []
    if len(STEM_OPTION_MARKER.findall(stem)) >= 2:
        defects.append("stem merges two questions (carries option markers)")
    if stem.count("?") >= 2:
        defects.append("stem merges two questions (two question marks)")
    if STEM_MID_NUMBERING.search(stem):
        defects.append("stem ran past its block boundary (numbering mid-stem)")
    return defects


def _words(text):
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def answer_contradicts_explanation(stem, options, answer, explanation):
    """True when the explanation names a WRONG option but not the marked one.

    This is a WARNING, never a skip, and the distinction matters. Measured
    on the real corpus: rows tripping this are ~35% factually wrong against
    a ~5% base rate — a 7x enrichment, so it is a genuinely useful signal —
    but the majority of them are still CORRECT. The common false positive is
    the "all of the following EXCEPT" question, whose explanation quite
    properly discusses the options that DO apply and never mentions the odd
    one out, which is the answer.

    So a row like this is imported, left `suggested` (i.e. invisible to
    learners — Question.objects.publishable() requires `accepted`), and
    stamped into `bank_feedback` so the human curating the bank is told
    which rows to look at hardest. Deciding it automatically in either
    direction would be wrong: skipping discards ~65% good questions,
    accepting ships wrong answers to exam candidates.
    """
    expl = _words(explanation)
    if not expl.strip():
        return False

    def testable(option):
        # Short option text ("Red", "1875") appears in prose by coincidence
        # far too often for its presence OR absence to mean anything.
        return len(_words(option).strip()) >= 4

    def named(option):
        return testable(option) and _words(option).strip() in expl

    correct = options[answer]
    # If the marked answer is itself too short to look for, "the explanation
    # does not mention it" is not evidence of anything — and flagging on a
    # long WRONG option alone would fire on every "Red / Blue / Green"
    # question whose explanation happens to discuss another colour. No
    # testable answer, no verdict.
    if not testable(correct):
        return False
    wrong_named = [o for i, o in enumerate(options) if i != answer and named(o)]
    return bool(wrong_named) and not named(correct)


class Command(BaseCommand):
    help = "Import parsed previous-year questions into the standalone question bank."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the normalised JSON.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report only. Writes nothing.")
        parser.add_argument(
            "--include-imperfect", action="store_true",
            help="Also import rows flagged for messy source text. Never "
                 "imports a row with no answer or no explanation.")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument(
            "--undo", action="store_true",
            help="Delete every imported standalone question that no learner "
                 "has answered.")

    def _existing_fingerprints(self):
        """Fingerprint every standalone question already in the bank.

        Computed from live data rather than stored in a column, deliberately.
        A stored hash is a second source of truth that goes stale the moment
        someone edits a question's text in the admin — and then a re-import
        silently creates the duplicate the column existed to prevent. This
        costs one extra query per run, which is nothing next to that.

        Two queries total, not one per row: the stems, and every choice keyed
        by question, joined in Python.
        """
        stems = dict(
            Question.objects.filter(quiz__isnull=True).values_list("id", "text")
        )
        if not stems:
            return set()
        options = {}
        for qid, text in Choice.objects.filter(
            question_id__in=stems
        ).values_list("question_id", "text"):
            options.setdefault(qid, []).append(text)
        return {
            fingerprint(stem, options.get(qid, []))
            for qid, stem in stems.items()
        }

    # ── tags ────────────────────────────────────────────────────────────
    def _tag(self, kind, label, cache):
        """get_or_create a tag, cached per run.

        Keyed on (kind, slug) to match the model's unique constraint —
        "General Knowledge" and "general knowledge" must not become two
        rails on the public page.
        """
        label = (label or "").strip()
        if not label:
            return None
        key = (kind, slugify(label))
        if key in cache:
            return cache[key]
        tag, _ = QuestionTag.objects.get_or_create(
            kind=kind, slug=slugify(label), defaults={"label": label},
        )
        cache[key] = tag
        return tag

    def handle(self, *args, **opts):
        if opts["undo"]:
            return self._undo(opts["dry_run"])

        try:
            with open(opts["path"]) as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            raise CommandError(f"Could not read {opts['path']}: {exc}")

        rows = payload.get("questions") or []
        if opts["limit"]:
            rows = rows[: opts["limit"]]
        if not rows:
            raise CommandError("No questions in that file.")

        skipped = Counter()
        warned = 0
        seen_this_run = set()
        existing = self._existing_fingerprints()
        to_create = []

        for row in rows:
            stem = clean_stem(row.get("stem", ""))
            options = [(o or "").strip() for o in (row.get("options") or [])]
            answer = row.get("answer_index")
            flags = quality_flags(row)

            # Hard gates — these are never importable, whatever the flags say.
            if "no_answer" in flags:
                skipped["no answer in the source"] += 1
                continue
            if "no_explanation" in flags:
                skipped["no explanation in the source"] += 1
                continue
            if len(options) < 2:
                skipped["fewer than two options"] += 1
                continue
            if len(set(o.lower() for o in options)) != len(options):
                # Two identical options means the answer index is ambiguous —
                # this is exactly the corruption signature a bad parse leaves.
                skipped["duplicate option text (answer ambiguous)"] += 1
                continue
            if not (0 <= answer < len(options)):
                skipped["answer index out of range"] += 1
                continue

            # Not overridable by --include-imperfect — see structural_defects().
            defects = structural_defects(stem)
            if defects:
                skipped[defects[0]] += 1
                continue

            soft = [f for f in flags if f not in ("no_answer", "no_explanation")]
            if soft and not opts["include_imperfect"]:
                skipped[f"held back: {', '.join(soft)}"] += 1
                continue

            fp = fingerprint(stem, options)
            if fp in existing or fp in seen_this_run:
                skipped["already in the bank (duplicate)"] += 1
                continue
            seen_this_run.add(fp)
            contradicts = answer_contradicts_explanation(
                stem, options, answer, clean_explanation(row.get("explanation")))
            if contradicts:
                warned += 1
            to_create.append((row, stem, options, answer, fp, contradicts))

        self.stdout.write(f"Read      : {len(rows)} rows")
        self.stdout.write(f"Importable: {len(to_create)}")
        self.stdout.write(f"Skipped   : {sum(skipped.values())}")
        for reason, n in skipped.most_common():
            self.stdout.write(f"    {n:6}  {reason}")
        if warned:
            self.stdout.write(self.style.WARNING(
                f"\n{warned} of the importable rows have an explanation that "
                "names a\nDIFFERENT option than the one marked correct. They "
                "are imported, but\nflagged in bank_feedback and must be read "
                "before being accepted —\nabout a third of them are genuinely "
                "wrong. Filter the bank screen\nby that flag and curate those "
                "first."))

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing was written."))
            return

        created = 0
        cache = {}
        # One transaction for the whole import so a failure half way through
        # cannot leave a partially-tagged bank behind.
        with transaction.atomic():
            for row, stem, options, answer, fp, contradicts in to_create:
                q = Question(
                    quiz=None,
                    text=stem,
                    explanation=clean_explanation(row.get("explanation")),
                    difficulty=row.get("difficulty") or Question.DIFFICULTY_MEDIUM,
                    topic=(row.get("topic") or "")[:120],
                    year=(row.get("years") or [None])[0],
                    source=Question.SOURCE_IMPORT,
                    question_type=Question.TYPE_SINGLE,
                    # Reuses the review-note field the admin screens already
                    # render, rather than adding a column — see
                    # answer_contradicts_explanation() for why this is a note
                    # for a human and not a decision.
                    bank_feedback=(IMPORT_WARNING if contradicts else ""),
                )
                # save(), never bulk_create: Question.save() carries the bank
                # invariant that normalises bank_state, and bulk_create
                # bypasses save() entirely. Slower, correct.
                q.save()
                Choice.objects.bulk_create([
                    Choice(question=q, text=text, is_correct=(i == answer))
                    for i, text in enumerate(options)
                ])
                tags = []
                subj = self._tag(QuestionTag.KIND_SUBJECT, row.get("subject"), cache)
                if subj:
                    tags.append(subj)
                for exam in row.get("exam_names") or []:
                    t = self._tag(QuestionTag.KIND_EXAM, exam, cache)
                    if t:
                        tags.append(t)
                if row.get("topic"):
                    t = self._tag(QuestionTag.KIND_TOPIC, row["topic"], cache)
                    if t:
                        tags.append(t)
                if tags:
                    q.tags.set(tags)
                created += 1

        self.stdout.write(self.style.SUCCESS(f"\nCreated {created} bank questions."))
        self.stdout.write(
            "All are bank_state='suggested' and quiz=None. They are NOT live: "
            "an admin promotes them from the question-bank screen.")

    def _undo(self, dry_run):
        """Remove imported rows, but never one a learner has already answered.

        Deleting an answered question would cascade StudentAnswer rows and
        silently rewrite someone's past attempt and score.
        """
        qs = Question.objects.filter(
            quiz__isnull=True, source=Question.SOURCE_IMPORT)
        total = qs.count()
        answered = qs.filter(studentanswer__isnull=False).distinct().count()
        practiced = qs.filter(practice_answers__isnull=False).distinct().count()
        deletable = qs.exclude(studentanswer__isnull=False).exclude(
            practice_answers__isnull=False)
        n = deletable.count()
        self.stdout.write(f"Imported rows      : {total}")
        self.stdout.write(f"Answered by someone: {answered} (kept)")
        self.stdout.write(f"Used in practice   : {practiced} (kept)")
        self.stdout.write(f"Deletable          : {n}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing deleted."))
            return
        deletable.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {n}."))
