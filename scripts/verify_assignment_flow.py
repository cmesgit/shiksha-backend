"""The assignment round trip, over real HTTP, both sides.

scripts/verify_flows.py covers materials, quizzes and recordings but stops
short of assignments, which is the only flow here with four legs and two
actors:

    teacher creates (draft) -> teacher publishes -> student sees it
    -> student submits -> teacher sees the submission -> teacher grades
    -> student sees the grade and the feedback

Each leg is checked from the side that is supposed to see it, because every
one of them is separately scoped and a break in the middle looks like
"nothing there" rather than an error.

SETUP — same as verify_flows.py:
    python manage.py migrate        --settings=config.settings_test
    python manage.py seed_demo_data --settings=config.settings_test
    python manage.py runserver 127.0.0.1:8001 --settings=config.settings_test
    python scripts/verify_assignment_flow.py

⚠ BATCH SCOPING. A quiz or assignment scoped to batch B is invisible to a
student in batch A — correctly. This script resolves the student's OWN batch
and targets that, rather than grabbing whichever batch the teacher happens to
list first. verify_flows.py takes the last batch it sees, which is why adding
a second batch to the demo course makes four of its checks fail: the content
lands on a batch the demo student is not in. That is the product working, not
breaking.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.verify_flows import (  # noqa: E402
    BASE, PW, RESULTS, Client, as_teacher, check, login, record,
)

A = "ASSIGNMENTS"

STUDENT = "demo.student@shiksha.test"
TEACHER = "demo.faculty@shiksha.test"


def as_student(email=STUDENT):
    """Log in and land on a learner context, selecting a profile if asked."""
    c, body = login(email)
    if body.get("context") == "learner":
        return c, body
    for profile in body.get("profiles") or []:
        r = c.post("/accounts/profiles/select/", json={"profile_id": profile["id"]})
        if r.status_code == 200:
            return c, r.json()
    raise SystemExit(f"could not reach a learner context for {email}: {body}")


def student_subject(s):
    """A subject the STUDENT can actually see.

    /courses/subjects/mine/ is the learner view and returns only {id, name} —
    no course, no batch. The batch has to come from the teacher's roster
    instead; see resolve_student_batch.
    """
    r = s.get("/courses/subjects/mine/")
    if not r.ok or not r.json():
        raise SystemExit(f"student has no subjects: {r.status_code} {r.text[:200]}")
    rows = r.json()
    first = rows[0] if isinstance(rows, list) else (rows.get("results") or [])[0]
    return first["id"]


def resolve_student_batch(t, subject_id, student_email):
    """The batch id THIS student is in, via the teacher's own roster.

    Deliberately not "whichever batch the teacher lists first". Content scoped
    to a batch the student is not in is invisible to them — correctly — so a
    round-trip test that guesses the batch reports a broken platform when the
    platform is fine. verify_flows.py takes the last batch it sees and fails
    exactly this way once a course has more than one.

    The roster gives each student a `batch_code`; the batches endpoint maps
    that code to an id.
    """
    roster = t.get(f"/courses/subjects/{subject_id}/students/")
    code = None
    if roster.ok:
        for row in roster.json().get("students", []):
            if row.get("email") == student_email:
                code = row.get("batch_code")
                break
    if not code:
        return None, None

    batches = t.get(f"/courses/subjects/{subject_id}/batches/")
    if batches.ok:
        for b in batches.json():
            if b.get("code") == code:
                return b["id"], code
    return None, code


def main():
    t = as_teacher(TEACHER)
    s, _ = as_student()

    subject_id = student_subject(s)
    check(A, "student has a subject to work in", bool(subject_id), str(subject_id))

    batch_id, batch_code = resolve_student_batch(t, subject_id, STUDENT)
    check(A, "teacher's roster shows the student and their batch",
          bool(batch_id), f"{batch_code} -> {batch_id}")

    # ── 1. teacher creates it as a DRAFT ────────────────────────────
    import uuid as _uuid

    title = f"Round-trip assignment {_uuid.uuid4().hex[:6]}"
    payload = {
        "subject_id": subject_id,
        "title": title,
        "description": "Created by verify_assignment_flow.py",
        "due_date": "2027-01-01T00:00:00Z",
        "max_marks": 20,
        "is_published": False,
    }
    if batch_id:
        payload["batch_id"] = batch_id

    created = t.post("/assignments/teacher/create/", json=payload)
    ok = check(A, "teacher creates a draft assignment",
               created.status_code in (200, 201),
               f"HTTP {created.status_code} {created.text[:180]}")
    if not ok:
        return summarise()
    assignment_id = created.json().get("id")

    # ── 2. a DRAFT must be invisible to the student ─────────────────
    listed = s.get(f"/assignments/subject/{subject_id}/")
    titles = [a.get("title") for a in _rows(listed)]
    check(A, "an UNPUBLISHED assignment is hidden from the student",
          listed.ok and title not in titles, f"{len(titles)} visible")

    # ── 3. publish it ───────────────────────────────────────────────
    pub = t.patch(f"/assignments/teacher/{assignment_id}/edit/",
                  json={"is_published": True})
    check(A, "teacher publishes it", pub.status_code in (200, 202),
          f"HTTP {pub.status_code} {pub.text[:180]}")

    listed = s.get(f"/assignments/subject/{subject_id}/")
    titles = [a.get("title") for a in _rows(listed)]
    check(A, "the PUBLISHED assignment reaches the student",
          title in titles, f"{len(titles)} visible")

    # ── 4. student submits ──────────────────────────────────────────
    # The field is "file" — SubmitAssignmentView reads request.FILES["file"],
    # not the model's own `submitted_file` column name.
    sub = s.post(
        f"/assignments/{assignment_id}/submit/",
        files={"file": ("answer.txt", b"My answer.", "text/plain")},
    )
    check(A, "student submits", sub.status_code in (200, 201),
          f"HTTP {sub.status_code} {sub.text[:180]}")

    # ── 5. teacher sees the submission ──────────────────────────────
    subs = t.get(f"/assignments/teacher/{assignment_id}/submissions/")
    rows = _rows(subs)
    check(A, "the submission reaches the teacher", subs.ok and len(rows) >= 1,
          f"HTTP {subs.status_code}, {len(rows)} submission(s)")
    if not rows:
        return summarise()
    submission_id = rows[0].get("id")

    # ── 6. teacher grades it ────────────────────────────────────────
    graded = t.post(
        f"/assignments/teacher/submissions/{submission_id}/grade/",
        json={"marks_obtained": 17, "feedback": "Good work — check question 3."},
    )
    check(A, "teacher grades the submission",
          graded.status_code in (200, 201),
          f"HTTP {graded.status_code} {graded.text[:180]}")

    # ── 7. the grade gets back to the student ───────────────────────
    detail = s.get(f"/assignments/{assignment_id}/")
    body = detail.json() if detail.ok else {}
    blob = str(body)
    check(A, "the student can read the assignment back", detail.ok,
          f"HTTP {detail.status_code}")
    check(A, "the MARK reaches the student", "17" in blob, _excerpt(body, "17"))
    check(A, "the FEEDBACK reaches the student",
          "check question 3" in blob.lower(),
          _excerpt(body, "feedback"))

    # ── 8. a student must not be able to grade ──────────────────────
    hijack = s.post(
        f"/assignments/teacher/submissions/{submission_id}/grade/",
        json={"marks_obtained": 20, "feedback": "A+"},
    )
    check(A, "a student CANNOT grade a submission",
          hijack.status_code in (401, 403, 404),
          f"HTTP {hijack.status_code}")

    # ── cleanup ─────────────────────────────────────────────────────
    t.delete(f"/assignments/teacher/{assignment_id}/delete/")
    return summarise()


def _rows(response):
    if not response.ok:
        return []
    body = response.json()
    if isinstance(body, list):
        return body
    return body.get("results") or body.get("submissions") or []


def _excerpt(body, needle):
    """A short, readable slice of the payload around what we looked for."""
    text = str(body)
    idx = text.lower().find(str(needle).lower())
    if idx < 0:
        return text[:120]
    return "…" + text[max(0, idx - 50):idx + 70] + "…"


def summarise():
    passed = sum(1 for *_, ok, _ in ((r[0], r[1], r[2], r[3]) for r in RESULTS) if ok)
    total = len(RESULTS)
    print("=" * 74)
    print(f"{passed}/{total} passed")
    failures = [r for r in RESULTS if not r[2]]
    if failures:
        print("\nFAILURES:")
        for area, name, _, detail in failures:
            print(f"  · {area}: {name} — {detail}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
