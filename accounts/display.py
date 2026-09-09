"""How a User's name is rendered to another human.

Extracted because this exact fallback chain was already open-coded in
`courses/serializers_recordings.py` and was about to be copied into
`materials/serializers.py` and the teacher resources hub — three copies of a
rule that has to agree, since the same teacher appears as the owner of a
recording, a material and an assignment in ONE list on the hub. Three copies
would eventually render the same person under two different names in adjacent
rows.

WHAT THIS ACTUALLY RESOLVES TO TODAY: `first last`, else `username`.

There is no display-name table to consult. `User` has no `profile` relation at
all (`hasattr(User, "profile")` is False — its relations are
`learner_profiles`, `teacher_profile`, `forum_profile`, `document_profile`,
`counselor_profile`), and `TeacherProfile` has no `full_name` column. The
`profile` lookup below is inherited verbatim from the recordings serializer,
where it has therefore never once fired.

It is kept, rather than deleted, for one reason: removing it would be a silent
behaviour change if a `profile` relation is ever added, and `getattr` with a
default is free. It is NOT evidence that such a relation exists.

The practical consequence is worth knowing before reading a screen: an account
with no first/last name renders as its raw username. That is not this
function misbehaving — it is the only name the database holds.
"""


def display_name_for(user):
    """Best human-readable name for `user`, or None if there is no user.

    Returns None rather than a placeholder like "Unknown": a NULL owner is a
    real state on Assignment.created_by and Quiz.created_by (rows predating the
    column, or whose author's account was deleted), and only the caller knows
    whether that should read "Unknown", "—", or be hidden entirely. Baking a
    string in here would make an absent owner indistinguishable from a teacher
    genuinely named "Unknown".
    """
    if not user:
        return None

    # See the module docstring: this never fires against the current schema.
    # `getattr` with a default is also what makes it safe for a reverse
    # one-to-one that has no row — Django's RelatedObjectDoesNotExist subclasses
    # AttributeError, so it is swallowed here rather than raised.
    profile = getattr(user, "profile", None)
    if profile is not None:
        full_name = getattr(profile, "full_name", None)
        if full_name:
            return full_name

    # get_full_name() is "" (not None) for an account with neither name set,
    # hence `or` rather than a None check.
    return user.get_full_name() or user.username
