"""
Board display helpers — the ONE convention for putting a board on an API payload.

Why this exists
---------------
Course titles were normalised to plain "Class 9" / "Class 11 Science", with the
board deliberately stripped out of the title. Two published courses can now
carry the SAME title and differ only by board (MBSE, board_type=STATE, vs CBSE,
board_type=CENTRAL — genuinely different syllabi, not duplicates). So any
payload that names a course, subject, batch or session without its board is
ambiguous to the user, and any code that treats a title as an identity is now
outright wrong.

The convention: a flat, nullable `board_name` string
----------------------------------------------------
New payloads add `board_name` — NOT a nested object, NOT `board`. Reasons:

- `Course.board` is `null=True` (`courses/models.py:108`), so this is always
  Optional[str] and every consumer has to handle None regardless.
- The pre-existing field names are a mess — `/teacher/my-classes/` sends
  `board_name`, `/teacher/my-batches/` and `/courses/:id/public/` send a flat
  `board` string, `CourseSerializer` sends a nested object, and
  `SubjectSerializer` sends `{id, name, board_type}`. Those all stay as they
  are (they have live consumers); `board_name` is what NEW additions use, and
  the frontend `BoardPill` reads all of these shapes tolerantly.
- List endpoints are the common case here and a nested serializer per row is
  wasted work when the only thing rendered is the name.

Always pair a `board_name` addition with `select_related` on the path to the
board, or a list endpoint gains one query per row. `board_name_for()` does not
and cannot do that for you — it only reads what the ORM already fetched.
"""


def board_name_for(obj):
    """Flat board name for a Course, or None.

    Accepts None so callers can write `board_name_for(getattr(x, "course", None))`
    without guarding first — a session with no course, or a course with no board,
    both collapse to None rather than raising.
    """
    if obj is None:
        return None
    board = getattr(obj, "board", None)
    if board is None:
        return None
    # `board` is normally a Board instance, but a few older payloads stash a
    # plain string there; be tolerant rather than returning a repr.
    if isinstance(board, str):
        return board or None
    return getattr(board, "name", None) or None


def board_name_via(obj, *path):
    """`board_name_for` after walking an attribute path, None-safe at every hop.

    Saves the repeated `obj.subject.course` chains in serializers where any
    link can legitimately be null:

        board_name_via(assignment, "subject", "course")
        board_name_via(session, "batch", "course")
    """
    cur = obj
    for attr in path:
        if cur is None:
            return None
        cur = getattr(cur, attr, None)
    return board_name_for(cur)
