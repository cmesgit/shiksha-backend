"""End-to-end production verification against a real running backend.

Drives the actual HTTP surface as a real client would: materials upload/
delete, quiz create/assign/attempt/review, recording list/edit/trim/delete/
playback, plus the student and admin sides of each. NOT a unit test — it
exercises the real request path (auth cookies, permission classes,
serializers, DB) over HTTP against a running server.

SETUP
    # 1. a local DB with demo content
    python manage.py migrate    --settings=config.settings_test
    python manage.py seed_demo_data --settings=config.settings_test

    # 2. an admin account (the seed makes no staff user)
    python manage.py shell --settings=config.settings_test -c "
    from accounts.models import User
    u,_ = User.objects.get_or_create(email='verify.admin@shiksha.test',
                                     defaults={'username':'verify.admin'})
    u.is_staff = u.is_superuser = u.is_verified = True
    u.set_password('ShikshaDemo@2026'); u.save()"

    # 3. the server
    python manage.py runserver 127.0.0.1:8001 --settings=config.settings_test

    # 4. this script
    python scripts/verify_flows.py

WHAT IT CANNOT COVER
    Recording UPLOAD. The bytes go browser -> Bunny directly over a TUS
    ticket; with BUNNY_LIBRARY_ID/BUNNY_API_KEY unset locally the slot call
    returns 401, and dev shares prod's Bunny zone so it must not be
    rehearsed there either. Recording rows are created here through
    `recordings/save/` instead, which is the same row the upload produces.
    Playback likewise reports 503 `playback_not_configured` locally.

NOTE ON COOKIES
    Auth is cookie-only (there is no Bearer support) and the cookies are set
    secure=True, which a plain-http cookiejar silently drops. Client._absorb
    lifts them out of the response and re-sets them flagless. Without that,
    every request after login is a 401 and the cause is invisible.
"""
import io
import json
import os
import sys
import uuid

import requests

# Override with SHIKSHA_VERIFY_BASE when 8001 is already taken (e.g. a second
# runserver on 8002 via the backend-verify-8002 launch config).
BASE = os.environ.get("SHIKSHA_VERIFY_BASE", "http://127.0.0.1:8001") + "/api"
PW = "ShikshaDemo@2026"

RESULTS = []


def record(area, name, ok, detail=""):
    RESULTS.append((area, name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {area}: {name}" + (f"  — {detail}" if detail else ""))


def check(area, name, cond, detail=""):
    record(area, name, bool(cond), detail)
    return bool(cond)


class Client:
    """requests.Session that stores the backend's `secure` cookies over http."""

    def __init__(self):
        self.s = requests.Session()

    def _absorb(self, r):
        # The auth cookies are set secure=True; a plain-http cookiejar drops
        # them, so lift them out of the response and set them flagless.
        for name in ("access", "refresh"):
            if name in r.cookies:
                self.s.cookies.set(name, r.cookies[name])

    def req(self, method, path, **kw):
        r = self.s.request(method, BASE + path, timeout=30, **kw)
        self._absorb(r)
        return r

    def get(self, p, **k):
        return self.req("GET", p, **k)

    def post(self, p, **k):
        return self.req("POST", p, **k)

    def patch(self, p, **k):
        return self.req("PATCH", p, **k)

    def delete(self, p, **k):
        return self.req("DELETE", p, **k)


def login(email, password=PW):
    c = Client()
    r = c.post("/accounts/login/", json={"email": email, "password": password})
    if r.status_code != 200:
        raise SystemExit(f"login failed for {email}: {r.status_code} {r.text[:300]}")
    return c, r.json()


def as_teacher(email="demo.faculty@shiksha.test"):
    c, _ = login(email)
    r = c.post("/accounts/context/teacher/", json={"password": PW})
    if r.status_code != 200:
        raise SystemExit(f"teacher context failed: {r.status_code} {r.text[:300]}")
    return c


def teacher_scope(t):
    """(subject_id, batch_id) for the faculty account.

    NOT /courses/subjects/mine/ — that is the LEARNER view and returns []
    for a teacher account. The teacher's own subjects are reachable through
    their recordings; batches through teacher/my-batches/.
    """
    subject_id = None
    r = t.get("/courses/teacher/recordings/all/")
    if r.ok and r.json():
        subject_id = r.json()[0]["subject"]
    batch_id = None
    b = t.get("/courses/teacher/my-batches/")
    if b.ok:
        for g in b.json().get("groups", []):
            for bat in g.get("batches", []):
                batch_id = bat["id"]
                break
    return subject_id, batch_id


def as_learner(email):
    c, body = login(email)
    if body.get("context") != "learner":
        profiles = body.get("profiles") or []
        if not profiles:
            raise SystemExit(f"no profiles for {email}: {body}")
        pid = profiles[0]["id"]
        r = c.post("/accounts/profiles/select/", json={"profile_id": pid})
        if r.status_code != 200:
            raise SystemExit(f"profile select failed: {r.status_code} {r.text[:300]}")
    return c


# ===========================================================================
def verify_materials(t):
    A = "MATERIALS"
    subject_id, _ = teacher_scope(t)
    if not subject_id:
        record(A, "find a subject to work in", False, "no subject resolved")
        return None

    before = t.get("/materials/teacher/materials/all/")
    check(A, "teacher can list materials", before.status_code == 200,
          f"HTTP {before.status_code}, {len(before.json()) if before.ok else '?'} rows")
    n_before = len(before.json()) if before.ok else 0

    # --- step 1: upload the file itself
    f = io.BytesIO(b"%PDF-1.4\n% verification fixture\n")
    up = t.post("/materials/files/upload/",
                files={"file": ("verify-note.pdf", f, "application/pdf")})
    if not check(A, "upload a PDF", up.status_code in (200, 201),
                 f"HTTP {up.status_code} {up.text[:160]}"):
        return subject_id
    file_id = up.json().get("id")

    # --- blocked extension must be refused
    bad = t.post("/materials/files/upload/",
                 files={"file": ("evil.html", io.BytesIO(b"<script>x</script>"),
                                 "text/html")})
    check(A, "blocked extension (.html) is refused", bad.status_code == 400,
          f"HTTP {bad.status_code}")

    # --- step 2: create the material row
    made = t.post("/materials/materials/upload/",
                  data={"title": "Verification note", "subject_id": subject_id,
                        "file_ids": file_id, "no_specific_chapter": "true"})
    if not check(A, "create the material", made.status_code in (200, 201),
                 f"HTTP {made.status_code} {made.text[:200]}"):
        return subject_id
    material_id = made.json().get("id")

    after = t.get("/materials/teacher/materials/all/")
    check(A, "the new material appears in the list",
          after.ok and len(after.json()) == n_before + 1,
          f"{n_before} -> {len(after.json()) if after.ok else '?'}")

    det = t.get(f"/materials/materials/{material_id}/")
    check(A, "material detail reads back", det.status_code == 200,
          f"HTTP {det.status_code}")

    # --- delete
    dele = t.delete(f"/materials/materials/{material_id}/delete/")
    check(A, "delete the material", dele.status_code == 204,
          f"HTTP {dele.status_code}")
    gone = t.get(f"/materials/materials/{material_id}/")
    check(A, "deleted material is really gone", gone.status_code == 404,
          f"HTTP {gone.status_code}")
    return subject_id


def verify_materials_admin(admin, teacher):
    """An admin must be able to see and remove teacher-uploaded content.

    NOT via /materials/teacher/materials/all/ — that endpoint joins through
    the caller's own TeachingAssignments, so it is empty for an admin by
    construction and staying teacher-context-only there is correct. The
    surfaces an admin actually needs are the subject list, the detail, and
    delete. All three used to 403: the delete was gated on IsTeacherContext
    (which rejects a pure admin at the class gate, making its own is_staff
    branch dead code) and the read gate had no staff branch at all.
    """
    A = "MATERIALS"
    subject_id, _ = teacher_scope(teacher)

    lst = admin.get(f"/materials/subjects/{subject_id}/materials/")
    if not check(A, "ADMIN can list a subject's materials",
                 lst.status_code == 200, f"HTTP {lst.status_code}"):
        return
    rows = lst.json()
    rows = rows.get("results", rows) if isinstance(rows, dict) else rows
    if not rows:
        record(A, "ADMIN sees at least one material to moderate", False,
               "subject has no materials")
        return

    mid = rows[0]["id"]
    det = admin.get(f"/materials/materials/{mid}/")
    check(A, "ADMIN can open a material detail", det.status_code == 200,
          f"HTTP {det.status_code}")

    # Delete a throwaway so the seed survives.
    up = teacher.post("/materials/files/upload/",
                      files={"file": ("admin-del.pdf",
                                      io.BytesIO(b"%PDF-1.4\nx\n"),
                                      "application/pdf")})
    made = teacher.post("/materials/materials/upload/", data={
        "title": "Admin will delete this", "subject_id": subject_id,
        "file_ids": up.json()["id"], "no_specific_chapter": "true",
    })
    if made.ok:
        tid = made.json()["id"]
        d = admin.delete(f"/materials/materials/{tid}/delete/")
        check(A, "ADMIN can delete a teacher's material",
              d.status_code in (200, 204), f"HTTP {d.status_code}")
        check(A, "...and it is really gone",
              admin.get(f"/materials/materials/{tid}/").status_code == 404)


# ===========================================================================
def verify_quizzes(t, student):
    A = "QUIZZES"
    lst = t.get("/teacher/quizzes/all/")
    if not check(A, "teacher can list quizzes", lst.status_code == 200,
                 f"HTTP {lst.status_code}"):
        return
    quizzes = lst.json()
    quizzes = quizzes.get("results", quizzes) if isinstance(quizzes, dict) else quizzes
    check(A, "seeded quizzes are present", len(quizzes) > 0, f"{len(quizzes)} rows")

    subject_id, batch_id = teacher_scope(t)
    if not (subject_id and batch_id):
        record(A, "find a subject+batch", False, f"{subject_id} / {batch_id}")
        return

    made = t.post("/teacher/quizzes/", json={
        "subject": subject_id, "batch_id": batch_id,
        "title": "Verification quiz", "quiz_type": "mock",
        "time_limit_minutes": 5, "no_specific_chapter": True,
    })
    if not check(A, "create a quiz", made.status_code in (200, 201),
                 f"HTTP {made.status_code} {made.text[:200]}"):
        return
    quiz_id = made.json()["id"]

    qs = t.put(BASE + f"/teacher/quizzes/{quiz_id}/questions/bulk/", json={
        "questions": [{
            "text": "What is 2 + 2?", "marks": 1, "order": 1,
            "explanation": "Basic arithmetic.", "difficulty": "easy",
            "choices": [{"text": "4", "is_correct": True},
                        {"text": "5", "is_correct": False}],
        }],
    }) if False else t.req("PUT", f"/teacher/quizzes/{quiz_id}/questions/bulk/", json={
        "questions": [{
            "text": "What is 2 + 2?", "marks": 1, "order": 1,
            "explanation": "Basic arithmetic.", "difficulty": "easy",
            "choices": [{"text": "4", "is_correct": True},
                        {"text": "5", "is_correct": False}],
        }],
    })
    check(A, "add a question", qs.status_code in (200, 201),
          f"HTTP {qs.status_code} {qs.text[:200]}")

    # A question with two correct answers must be refused.
    badq = t.req("PUT", f"/teacher/quizzes/{quiz_id}/questions/bulk/", json={
        "questions": [{
            "text": "Broken", "marks": 1, "order": 1, "explanation": "x",
            "choices": [{"text": "a", "is_correct": True},
                        {"text": "b", "is_correct": True}],
        }],
    })
    check(A, "two correct answers is refused", badq.status_code == 400,
          f"HTTP {badq.status_code}")

    # --- not assigned yet: student must NOT see it
    sq = student.get("/student/quizzes/")
    if sq.ok:
        body = sq.json()
        rows = body.get("results", body) if isinstance(body, dict) else body
        ids = {q.get("id") for q in rows}
        check(A, "an UNASSIGNED quiz is hidden from students",
              quiz_id not in ids, f"{len(ids)} visible")
    else:
        record(A, "student quiz list", False, f"HTTP {sq.status_code} {sq.text[:150]}")

    start_blocked = student.post(f"/quizzes/{quiz_id}/start/", json={})
    check(A, "starting an unassigned quiz is refused",
          start_blocked.status_code in (403, 404),
          f"HTTP {start_blocked.status_code}")

    # --- assign it
    asg = t.patch(f"/teacher/quizzes/{quiz_id}/assign/",
                  json={"assign": True, "batch_ids": [batch_id]})
    check(A, "assign the quiz to a batch", asg.status_code == 200,
          f"HTTP {asg.status_code} {asg.text[:200]}")

    sq2 = student.get("/student/quizzes/")
    if sq2.ok:
        body = sq2.json()
        rows = body.get("results", body) if isinstance(body, dict) else body
        ids = {q.get("id") for q in rows}
        check(A, "an ASSIGNED quiz becomes visible to the student",
              quiz_id in ids, f"{len(ids)} visible")

    # --- attempt it
    st = student.post(f"/quizzes/{quiz_id}/start/", json={})
    if not check(A, "student can start the quiz", st.status_code in (200, 201),
                 f"HTTP {st.status_code} {st.text[:200]}"):
        return
    # start/ returns only {detail, attempt_id, started_at, expires_at} —
    # the questions come from the quiz detail endpoint once an attempt is open.
    detail = student.get(f"/quizzes/{quiz_id}/")
    questions = detail.json().get("questions", []) if detail.ok else []
    if not check(A, "the attempt carries questions", len(questions) > 0,
                 f"GET /quizzes/<id>/ HTTP {detail.status_code}, "
                 f"{len(questions)} questions"):
        return
    q0 = questions[0]
    check(A, "the answer key is NOT sent to the student",
          all("is_correct" not in c for c in q0.get("choices", [])),
          "no is_correct on any choice")

    correct = next((c for c in q0["choices"] if c["text"] == "4"), q0["choices"][0])
    sub = student.post(f"/student/quizzes/{quiz_id}/submit/", json={
        "answers": [{"question": q0["id"], "selected_choice": correct["id"]}],
    })
    if check(A, "student can submit", sub.status_code in (200, 201),
             f"HTTP {sub.status_code} {sub.text[:200]}"):
        body = sub.json()
        check(A, "a correct answer scores", body.get("score") == 1,
              f"score={body.get('score')} / {body.get('total_marks')}")

    res = student.get(f"/quizzes/{quiz_id}/result/")
    check(A, "student can read the result", res.status_code == 200,
          f"HTTP {res.status_code}")

    att = t.get(f"/teacher/quizzes/{quiz_id}/attempts/")
    check(A, "teacher can review attempts", att.status_code == 200,
          f"HTTP {att.status_code}")

    ana = t.get(f"/teacher/quizzes/{quiz_id}/analytics/")
    check(A, "teacher can read analytics", ana.status_code == 200,
          f"HTTP {ana.status_code}")

    # Deleting a quiz students have attempted is guarded: 409 + requires_force,
    # so a teacher cannot silently destroy scores with one click.
    guard = t.delete(f"/teacher/quizzes/{quiz_id}/delete/")
    check(A, "deleting an ATTEMPTED quiz is guarded (409 + force)",
          guard.status_code == 409 and guard.json().get("requires_force") is True,
          f"HTTP {guard.status_code}")
    forced = t.delete(f"/teacher/quizzes/{quiz_id}/delete/?force=true")
    check(A, "teacher can delete with ?force=true",
          forced.status_code in (200, 204), f"HTTP {forced.status_code}")
    check(A, "the deleted quiz is really gone",
          t.get(f"/quizzes/{quiz_id}/").status_code == 404)


def verify_quiz_admin_review(t, admin):
    A = "QUIZ REVIEW"
    subject_id, batch_id = teacher_scope(t)
    if not (subject_id and batch_id):
        record(A, "find a subject+batch", False)
        return
    made = t.post("/teacher/quizzes/", json={
        "subject": subject_id, "batch_id": batch_id,
        "title": "Review-queue quiz", "quiz_type": "mock",
        "time_limit_minutes": 5, "no_specific_chapter": True,
    })
    if made.status_code not in (200, 201):
        record(A, "create a quiz to review", False, f"HTTP {made.status_code}")
        return
    quiz_id = made.json()["id"]
    t.req("PUT", f"/teacher/quizzes/{quiz_id}/questions/bulk/", json={
        "questions": [{"text": "Q?", "marks": 1, "order": 1, "explanation": "e",
                       "choices": [{"text": "a", "is_correct": True},
                                   {"text": "b", "is_correct": False}]}]})

    sfr = t.patch(f"/teacher/quizzes/{quiz_id}/submit-for-review/")
    check(A, "teacher submits for review", sfr.status_code == 200,
          f"HTTP {sfr.status_code} {sfr.text[:160]}")

    q = admin.get("/quizzes/admin/?status=pending")
    if check(A, "admin sees the pending queue", q.status_code == 200,
             f"HTTP {q.status_code}"):
        body = q.json()
        rows = body.get("results", body) if isinstance(body, dict) else body
        check(A, "the submitted quiz is IN the queue",
              any(r.get("id") == quiz_id for r in rows), f"{len(rows)} pending")

    bad = admin.post(f"/quizzes/admin/{quiz_id}/review/", json={"action": "reject"})
    check(A, "rejecting without a reason is refused", bad.status_code == 400,
          f"HTTP {bad.status_code}")

    ok = admin.post(f"/quizzes/admin/{quiz_id}/review/",
                    json={"action": "approve", "reason": ""})
    check(A, "admin can approve", ok.status_code == 200,
          f"HTTP {ok.status_code} {ok.text[:160]}")

    t.delete(f"/teacher/quizzes/{quiz_id}/delete/")


# ===========================================================================
def verify_recordings(t, student, admin):
    A = "RECORDINGS"
    lst = t.get("/courses/teacher/recordings/all/")
    if not check(A, "teacher can list recordings", lst.status_code == 200,
                 f"HTTP {lst.status_code}"):
        return
    recs = lst.json()
    if not check(A, "a seeded recording exists", len(recs) > 0, f"{len(recs)} rows"):
        return
    rec = recs[0]
    rid = rec["id"]

    # --- EDIT (the endpoint that did not exist before this work)
    ed = t.patch(f"/courses/recordings/{rid}/", json={"title": "Renamed by verification"})
    if check(A, "EDIT: rename a recording", ed.status_code == 200,
             f"HTTP {ed.status_code} {ed.text[:200]}"):
        check(A, "the new title is persisted",
              t.get(f"/courses/recordings/{rid}/").json()["title"]
              == "Renamed by verification")

    # --- the whitelist is the security boundary
    hijack = t.patch(f"/courses/recordings/{rid}/", json={
        "bunny_video_id": "someone-elses-video", "status": 0,
        "duration_seconds": 99999,
    })
    if hijack.status_code == 200:
        now = t.get(f"/courses/recordings/{rid}/").json()
        check(A, "protected fields are NOT writable",
              now["bunny_video_id"] == rec["bunny_video_id"]
              and now["status"] == rec["status"]
              and now["duration_seconds"] == rec["duration_seconds"],
              "bunny_video_id / status / duration unchanged")
    else:
        record(A, "protected fields are NOT writable", False,
               f"PATCH itself failed: {hijack.status_code}")

    # --- TRIM
    tr = t.patch(f"/courses/recordings/{rid}/",
                 json={"trim_start_seconds": 10, "trim_end_seconds": 60})
    if check(A, "TRIM: set a window", tr.status_code == 200,
             f"HTTP {tr.status_code} {tr.text[:200]}"):
        check(A, "effective duration reflects the trim",
              tr.json().get("effective_duration_seconds") == 50,
              f"= {tr.json().get('effective_duration_seconds')}")
    inv = t.patch(f"/courses/recordings/{rid}/",
                  json={"trim_start_seconds": 300, "trim_end_seconds": 100})
    check(A, "an inverted trim is a 400, not a 500", inv.status_code == 400,
          f"HTTP {inv.status_code}")

    # --- PLAYBACK
    pb = t.get(f"/courses/recordings/{rid}/playback/")
    if pb.status_code == 200:
        body = pb.json()
        check(A, "playback returns an embed url", bool(body.get("embed_url")))
        check(A, "playback uses Bunny's `t=` seek param, not `start=`",
              "t=" in body["embed_url"] and "start=" not in body["embed_url"],
              body["embed_url"][:110])
        check(A, "playback reports token_auth honestly",
              "token_auth" in body, f"token_auth={body.get('token_auth')}")
    else:
        check(A, "playback endpoint responds",
              pb.status_code == 503, f"HTTP {pb.status_code} (503 = Bunny unset)")

    # --- STUDENT gating
    sp = student.get(f"/courses/recordings/{rid}/")
    check(A, "an enrolled student can read a published recording",
          sp.status_code == 200, f"HTTP {sp.status_code}")
    sed = student.patch(f"/courses/recordings/{rid}/", json={"title": "hijack"})
    check(A, "a student CANNOT edit a recording", sed.status_code == 403,
          f"HTTP {sed.status_code}")
    sdel = student.delete(f"/courses/recordings/{rid}/delete/")
    check(A, "a student CANNOT delete a recording", sdel.status_code == 403,
          f"HTTP {sdel.status_code}")

    # --- unpublished must be invisible
    t.patch(f"/courses/recordings/{rid}/", json={"is_published": False})
    hid = student.get(f"/courses/recordings/{rid}/")
    check(A, "an UNPUBLISHED recording is hidden from students",
          hid.status_code == 403, f"HTTP {hid.status_code}")
    hidpb = student.get(f"/courses/recordings/{rid}/playback/")
    check(A, "...and its playback URL too", hidpb.status_code == 403,
          f"HTTP {hidpb.status_code}")
    t.patch(f"/courses/recordings/{rid}/", json={"is_published": True})

    # --- ADMIN parity
    aed = admin.patch(f"/courses/recordings/{rid}/", json={"title": "Admin edit"})
    check(A, "ADMIN can edit a recording", aed.status_code == 200,
          f"HTTP {aed.status_code}")
    apb = admin.get(f"/courses/recordings/{rid}/playback/")
    check(A, "ADMIN can get a playback URL", apb.status_code in (200, 503),
          f"HTTP {apb.status_code}")
    alist = admin.get("/livestream/admin/recordings/")
    check(A, "ADMIN recordings list works", alist.status_code == 200,
          f"HTTP {alist.status_code}")

    # --- PROGRESS
    student.post(f"/courses/recordings/{rid}/progress/save/",
                 json={"last_position": 30})
    pr = student.get(f"/courses/recordings/{rid}/progress/")
    if check(A, "student watch-progress round-trips", pr.status_code == 200,
             f"HTTP {pr.status_code}"):
        check(A, "progress carries the resolved trim window",
              "effective_duration_seconds" in pr.json(), str(pr.json())[:160])

    # --- DELETE (on a throwaway row so the seed survives)
    sid, _ = teacher_scope(t)
    tmp = t.post(f"/courses/subjects/{sid}/recordings/save/", json={
        "title": "Delete me", "video_id": f"verify-{uuid.uuid4().hex[:8]}",
        "session_date": "2026-08-01",
    })
    if check(A, "create a recording row via save/", tmp.status_code in (200, 201),
             f"HTTP {tmp.status_code} {tmp.text[:160]}"):
        tid = tmp.json()["id"]
        d = t.delete(f"/courses/recordings/{tid}/delete/")
        check(A, "DELETE a recording", d.status_code == 204, f"HTTP {d.status_code}")
        check(A, "deleted recording is really gone",
              t.get(f"/courses/recordings/{tid}/").status_code == 404)

    # --- the removed insecure create route
    old = t.post(f"/courses/subjects/{sid}/recordings/create/",
                 json={"title": "x", "bunny_video_id": "stolen"})
    check(A, "the old client-supplied-video_id route is gone",
          old.status_code == 404, f"HTTP {old.status_code}")


# ===========================================================================
def main():
    print("=" * 74)
    teacher = as_teacher()
    student = as_learner("demo.student@shiksha.test")
    admin = Client()
    r = admin.post("/accounts/login/", json={"email": "verify.admin@shiksha.test",
                                             "password": PW})
    if r.status_code != 200:
        print("!! admin login failed — admin checks will be skipped")
        admin = None

    verify_materials(teacher)
    if admin:
        verify_materials_admin(admin, teacher)
    verify_quizzes(teacher, student)
    if admin:
        verify_quiz_admin_review(teacher, admin)
        verify_recordings(teacher, student, admin)

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
