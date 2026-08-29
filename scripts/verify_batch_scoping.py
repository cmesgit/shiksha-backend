"""Batch isolation + chapter tagging, verified against a real running server.

Answers two production questions:

  1. If a teacher uploads to Batch A, does ONLY Batch A see it — across study
     materials, quizzes and recordings, on the list AND on the per-id
     endpoints (a student who has the UUID must be refused too, not merely
     have the row hidden from their list).
  2. When tagging content, can a teacher use an official course chapter, their
     OWN custom label, and a "no specific chapter" default — and can a custom
     one be promoted into the course's real chapter list?

SETUP — everything in scripts/verify_flows.py's docstring, plus a second
batch and a student inside it:

    python manage.py shell --settings=config.settings_test -c "
    from accounts.models import User, LearnerProfile, Role, UserRole
    from courses.models import Course, Subject, Batch
    from enrollments.models import Enrollment
    course = Course.objects.get(title__startswith='Class 11 Science')
    bB,_ = Batch.objects.get_or_create(course=course, code='DEMO-B1',
                                       defaults={'name':'Demo Batch B1'})
    srole,_ = Role.objects.get_or_create(name='STUDENT')
    u,_ = User.objects.get_or_create(email='demo.student.b@shiksha.test',
                                     defaults={'username':'demo.student.b'})
    u.set_password('ShikshaDemo@2026'); u.is_verified=True; u.save()
    UserRole.objects.get_or_create(user=u, role=srole)
    p,_ = LearnerProfile.objects.get_or_create(account=u, defaults={
        'display_name':'Bhavna','full_name':'Bhavna','is_default':True})
    Enrollment.objects.update_or_create(user=u, learner_profile=p,
        course=course, defaults={'batch':bB,'status':Enrollment.STATUS_ACTIVE})"

Then:  python scripts/verify_batch_scoping.py
"""
import io
import sys
import uuid

sys.path.insert(0, __import__("os").path.dirname(__file__))
from verify_flows import (  # noqa: E402
    BASE, PW, Client, RESULTS, as_learner, as_teacher, check, record,
    teacher_scope,
)

STUDENT_A = "demo.student@shiksha.test"     # Demo Batch A1
STUDENT_B = "demo.student.b@shiksha.test"   # Demo Batch B1


def batches(t):
    b = t.get("/courses/teacher/my-batches/")
    out = []
    for g in b.json().get("groups", []):
        out.extend(g.get("batches", []))
    return out


def rows(resp):
    if not resp.ok:
        return []
    body = resp.json()
    return body.get("results", body) if isinstance(body, dict) else body


# ===========================================================================
def verify_material_batch_scoping(t, sid, bA, bB, sa, sb):
    A = "MATERIALS/BATCH"
    up = t.post("/materials/files/upload/",
                files={"file": ("batch-a-note.pdf",
                                io.BytesIO(b"%PDF-1.4\nbatch A only\n"),
                                "application/pdf")})
    if not check(A, "upload a file", up.ok, f"HTTP {up.status_code}"):
        return
    made = t.post("/materials/materials/upload/", data={
        "title": "Batch A ONLY material", "subject_id": sid,
        "file_ids": up.json()["id"], "batch_id": bA,
        "no_specific_chapter": "true",
    })
    if not check(A, "upload a material scoped to Batch A", made.ok,
                 f"HTTP {made.status_code} {made.text[:180]}"):
        return
    mid = made.json()["id"]

    a_list = rows(sa.get(f"/materials/student/subjects/{sid}/materials/"))
    b_list = rows(sb.get(f"/materials/student/subjects/{sid}/materials/"))
    check(A, "Batch A student SEES it in their list",
          any(m.get("id") == mid for m in a_list), f"{len(a_list)} rows")
    check(A, "Batch B student does NOT see it in their list",
          not any(m.get("id") == mid for m in b_list), f"{len(b_list)} rows")

    check(A, "Batch A student can open it",
          sa.get(f"/materials/materials/{mid}/").status_code == 200)
    db = sb.get(f"/materials/materials/{mid}/")
    check(A, "Batch B student is REFUSED it by UUID",
          db.status_code in (403, 404), f"HTTP {db.status_code}")

    # course-wide (batch omitted) must reach everyone
    up2 = t.post("/materials/files/upload/",
                 files={"file": ("all.pdf", io.BytesIO(b"%PDF-1.4\nall\n"),
                                 "application/pdf")})
    made2 = t.post("/materials/materials/upload/", data={
        "title": "Course-wide material", "subject_id": sid,
        "file_ids": up2.json()["id"], "no_specific_chapter": "true",
    })
    if made2.ok:
        m2 = made2.json()["id"]
        b2 = rows(sb.get(f"/materials/student/subjects/{sid}/materials/"))
        check(A, "a COURSE-WIDE material reaches Batch B too",
              any(m.get("id") == m2 for m in b2))
        t.delete(f"/materials/materials/{m2}/delete/")

    # a batch from another course must be refused
    other = t.post("/materials/materials/upload/", data={
        "title": "Foreign batch", "subject_id": sid,
        "file_ids": up.json()["id"],
        "batch_id": "00000000-0000-0000-0000-000000000000",
        "no_specific_chapter": "true",
    })
    check(A, "a non-existent/foreign batch is refused",
          other.status_code in (400, 404), f"HTTP {other.status_code}")

    t.delete(f"/materials/materials/{mid}/delete/")


# ===========================================================================
def verify_quiz_batch_scoping(t, sid, bA, bB, sa, sb):
    A = "QUIZZES/BATCH"
    made = t.post("/teacher/quizzes/", json={
        "subject": sid, "batch_id": bA, "title": "Batch A ONLY quiz",
        "quiz_type": "mock", "time_limit_minutes": 5,
        "no_specific_chapter": True,
    })
    if not check(A, "create a quiz scoped to Batch A", made.ok,
                 f"HTTP {made.status_code} {made.text[:180]}"):
        return
    qid = made.json()["id"]
    t.req("PUT", f"/teacher/quizzes/{qid}/questions/bulk/", json={
        "questions": [{"text": "Batch A question?", "marks": 1, "order": 1,
                       "explanation": "e",
                       "choices": [{"text": "yes", "is_correct": True},
                                   {"text": "no", "is_correct": False}]}]})
    t.patch(f"/teacher/quizzes/{qid}/assign/",
            json={"assign": True, "batch_ids": [bA]})

    a_ids = {q.get("id") for q in rows(sa.get("/student/quizzes/"))}
    b_ids = {q.get("id") for q in rows(sb.get("/student/quizzes/"))}
    check(A, "Batch A student SEES it in their list", qid in a_ids)
    check(A, "Batch B student does NOT see it in their list", qid not in b_ids,
          f"{len(b_ids)} visible")

    # The per-id endpoints are the ones that were flagged as unguarded.
    d = sb.get(f"/quizzes/{qid}/")
    check(A, "Batch B student is REFUSED the quiz DETAIL by UUID",
          d.status_code in (403, 404), f"HTTP {d.status_code}")
    s = sb.post(f"/quizzes/{qid}/start/", json={})
    started = s.status_code in (200, 201)
    check(A, "Batch B student CANNOT START it by UUID",
          not started, f"HTTP {s.status_code}")

    if started:
        det = sb.get(f"/quizzes/{qid}/")
        qs = det.json().get("questions", []) if det.ok else []
        if qs:
            sub = sb.post(f"/student/quizzes/{qid}/submit/", json={
                "answers": [{"question": qs[0]["id"],
                             "selected_choice": qs[0]["choices"][0]["id"]}]})
            check(A, "Batch B student CANNOT SUBMIT it either",
                  sub.status_code not in (200, 201), f"HTTP {sub.status_code}")

    # widening to course-wide must reach B
    t.patch(f"/teacher/quizzes/{qid}/assign/",
            json={"assign": True, "batch_ids": []})
    b_ids2 = {q.get("id") for q in rows(sb.get("/student/quizzes/"))}
    check(A, "an EMPTY batch list widens the quiz to every batch",
          qid in b_ids2, "empty batches = course-wide, not nobody")

    t.delete(f"/teacher/quizzes/{qid}/delete/?force=true")


# ===========================================================================
def verify_recording_batch_scoping(t, sid, bA, bB, sa, sb):
    A = "RECORDINGS/BATCH"
    made = t.post(f"/courses/subjects/{sid}/recordings/save/", json={
        "title": "Batch A ONLY recording",
        "video_id": f"verify-{uuid.uuid4().hex[:8]}",
        "session_date": "2026-08-02", "batch_id": bA,
    })
    if not check(A, "save a recording scoped to Batch A", made.ok,
                 f"HTTP {made.status_code} {made.text[:180]}"):
        return
    rid = made.json()["id"]

    a_ids = {r.get("id") for r in rows(sa.get(f"/courses/subjects/{sid}/recordings/"))}
    b_ids = {r.get("id") for r in rows(sb.get(f"/courses/subjects/{sid}/recordings/"))}
    check(A, "Batch A student SEES it in their list", rid in a_ids)
    check(A, "Batch B student does NOT see it in their list", rid not in b_ids,
          f"{len(b_ids)} rows")

    db = sb.get(f"/courses/recordings/{rid}/")
    check(A, "Batch B student is REFUSED it by UUID",
          db.status_code == 403, f"HTTP {db.status_code}")
    pb = sb.get(f"/courses/recordings/{rid}/playback/")
    check(A, "Batch B student is REFUSED its PLAYBACK url",
          pb.status_code == 403, f"HTTP {pb.status_code}")
    pr = sb.post(f"/courses/recordings/{rid}/progress/save/",
                 json={"last_position": 5})
    check(A, "Batch B student cannot write progress against it",
          pr.status_code == 403, f"HTTP {pr.status_code}")

    # Moving it to Batch B must flip visibility both ways.
    t.patch(f"/courses/recordings/{rid}/", json={"batch_id": bB})
    a2 = {r.get("id") for r in rows(sa.get(f"/courses/subjects/{sid}/recordings/"))}
    b2 = {r.get("id") for r in rows(sb.get(f"/courses/subjects/{sid}/recordings/"))}
    check(A, "re-assigning to Batch B hides it from A", rid not in a2)
    check(A, "re-assigning to Batch B reveals it to B", rid in b2)

    # Course-wide reaches both.
    t.patch(f"/courses/recordings/{rid}/", json={"batch_id": None})
    a3 = {r.get("id") for r in rows(sa.get(f"/courses/subjects/{sid}/recordings/"))}
    b3 = {r.get("id") for r in rows(sb.get(f"/courses/subjects/{sid}/recordings/"))}
    check(A, "clearing the batch makes it course-wide for BOTH",
          rid in a3 and rid in b3)

    t.delete(f"/courses/recordings/{rid}/delete/")


def verify_recording_live_session_override(t, sid, bA, bB):
    """The flagged bug: uploading from a Live Session detail page silently
    overrides an explicit 'all batches' choice with that session's batch."""
    A = "RECORDINGS/BATCH"
    ls = t.get("/livestream/teacher/sessions/")
    sessions = rows(ls)
    # The teacher serializer exposes `batch_name`, not a `batch` id.
    batched = [s for s in sessions if s.get("batch_name")]
    if not batched:
        record(A, "find a live session with a batch to test the override",
               False, "no batched live session in the seed")
        return
    session = batched[0]

    made = t.post(f"/courses/subjects/{sid}/recordings/save/", json={
        "title": "From a live session, explicitly ALL batches",
        "video_id": f"verify-{uuid.uuid4().hex[:8]}",
        "live_session_id": session["id"],
        "batch_id": None,          # explicit "all batches"
    })
    if not made.ok:
        record(A, "save a recording from a live session with batch_id=None",
               False, f"HTTP {made.status_code} {made.text[:180]}")
        return
    rid = made.json()["id"]
    got = made.json().get("batch")
    check(A, "an explicit 'all batches' is NOT overridden by the "
             "live session's batch",
          got is None,
          f"session batch={session.get('batch_name')!r} -> recording.batch={got}")
    t.delete(f"/courses/recordings/{rid}/delete/")


# ===========================================================================
def verify_chapter_tagging(t, sid):
    """Official chapter + the teacher's own custom label + a no-chapter
    default, and promoting a custom one into the course."""
    A = "CHAPTERS"
    ch = t.get(f"/courses/subjects/{sid}/chapters/")
    chapters = rows(ch)
    check(A, "the subject's official chapters are listable",
          ch.status_code == 200 and len(chapters) > 0,
          f"HTTP {ch.status_code}, {len(chapters)} chapters")
    if not chapters:
        return
    ch_id = chapters[0]["id"]

    def upload(title, extra):
        up = t.post("/materials/files/upload/",
                    files={"file": ("c.pdf", io.BytesIO(b"%PDF-1.4\nc\n"),
                                    "application/pdf")})
        data = {"title": title, "subject_id": sid, "file_ids": up.json()["id"]}
        data.update(extra)
        return t.post("/materials/materials/upload/", data=data)

    # 1. an OFFICIAL chapter
    r1 = upload("Tagged to an official chapter",
                {"chapter_tags": '[{"chapter_id": "%s"}]' % ch_id})
    if check(A, "OPTION 1: tag to an official course chapter", r1.ok,
             f"HTTP {r1.status_code} {r1.text[:160]}"):
        tags = r1.json().get("chapter_tags", [])
        check(A, "  ...and the tag comes back", len(tags) == 1, str(tags)[:120])
        t.delete(f"/materials/materials/{r1.json()['id']}/delete/")

    # 2. the teacher's OWN custom label, not promoted to the course
    label = f"My own topic {uuid.uuid4().hex[:4]}"
    r2 = upload("Tagged to my own label",
                {"chapter_tags": '[{"label": "%s"}]' % label})
    if check(A, "OPTION 2: tag with the teacher's OWN custom label", r2.ok,
             f"HTTP {r2.status_code} {r2.text[:160]}"):
        tags = r2.json().get("chapter_tags", [])
        check(A, "  ...the custom label round-trips",
              any(t_.get("label") == label or t_.get("custom_label") == label
                  for t_ in tags), str(tags)[:160])
        after = rows(t.get(f"/courses/subjects/{sid}/chapters/"))
        check(A, "  ...and it does NOT pollute the course chapter list",
              len(after) == len(chapters),
              f"{len(chapters)} -> {len(after)} chapters")
        t.delete(f"/materials/materials/{r2.json()['id']}/delete/")

    # 3. the "no specific chapter" default
    r3 = upload("No specific chapter", {"no_specific_chapter": "true"})
    if check(A, "OPTION 3: the 'no specific chapter' default", r3.ok,
             f"HTTP {r3.status_code} {r3.text[:160]}"):
        check(A, "  ...it is stored as a real state, not just empty tags",
              r3.json().get("no_specific_chapter") is True,
              str(r3.json().get("no_specific_chapter")))
        t.delete(f"/materials/materials/{r3.json()['id']}/delete/")

    # 4. promote a custom label into the course's real chapter list
    promote = f"Promoted topic {uuid.uuid4().hex[:4]}"
    r4 = upload("Promote my label to the course", {
        "chapter_tags": '[{"label": "%s"}]' % promote,
        "save_chapters_to_course": "true",
    })
    if check(A, "OPTION 4: promote a custom label into the course", r4.ok,
             f"HTTP {r4.status_code} {r4.text[:160]}"):
        after = rows(t.get(f"/courses/subjects/{sid}/chapters/"))
        check(A, "  ...the course chapter list GREW by one",
              len(after) == len(chapters) + 1,
              f"{len(chapters)} -> {len(after)}")
        check(A, "  ...and the new chapter carries the teacher's label",
              any(c.get("title") == promote for c in after),
              [c.get("title") for c in after][-3:])
        t.delete(f"/materials/materials/{r4.json()['id']}/delete/")

    # 5. combining several tags at once
    r5 = upload("Official + my own together", {
        "chapter_tags": '[{"chapter_id": "%s"}, {"label": "extra topic"}]' % ch_id,
    })
    if check(A, "OPTION 5: an official chapter AND a custom label together",
             r5.ok, f"HTTP {r5.status_code} {r5.text[:160]}"):
        check(A, "  ...both tags are kept",
              len(r5.json().get("chapter_tags", [])) == 2,
              str(r5.json().get("chapter_tags"))[:180])
        t.delete(f"/materials/materials/{r5.json()['id']}/delete/")


# ===========================================================================
def main():
    print("=" * 74)
    t = as_teacher()
    sa = as_learner(STUDENT_A)
    sb = as_learner(STUDENT_B)

    sid, _ = teacher_scope(t)
    bs = batches(t)
    by_code = {b.get("code"): b["id"] for b in bs}
    bA = by_code.get("DEMO-A1") or bs[0]["id"]
    bB = by_code.get("DEMO-B1")
    if not bB:
        print("!! Batch B missing — run the fixture in this file's docstring")
        return 1
    print(f"subject={sid}\nbatchA={bA}\nbatchB={bB}\n" + "=" * 74)

    verify_material_batch_scoping(t, sid, bA, bB, sa, sb)
    verify_quiz_batch_scoping(t, sid, bA, bB, sa, sb)
    verify_recording_batch_scoping(t, sid, bA, bB, sa, sb)
    verify_recording_live_session_override(t, sid, bA, bB)
    verify_chapter_tagging(t, sid)

    print("=" * 74)
    failed = [r for r in RESULTS if not r[2]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("\nFAILURES:")
        for area, name, _, detail in failed:
            print(f"  · {area}: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
